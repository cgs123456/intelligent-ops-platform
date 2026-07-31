"""WarehouseService 数仓 ETL 流水线测试

覆盖：
- 基础血缘链初始化（_init_lineage 幂等）
- 全量 ETL：ODS→DWD→DWS→ADS（run_full_pipeline）
- ODS 贴源层增量拉取 + 去重（_etl_ods）
- DWD 明细层清洗 + 关联维度 + 去重（_etl_dwd）
- DWS 汇总层 SKU+日聚合（_etl_dws）
- ADS 应用层补货建议 + 经营日报（_etl_ads）
- 数据质量测试 not_null/unique/range（run_data_quality_tests）
- 历史回刷 backfill
- 各层记录数统计 get_layer_stats
- 血缘查询 get_lineage
- DQ 报告聚合 get_dq_report
- ADS 数据查询 get_ads_data（含缓存失效）
"""

from datetime import date, datetime, timedelta

import pytest

from extensions import cache, db
from models.erp import Product, PurchaseOrder, SaleOrder, StockMove, Supplier
from models.warehouse import (
    AdsDailyOpsReport,
    AdsReplenishmentSuggest,
    DataLineage,
    DataQualityLog,
    DwdSalesFact,
    DwdStockFact,
    DwsInventoryDaily,
    DwsSalesSkuDaily,
    EtlMeta,
    OdsPurchaseOrder,
    OdsSaleOrder,
    OdsStockMove,
)
from services.warehouse_service import WarehouseService


@pytest.fixture(autouse=True)
def _cleanup_warehouse_tables(app):
    """每个测试前后清理数仓 + ERP 相关表，避免数据污染。

    WarehouseService 方法会 commit，db_session fixture 的事务回滚会失效，
    故需显式清理（与 test_erp_service.py 同模式）。
    """
    models_to_clean = [
        DataQualityLog,
        DataLineage,
        EtlMeta,
        AdsReplenishmentSuggest,
        AdsDailyOpsReport,
        DwsSalesSkuDaily,
        DwsInventoryDaily,
        DwdSalesFact,
        DwdStockFact,
        OdsSaleOrder,
        OdsPurchaseOrder,
        OdsStockMove,
        StockMove,
        SaleOrder,
        PurchaseOrder,
        Product,
        Supplier,
    ]
    with app.app_context():
        for m in models_to_clean:
            db.session.query(m).delete()
        db.session.commit()
        # 清理 ADS 缓存（@cache.memoize 跨测试可能残留）
        try:
            cache.delete_memoized(WarehouseService.get_ads_data)
        except Exception:
            pass
    yield
    with app.app_context():
        for m in models_to_clean:
            db.session.query(m).delete()
        db.session.commit()
        try:
            cache.delete_memoized(WarehouseService.get_ads_data)
        except Exception:
            pass


@pytest.fixture
def seed_erp_data(app):
    """创建基础 ERP 数据：1 供应商 + 2 产品 + 销售单 + 采购单 + 库存移动"""
    with app.app_context():
        supplier = Supplier(name="测试供应商W", lead_days=3, rating="A")
        db.session.add(supplier)
        db.session.flush()

        # 产品 A：低库存（触发补货建议）
        p_low = Product(
            sku="WHS-LOW-001",
            name="低库存商品",
            cost_price=10,
            sale_price=20,
            stock_qty=5,
            safety_stock=20,
            avg_cost=10,
            is_active=True,
        )
        # 产品 B：库存充足
        p_ok = Product(
            sku="WHS-OK-002",
            name="充足库存商品",
            cost_price=8,
            sale_price=15,
            stock_qty=200,
            safety_stock=10,
            avg_cost=8,
            is_active=True,
        )
        db.session.add_all([p_low, p_ok])
        db.session.flush()

        # 销售单（用于 ODS/DWD/DWS）
        so = SaleOrder(
            order_no="SO-WHS-001",
            product_id=p_low.id,
            qty=10,
            unit_price=20,
            customer="测试客户",
            platform="线下",
        )
        db.session.add(so)

        # 已确认采购单（用于 in_transit 计算，qty=5 不足以覆盖缺口，确保补货建议 > 0）
        po = PurchaseOrder(
            order_no="PO-WHS-001",
            supplier_id=supplier.id,
            product_id=p_low.id,
            qty=5,
            unit_price=12,
            status="confirmed",
        )
        db.session.add(po)

        # 库存移动（出库）
        sm = StockMove(product_id=p_low.id, qty=-10, move_type="out", ref_order="SO-WHS-001")
        db.session.add(sm)

        db.session.commit()
        return {
            "supplier_id": supplier.id,
            "p_low_id": p_low.id,
            "p_ok_id": p_ok.id,
            "so_id": so.id,
            "po_id": po.id,
        }


class TestLineageInit:
    """血缘初始化测试"""

    def test_init_lineage_creates_chain(self, app):
        """首次构造 WarehouseService 时写入 10 条基础血缘链"""
        with app.app_context():
            # 先清理（autouse fixture 已清理，但构造前确认）
            assert DataLineage.query.count() == 0
            WarehouseService()
            count = DataLineage.query.count()
            # _LINEAGE_CHAINS 有 10 条
            assert count == 10

    def test_init_lineage_idempotent(self, app):
        """重复构造 WarehouseService 不产生重复血缘（幂等）"""
        with app.app_context():
            WarehouseService()
            WarehouseService()
            WarehouseService()
            assert DataLineage.query.count() == 10


class TestFullPipeline:
    """全量 ETL 流水线测试"""

    def test_run_full_pipeline_success(self, app, seed_erp_data):
        """全量 ETL：ODS→DWD→DWS→ADS 各层均有数据"""
        with app.app_context():
            svc = WarehouseService()
            result = svc.run_full_pipeline()

            assert "ods" in result
            assert "dwd" in result
            assert "dws" in result
            assert "ads" in result
            assert "error" not in result

            # ODS：1 销售单 + 1 采购单 + 1 库存移动 = 3 行
            assert result["ods"]["rows"] == 3
            # DWD：1 销售事实 + 1 库存事实 = 2 行
            assert result["dwd"]["rows"] == 2
            # DWS：销售汇总（1 个 SKU 日）+ 库存快照（2 个产品）= 3 行
            assert result["dws"]["rows"] >= 2
            # ADS：补货建议（2 产品）+ 日报（1 条）= 3 行
            assert result["ads"]["rows"] >= 2

    def test_run_full_pipeline_invalidates_cache(self, app, seed_erp_data):
        """ETL 完成后失效 ADS 缓存，下次读取为最新数据"""
        with app.app_context():
            svc = WarehouseService()
            svc.run_full_pipeline()
            # get_ads_data 应返回最新数据
            data = svc.get_ads_data()
            assert "suggestions" in data
            assert "report" in data
            # 2 个产品的补货建议
            assert len(data["suggestions"]) == 2

    def test_run_full_pipeline_empty_erp(self, app):
        """ERP 无数据时 ETL 不报错，各层 0 行"""
        with app.app_context():
            svc = WarehouseService()
            result = svc.run_full_pipeline()
            # ODS 无数据
            assert result["ods"]["rows"] == 0
            assert "error" not in result


class TestEtlOds:
    """ODS 贴源层测试"""

    def test_ods_full_pull(self, app, seed_erp_data):
        """全量拉取：ERP 销售单/采购单/库存移动 → ODS"""
        with app.app_context():
            svc = WarehouseService()
            result = svc._etl_ods()
            assert result["rows"] == 3
            assert OdsSaleOrder.query.count() == 1
            assert OdsPurchaseOrder.query.count() == 1
            assert OdsStockMove.query.count() == 1

    def test_ods_incremental_dedup(self, app, seed_erp_data):
        """增量拉取：重复执行不产生重复 ODS 记录"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_ods()  # 第二次执行
            assert OdsSaleOrder.query.count() == 1
            assert OdsPurchaseOrder.query.count() == 1
            assert OdsStockMove.query.count() == 1

    def test_ods_etl_meta_recorded(self, app, seed_erp_data):
        """ODS ETL 后写入 EtlMeta（水位线 + 状态）"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            meta = EtlMeta.query.filter_by(layer="ODS", table_name="ods_sale_order").first()
            assert meta is not None
            assert meta.status == "success"
            assert meta.rows_processed == 1
            assert meta.last_watermark is not None


class TestEtlDwd:
    """DWD 明细层测试"""

    def test_dwd_clean_and_join(self, app, seed_erp_data):
        """DWD：ODS → DWD 关联 Product 维度，写入 product_name"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            result = svc._etl_dwd()
            assert result["rows"] == 2

            fact = DwdSalesFact.query.first()
            assert fact is not None
            assert fact.product_name == "低库存商品"
            # amount = qty * unit_price = 10 * 20 = 200
            assert float(fact.amount) == 200.0

    def test_dwd_dedup(self, app, seed_erp_data):
        """DWD 去重：重复执行不产生重复事实表记录"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_dwd()
            svc._etl_dwd()  # 第二次
            assert DwdSalesFact.query.count() == 1
            assert DwdStockFact.query.count() == 1


class TestEtlDws:
    """DWS 汇总层测试"""

    def test_dws_aggregation(self, app, seed_erp_data):
        """DWS：按 SKU + 日聚合销售，库存日快照"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_dwd()
            result = svc._etl_dws()
            # 销售汇总：1 个 SKU 日（低库存商品）
            assert result["details"]["dws_sales_sku_daily"] >= 1
            # 库存快照：2 个产品
            assert result["details"]["dws_inventory_snapshot_daily"] == 2

            sales = DwsSalesSkuDaily.query.all()
            assert len(sales) >= 1
            assert all(s.sale_qty is not None for s in sales)

    def test_dws_full_recalc_clears_old(self, app, seed_erp_data):
        """全量重算时清空旧 DWS 数据"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_dwd()
            svc._etl_dws()
            # 再跑一次全量
            svc._etl_dws()
            # 不应出现重复行（DWS 已被 delete 后重算）
            sales_count = DwsSalesSkuDaily.query.count()
            assert sales_count >= 1


class TestEtlAds:
    """ADS 应用层测试"""

    def test_ads_replenishment_suggest(self, app, seed_erp_data):
        """ADS 补货建议：低库存产品生成建议，充足产品建议为 0"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_dwd()
            svc._etl_dws()
            result = svc._etl_ads()

            assert result["details"]["ads_replenishment_suggest"] == 2
            assert result["details"]["ads_daily_ops_report"] == 1

            # 低库存产品应有补货建议
            low_suggest = AdsReplenishmentSuggest.query.filter_by(product_id=seed_erp_data["p_low_id"]).first()
            assert low_suggest is not None
            assert low_suggest.suggested_qty > 0
            assert "近7日销量" in low_suggest.reason

    def test_ads_idempotent(self, app, seed_erp_data):
        """ADS 幂等：同一天重复执行覆盖旧数据"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_dwd()
            svc._etl_dws()
            svc._etl_ads()
            svc._etl_ads()  # 第二次
            # 同一天只有 1 条日报 + 2 条补货建议
            today = date.today()
            assert AdsDailyOpsReport.query.filter_by(dt=today).count() == 1
            assert AdsReplenishmentSuggest.query.filter_by(dt=today).count() == 2

    def test_ads_daily_ops_report_content(self, app, seed_erp_data):
        """ADS 经营日报字段正确"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_dwd()
            svc._etl_dws()
            svc._etl_ads()

            report = AdsDailyOpsReport.query.filter_by(dt=date.today()).first()
            assert report is not None
            assert report.total_sales_amount is not None
            assert report.inventory_value is not None
            # 低库存产品数：低库存商品 stock_qty=5 < safety_stock=20
            assert report.low_stock_count >= 1


class TestDataQualityTests:
    """数据质量测试"""

    def test_dq_all_pass(self, app, seed_erp_data):
        """干净数据：所有 DQ 测试通过"""
        with app.app_context():
            svc = WarehouseService()
            svc.run_full_pipeline()
            # 关闭 strict 模式，仅验证测试执行
            from flask import current_app

            old = current_app.config.get("FDE_DQ_STRICT")
            current_app.config["FDE_DQ_STRICT"] = False
            try:
                result = svc.run_data_quality_tests()
            finally:
                if old is not None:
                    current_app.config["FDE_DQ_STRICT"] = old

            # 至少检查了 9 张表
            assert len(result) >= 9
            for table, tests in result.items():
                for test_name, status in tests.items():
                    assert status == "pass", f"{table}.{test_name} 应为 pass，实际 {status}"

    def test_dq_strict_mode_raises_on_failure(self, app, seed_erp_data):
        """FDE_DQ_STRICT=True 且有失败时抛 ValueError"""
        with app.app_context():
            svc = WarehouseService()
            svc.run_full_pipeline()
            # 注入坏数据：qty=0 的 ODS 库存移动（触发 range 失败）
            bad = OdsStockMove(
                product_id=seed_erp_data["p_low_id"], qty=0, move_type="in", ref_order="BAD-001", dt=date.today()
            )
            db.session.add(bad)
            db.session.commit()

            from flask import current_app

            current_app.config["FDE_DQ_STRICT"] = True
            with pytest.raises(ValueError, match="数据质量强校验失败"):
                svc.run_data_quality_tests()

    def test_dq_log_written(self, app, seed_erp_data):
        """DQ 测试结果写入 DataQualityLog"""
        with app.app_context():
            svc = WarehouseService()
            svc.run_full_pipeline()
            from flask import current_app

            current_app.config["FDE_DQ_STRICT"] = False
            svc.run_data_quality_tests()
            assert DataQualityLog.query.count() > 0


class TestBackfill:
    """历史回刷测试"""

    def test_backfill_dws_and_ads(self, app, seed_erp_data):
        """回刷指定日期范围：DWS + ADS 重算"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_dwd()
            start = date.today() - timedelta(days=2)
            end = date.today()
            result = svc.backfill(start, end)

            assert "dates" in result
            assert len(result["dates"]) == 3
            assert result["dws"] is not None
            assert len(result["ads"]) == 3

    def test_backfill_invalid_date_range(self, app, seed_erp_data):
        """start > end 时抛 ValueError"""
        with app.app_context():
            svc = WarehouseService()
            start = date.today()
            end = date.today() - timedelta(days=1)
            with pytest.raises(ValueError, match="start_date"):
                svc.backfill(start, end)

    def test_backfill_accepts_string_dates(self, app, seed_erp_data):
        """backfill 接受字符串日期格式"""
        with app.app_context():
            svc = WarehouseService()
            svc._etl_ods()
            svc._etl_dwd()
            result = svc.backfill(
                (date.today() - timedelta(days=1)).strftime("%Y-%m-%d"),
                date.today().strftime("%Y-%m-%d"),
            )
            assert len(result["dates"]) == 2


class TestQueryInterfaces:
    """查询接口测试"""

    def test_get_layer_stats(self, app, seed_erp_data):
        """get_layer_stats 返回 ODS/DWD/DWS/ADS 各层记录数"""
        with app.app_context():
            svc = WarehouseService()
            svc.run_full_pipeline()
            stats = svc.get_layer_stats()
            assert "ODS" in stats
            assert "DWD" in stats
            assert "DWS" in stats
            assert "ADS" in stats
            assert stats["ODS"]["ods_sale_order"] == 1
            assert stats["DWD"]["dwd_sales_order_di"] == 1

    def test_get_lineage(self, app, seed_erp_data):
        """get_lineage 返回血缘列表"""
        with app.app_context():
            svc = WarehouseService()
            lineage = svc.get_lineage()
            assert isinstance(lineage, list)
            assert len(lineage) >= 10
            # 每条包含 upstream/downstream/layer/transformation
            for item in lineage:
                assert "upstream" in item
                assert "downstream" in item
                assert "layer" in item
                assert "transformation" in item

    def test_get_dq_report(self, app, seed_erp_data):
        """get_dq_report 聚合最近一轮 DQ 结果"""
        with app.app_context():
            svc = WarehouseService()
            svc.run_full_pipeline()
            from flask import current_app

            current_app.config["FDE_DQ_STRICT"] = False
            svc.run_data_quality_tests()
            report = svc.get_dq_report()
            assert isinstance(report, dict)
            assert len(report) > 0
            # 每张表包含 not_null/unique/range 测试结果
            for _table, tests in report.items():
                for _test_name, info in tests.items():
                    assert info["status"] in ("pass", "fail")

    def test_get_ads_data_cached(self, app, seed_erp_data):
        """get_ads_data 返回 ADS 数据（含 suggestions + report）"""
        with app.app_context():
            svc = WarehouseService()
            svc.run_full_pipeline()
            data = svc.get_ads_data()
            assert "suggestions" in data
            assert "report" in data
            assert len(data["suggestions"]) == 2
            # report 应存在
            assert data["report"] is not None
            assert "total_sales_amount" in data["report"]
