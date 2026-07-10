"""
FDE 数仓服务层（WarehouseService）
===============================
模拟 Airflow 调度 + dbt 模型转换 + ClickHouse 分层加工的 ETL 流水线。
分层职责：ODS 贴源 → DWD 明细 → DWS 汇总 → ADS 应用。
核心原则：每层只做一件事，下游不直接读 ODS，指标在 ADS 统一口径。

能力清单（FDE 完整度 100%）：
  1. run_full_pipeline()  全量 ETL：ODS→DWD→DWS→ADS
  2. _etl_ods()           增量拉取（水位线 + etl_batch）
  3. _etl_dwd()           清洗、关联维度、去重、拆事实表
  4. _etl_dws()           按 SKU+日聚合销售、日快照库存
  5. _etl_ads()           补货建议源数据 + 经营日报
  6. run_data_quality_tests()  not_null/unique/range 数据质量测试
  7. _record_lineage()/_init_lineage()  血缘追踪
  8. _update_etl_meta()   ETL 元数据与水位线
  9. backfill()           历史回刷 DWS+ADS
 10. get_layer_stats()/get_ads_data()/get_lineage()/get_dq_report()  查询接口
 11. @cache.memoize + 失效  ADS 缓存

依赖：extensions.db / extensions.cache、models.warehouse、models.erp、
     services.rpa_service（延迟导入，避免循环依赖）。
"""

import logging
from datetime import date, datetime, timedelta

from flask import current_app
from sqlalchemy import func, text

from extensions import cache, db
from models.erp import Product, PurchaseOrder, SaleOrder, StockMove
from models.warehouse import *  # noqa: F401,F403  ODS/DWD/DWS/ADS 模型及治理表

logger = logging.getLogger(__name__)


class WarehouseService:
    """数仓 ETL 流水线服务（实例方法，__init__ 可注入 db_session 便于测试）"""

    # 基础血缘链：(上游表, 下游表, 层, 转换逻辑)
    _LINEAGE_CHAINS = [
        ("erp_sale_order", "ods_sale_order", "ODS", "贴源抽取"),
        ("erp_purchase_order", "ods_purchase_order", "ODS", "贴源抽取"),
        ("erp_stock_move", "ods_stock_move", "ODS", "贴源抽取"),
        ("ods_sale_order", "dwd_sales_order_di", "DWD", "清洗+关联维度+去重"),
        ("ods_stock_move", "dwd_stock_move_di", "DWD", "清洗+关联维度+去重"),
        ("dwd_sales_order_di", "dws_sales_sku_daily", "DWS", "按SKU+日聚合"),
        ("dwd_stock_move_di", "dws_inventory_snapshot_daily", "DWS", "日快照"),
        ("dws_sales_sku_daily", "ads_replenishment_suggest", "ADS", "补货测算"),
        ("dws_inventory_snapshot_daily", "ads_replenishment_suggest", "ADS", "库存参考"),
        ("dws_sales_sku_daily", "ads_daily_ops_report", "ADS", "经营日报"),
    ]

    def __init__(self, db_session=None):
        self.session = db_session or db.session
        # 初始化基础血缘（幂等，失败不阻断构造）
        try:
            self._init_lineage()
        except Exception as e:  # pragma: no cover - 构造期容错
            logger.debug("init lineage skipped: %s", e)

    # ==================== 配置读取 ====================

    def _flag(self, key, default):
        """从 Flask config 读取开关；无应用上下文时回退默认值"""
        try:
            if current_app:
                return current_app.config.get(key, default)
        except Exception:
            pass
        return default

    # ==================== 公共接口 ====================

    def run_full_pipeline(self):
        """全量 ETL：ODS → DWD → DWS → ADS，返回每层处理记录数"""
        results = {}
        try:
            results["ods"] = self._etl_ods()
            results["dwd"] = self._etl_dwd()
            results["dws"] = self._etl_dws()
            results["ads"] = self._etl_ads()
        except Exception as e:
            logger.exception("ETL pipeline failed: %s", e)
            results["error"] = str(e)
        # 失效 ADS 缓存（无论成功失败，确保下次读取为最新）
        self._invalidate_ads_cache()
        return results

    def run_data_quality_tests(self):
        """对每层跑数据质量测试（not_null/unique/range），结果写 DataQualityLog。
        返回 {table: {test: status}}；FDE_DQ_STRICT=True 且有失败时抛 ValueError。
        """
        # (模型, 表名, not_null字段, 唯一字段或None, range失败条件或None)
        tests = [
            (OdsSaleOrder, "ods_sale_order", ["order_no", "product_id", "qty"], "order_no", "qty <= 0"),
            (OdsPurchaseOrder, "ods_purchase_order", ["order_no", "product_id", "qty"], "order_no", "qty <= 0"),
            (OdsStockMove, "ods_stock_move", ["product_id", "qty"], None, "qty = 0"),
            (DwdSalesFact, "dwd_sales_order_di", ["order_no", "product_id", "qty", "amount"], "order_no", "qty <= 0"),
            (DwdStockFact, "dwd_stock_move_di", ["product_id", "qty"], None, "qty = 0"),
            (DwsSalesSkuDaily, "dws_sales_sku_daily", ["product_id", "dt", "sale_qty"], None, "sale_qty < 0"),
            (
                DwsInventoryDaily,
                "dws_inventory_snapshot_daily",
                ["product_id", "dt", "stock_qty"],
                None,
                "stock_qty < 0",
            ),
            (
                AdsReplenishmentSuggest,
                "ads_replenishment_suggest",
                ["product_id", "suggested_qty"],
                None,
                "suggested_qty < 0",
            ),
            (AdsDailyOpsReport, "ads_daily_ops_report", ["dt"], "dt", None),
        ]
        results = {}
        strict = self._flag("FDE_DQ_STRICT", True)
        overall_fail = 0
        for model, table, nn_cols, uniq_col, range_fail in tests:
            table_result = {}
            # ---- not_null ----
            for col in nn_cols:
                col_obj = getattr(model, col)
                failures = self.session.query(func.count(model.id)).filter(col_obj.is_(None)).scalar() or 0
                status = "pass" if failures == 0 else "fail"
                table_result[f"not_null:{col}"] = status
                self._log_dq(table, f"not_null:{col}", col, status, failures)
                overall_fail += failures
            # ---- unique ----
            if uniq_col:
                col_obj = getattr(model, uniq_col)
                total = self.session.query(func.count(col_obj)).scalar() or 0
                distinct = self.session.query(func.count(func.distinct(col_obj))).scalar() or 0
                failures = max(0, total - distinct)
                status = "pass" if failures == 0 else "fail"
                table_result["unique"] = status
                self._log_dq(table, "unique", uniq_col, status, failures)
                overall_fail += failures
            # ---- range（range_fail 即"失败条件"，直接统计命中行数）----
            if range_fail:
                col_name = range_fail.split()[0]
                fail_count = self.session.query(func.count(model.id)).filter(text(range_fail)).scalar() or 0
                status = "pass" if fail_count == 0 else "fail"
                table_result["range"] = status
                self._log_dq(table, "range", col_name, status, fail_count)
                overall_fail += fail_count
            results[table] = table_result

        try:
            self.session.commit()
        except Exception:
            self.session.rollback()

        if strict and overall_fail > 0:
            logger.warning("DQ strict mode: %s failure rows detected", overall_fail)
            raise ValueError(f"数据质量强校验失败：共 {overall_fail} 条问题记录")
        logger.info("DQ tests done: %s tables checked", len(results))
        return results

    def backfill(self, start_date, end_date):
        """重跑指定日期范围 [start_date, end_date] 的 DWS + ADS"""
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date")

        dates = []
        d = start_date
        while d <= end_date:
            dates.append(d)
            d += timedelta(days=1)

        results = {"dates": [str(x) for x in dates], "dws": None, "ads": []}
        try:
            results["dws"] = self._etl_dws(target_dates=dates)
            for d in dates:
                results["ads"].append(self._etl_ads(target_date=d))
        except Exception as e:
            logger.exception("backfill failed: %s", e)
            results["error"] = str(e)
        self._invalidate_ads_cache()
        return results

    @cache.memoize(timeout=1800)
    def get_ads_data(self):
        """获取 ADS 层数据供 AIGC 和前端使用（缓存 30 分钟）"""
        today = date.today()
        suggest = self.session.query(AdsReplenishmentSuggest).filter_by(dt=today).all()
        report = self.session.query(AdsDailyOpsReport).filter_by(dt=today).first()
        return {
            "suggestions": [
                {
                    "id": s.id,
                    "product_id": s.product_id,
                    "product_name": s.product_name,
                    "recent_7d_sales": s.recent_7d_sales,
                    "current_stock": s.current_stock,
                    "in_transit": s.in_transit,
                    "suggested_qty": s.suggested_qty,
                    "suggested_supplier_id": s.suggested_supplier_id,
                    "suggested_supplier_name": s.suggested_supplier_name,
                    "reason": s.reason,
                }
                for s in suggest
            ],
            "report": (
                {
                    "dt": str(report.dt) if report else "-",
                    "total_sales_amount": float(report.total_sales_amount) if report else 0,
                    "total_purchase_amount": float(report.total_purchase_amount) if report else 0,
                    "total_sale_qty": report.total_sale_qty if report else 0,
                    "inventory_value": float(report.inventory_value) if report else 0,
                    "top_sku": report.top_sku if report else "-",
                    "low_stock_count": report.low_stock_count if report else 0,
                }
                if report
                else None
            ),
        }

    def get_layer_stats(self):
        """各层记录数统计（数仓监控）"""
        return {
            "ODS": {
                "ods_sale_order": self.session.query(OdsSaleOrder).count(),
                "ods_purchase_order": self.session.query(OdsPurchaseOrder).count(),
                "ods_stock_move": self.session.query(OdsStockMove).count(),
            },
            "DWD": {
                "dwd_sales_order_di": self.session.query(DwdSalesFact).count(),
                "dwd_stock_move_di": self.session.query(DwdStockFact).count(),
            },
            "DWS": {
                "dws_sales_sku_daily": self.session.query(DwsSalesSkuDaily).count(),
                "dws_inventory_snapshot_daily": self.session.query(DwsInventoryDaily).count(),
            },
            "ADS": {
                "ads_replenishment_suggest": self.session.query(AdsReplenishmentSuggest)
                .filter_by(dt=date.today())
                .count(),
                "ads_daily_ops_report": self.session.query(AdsDailyOpsReport).count(),
            },
        }

    def get_lineage(self):
        """返回全量数据血缘（上游 → 下游）"""
        rows = self.session.query(DataLineage).order_by(DataLineage.id.asc()).all()
        return [
            {
                "upstream": r.upstream_table,
                "downstream": r.downstream_table,
                "layer": r.layer,
                "transformation": r.transformation,
            }
            for r in rows
        ]

    def get_dq_report(self):
        """按表聚合最近一轮数据质量测试结果（每表每测试取最新一条）"""
        rows = self.session.query(DataQualityLog).order_by(DataQualityLog.checked_at.desc()).all()
        latest = {}
        for r in rows:
            key = (r.table_name, r.test_name)
            if key not in latest:
                latest[key] = r
        report = {}
        for (table, test_name), r in latest.items():
            report.setdefault(table, {})[test_name] = {
                "status": r.status,
                "failures": r.failures,
                "column": r.column_name,
                "checked_at": str(r.checked_at) if r.checked_at else None,
            }
        return report

    # ==================== ETL 各层 ====================

    def _etl_ods(self):
        """ODS 贴源层：从 ERP 抽取单据原样落地。
        FDE_INCREMENTAL=1 时基于 EtlMeta.last_watermark 增量拉取，每次更新水位线。
        生成 etl_batch 批次号。
        """
        incremental = self._flag("FDE_INCREMENTAL", True)
        batch_no = f'batch-{datetime.now().strftime("%Y%m%d%H%M%S")}'
        meta_updates = []  # (table, watermark, rows)
        total = 0
        try:
            # ---- 销售单 ----
            wm = self._get_watermark("ODS", "ods_sale_order")
            q = self.session.query(SaleOrder)
            if incremental and wm is not None:
                q = q.filter(SaleOrder.created_at > wm)
            existing_nos = {r[0] for r in self.session.query(OdsSaleOrder.order_no).all()}
            n, new_wm = 0, wm
            for so in q.order_by(SaleOrder.created_at.asc()).all():
                if new_wm is None or so.created_at > new_wm:
                    new_wm = so.created_at
                if so.order_no in existing_nos:
                    continue
                self.session.add(
                    OdsSaleOrder(
                        order_no=so.order_no,
                        product_id=so.product_id,
                        qty=so.qty,
                        unit_price=so.unit_price,
                        customer=so.customer,
                        platform=so.platform,
                        dt=so.created_at.date(),
                        etl_batch=batch_no,
                    )
                )
                n += 1
            total += n
            meta_updates.append(("ods_sale_order", new_wm, n))

            # ---- 采购单 ----
            wm = self._get_watermark("ODS", "ods_purchase_order")
            q = self.session.query(PurchaseOrder)
            if incremental and wm is not None:
                q = q.filter(PurchaseOrder.created_at > wm)
            existing_nos = {r[0] for r in self.session.query(OdsPurchaseOrder.order_no).all()}
            n, new_wm = 0, wm
            for po in q.order_by(PurchaseOrder.created_at.asc()).all():
                if new_wm is None or po.created_at > new_wm:
                    new_wm = po.created_at
                if po.order_no in existing_nos:
                    continue
                self.session.add(
                    OdsPurchaseOrder(
                        order_no=po.order_no,
                        supplier_id=po.supplier_id,
                        product_id=po.product_id,
                        qty=po.qty,
                        unit_price=po.unit_price,
                        status=po.status,
                        dt=po.created_at.date(),
                        etl_batch=batch_no,
                    )
                )
                n += 1
            total += n
            meta_updates.append(("ods_purchase_order", new_wm, n))

            # ---- 库存移动 ----
            wm = self._get_watermark("ODS", "ods_stock_move")
            q = self.session.query(StockMove)
            if incremental and wm is not None:
                q = q.filter(StockMove.created_at > wm)
            existing_keys = {
                (r[0], r[1], r[2], r[3], r[4])
                for r in self.session.query(
                    OdsStockMove.ref_order,
                    OdsStockMove.product_id,
                    OdsStockMove.qty,
                    OdsStockMove.move_type,
                    OdsStockMove.dt,
                ).all()
            }
            n, new_wm = 0, wm
            for sm in q.order_by(StockMove.created_at.asc()).all():
                if new_wm is None or sm.created_at > new_wm:
                    new_wm = sm.created_at
                key = (sm.ref_order, sm.product_id, sm.qty, sm.move_type, sm.created_at.date())
                if key in existing_keys:
                    continue
                self.session.add(
                    OdsStockMove(
                        product_id=sm.product_id,
                        qty=sm.qty,
                        move_type=sm.move_type,
                        ref_order=sm.ref_order,
                        dt=sm.created_at.date(),
                        etl_batch=batch_no,
                    )
                )
                n += 1
            total += n
            meta_updates.append(("ods_stock_move", new_wm, n))

            self.session.commit()
        except Exception as e:
            self.session.rollback()
            logger.exception("ODS ETL failed: %s", e)
            for table, _, _ in meta_updates:
                self._update_etl_meta("ODS", table, None, 0, "failed", str(e))
            raise

        for table, wm, n in meta_updates:
            self._update_etl_meta("ODS", table, wm, n, "success")
        self._record_lineage("erp_sale_order", "ods_sale_order", "ODS", "贴源抽取")
        self._record_lineage("erp_purchase_order", "ods_purchase_order", "ODS", "贴源抽取")
        self._record_lineage("erp_stock_move", "ods_stock_move", "ODS", "贴源抽取")
        logger.info("ODS ETL done: %s rows (batch=%s, incremental=%s)", total, batch_no, incremental)
        return {"rows": total, "batch": batch_no, "incremental": incremental}

    def _etl_dwd(self):
        """DWD 明细层：ODS → DWD，关联维度表（Product）、去重、拆销售/库存事实表"""
        details = {}
        total = 0
        try:
            products = {p.id: p.name for p in self.session.query(Product).all()}

            # ---- 销售事实表（按 order_no 去重）----
            existing_sale = {r[0] for r in self.session.query(DwdSalesFact.order_no).all()}
            n = 0
            for ods in self.session.query(OdsSaleOrder).all():
                if ods.order_no in existing_sale:
                    continue
                self.session.add(
                    DwdSalesFact(
                        order_no=ods.order_no,
                        product_id=ods.product_id,
                        product_name=products.get(ods.product_id, "-"),
                        qty=ods.qty,
                        amount=float(ods.qty or 0) * float(ods.unit_price or 0),
                        customer=ods.customer,
                        platform=ods.platform,
                        dt=ods.dt,
                    )
                )
                n += 1
            total += n
            details["dwd_sales_order_di"] = n

            # ---- 库存移动事实表（按 ref_order+dt+move_type+product_id 去重）----
            existing_stock = {
                (r[0], r[1], r[2], r[3])
                for r in self.session.query(
                    DwdStockFact.ref_order, DwdStockFact.dt, DwdStockFact.move_type, DwdStockFact.product_id
                ).all()
            }
            n = 0
            for ods in self.session.query(OdsStockMove).all():
                key = (ods.ref_order, ods.dt, ods.move_type, ods.product_id)
                if key in existing_stock:
                    continue
                self.session.add(
                    DwdStockFact(
                        product_id=ods.product_id,
                        product_name=products.get(ods.product_id, "-"),
                        qty=ods.qty,
                        move_type=ods.move_type,
                        ref_order=ods.ref_order,
                        dt=ods.dt,
                    )
                )
                n += 1
            total += n
            details["dwd_stock_move_di"] = n

            self.session.commit()
        except Exception as e:
            self.session.rollback()
            logger.exception("DWD ETL failed: %s", e)
            self._update_etl_meta("DWD", "dwd_sales_order_di", None, 0, "failed", str(e))
            self._update_etl_meta("DWD", "dwd_stock_move_di", None, 0, "failed", str(e))
            raise

        self._update_etl_meta("DWD", "dwd_sales_order_di", datetime.now(), details["dwd_sales_order_di"], "success")
        self._update_etl_meta("DWD", "dwd_stock_move_di", datetime.now(), details["dwd_stock_move_di"], "success")
        self._record_lineage("ods_sale_order", "dwd_sales_order_di", "DWD", "清洗+关联维度+去重")
        self._record_lineage("ods_stock_move", "dwd_stock_move_di", "DWD", "清洗+关联维度+去重")
        logger.info("DWD ETL done: %s rows", total)
        return {"rows": total, "details": details}

    def _etl_dws(self, target_dates=None):
        """DWS 汇总层：DWD → DWS。
        target_dates 为 None 时全量重算；给定日期列表则只重算对应日期（用于 backfill）。
        注意：SQLite 原生 SQL 返回的 dt 可能是字符串，统一转为 date 对象。
        """
        details = {}
        try:
            if target_dates is None:
                # 全量重算：清空 DWS
                self.session.query(DwsSalesSkuDaily).delete(synchronize_session=False)
                self.session.query(DwsInventoryDaily).delete(synchronize_session=False)
                date_filter = None
            else:
                for d in target_dates:
                    self.session.query(DwsSalesSkuDaily).filter_by(dt=d).delete(synchronize_session=False)
                    self.session.query(DwsInventoryDaily).filter_by(dt=d).delete(synchronize_session=False)
                date_filter = set(target_dates)

            # ---- SKU 日销售汇总（原生 SQL 聚合）----
            sales_sql = text("""
                SELECT product_id, product_name, dt,
                       SUM(qty) AS sale_qty, SUM(amount) AS sale_amount
                FROM dwd_sales_order_di
                GROUP BY product_id, product_name, dt
            """)
            sales_n = 0
            for row in self.session.execute(sales_sql):
                dt_val = row[2]
                # SQLite 原生 SQL 返回 dt 可能为字符串，统一转 date
                if isinstance(dt_val, str):
                    dt_val = datetime.strptime(dt_val, "%Y-%m-%d").date()
                if date_filter is not None and dt_val not in date_filter:
                    continue
                self.session.add(
                    DwsSalesSkuDaily(
                        product_id=row[0],
                        product_name=row[1],
                        dt=dt_val,
                        sale_qty=int(row[3] or 0),
                        sale_amount=float(row[4] or 0),
                    )
                )
                sales_n += 1
            details["dws_sales_sku_daily"] = sales_n

            # ---- 库存日快照（用当前库存近似，真实场景按日结算）----
            if date_filter is not None:
                snapshot_dates = date_filter
            else:
                snapshot_dates = {date.today()}
            inv_n = 0
            for d in snapshot_dates:
                for p in self.session.query(Product).all():
                    self.session.add(
                        DwsInventoryDaily(
                            product_id=p.id,
                            product_name=p.name,
                            dt=d,
                            stock_qty=p.stock_qty,
                        )
                    )
                    inv_n += 1
            details["dws_inventory_snapshot_daily"] = inv_n

            self.session.commit()
        except Exception as e:
            self.session.rollback()
            logger.exception("DWS ETL failed: %s", e)
            self._update_etl_meta("DWS", "dws_sales_sku_daily", None, 0, "failed", str(e))
            self._update_etl_meta("DWS", "dws_inventory_snapshot_daily", None, 0, "failed", str(e))
            raise

        self._update_etl_meta("DWS", "dws_sales_sku_daily", datetime.now(), details["dws_sales_sku_daily"], "success")
        self._update_etl_meta(
            "DWS", "dws_inventory_snapshot_daily", datetime.now(), details["dws_inventory_snapshot_daily"], "success"
        )
        self._record_lineage("dwd_sales_order_di", "dws_sales_sku_daily", "DWS", "按SKU+日聚合")
        self._record_lineage("dwd_stock_move_di", "dws_inventory_snapshot_daily", "DWS", "日快照")
        logger.info("DWS ETL done: %s", details)
        return {"rows": details["dws_sales_sku_daily"] + details["dws_inventory_snapshot_daily"], "details": details}

    def _etl_ads(self, target_date=None):
        """ADS 应用层：DWS → ADS。
        补货建议 = 安全库存 + 日均销量*交期 - 当前库存 - 在途
        经营日报 = 近7日销售/采购/库存指标汇总。
        target_date 默认今天；backfill 时按指定日期生成。
        """
        target_date = target_date or date.today()
        week_ago = target_date - timedelta(days=7)
        details = {}
        try:
            # 清空目标日期 ADS 重算（幂等）
            self.session.query(AdsReplenishmentSuggest).filter_by(dt=target_date).delete(synchronize_session=False)
            self.session.query(AdsDailyOpsReport).filter_by(dt=target_date).delete(synchronize_session=False)

            # 延迟导入 RPAService 避免循环依赖
            try:
                from services.rpa_service import RPAService
            except Exception:
                RPAService = None

            products = self.session.query(Product).all()
            rep_n = 0
            for p in products:
                # 近7日销量
                recent_sales = (
                    self.session.query(func.coalesce(func.sum(DwsSalesSkuDaily.sale_qty), 0))
                    .filter(
                        DwsSalesSkuDaily.product_id == p.id,
                        DwsSalesSkuDaily.dt >= week_ago,
                        DwsSalesSkuDaily.dt <= target_date,
                    )
                    .scalar()
                    or 0
                )

                # 在途采购量（已确认未到货）
                in_transit = (
                    self.session.query(func.coalesce(func.sum(PurchaseOrder.qty), 0))
                    .filter(
                        PurchaseOrder.product_id == p.id,
                        PurchaseOrder.status == "confirmed",
                    )
                    .scalar()
                    or 0
                )

                avg_daily = float(recent_sales) / 7.0 if recent_sales else 0.0

                # 最优供应商（决定交期与建议供应商）；RPAService.get_best_quote 为实例方法，需实例化调用
                best = None
                if RPAService is not None:
                    try:
                        best = RPAService(self.session).get_best_quote(p.id)
                    except Exception as e:
                        logger.warning("RPA get_best_quote failed for product %s: %s", p.id, e)
                lead_days = best[1].lead_days if best else 5
                suggested = max(0, int(p.safety_stock + avg_daily * lead_days - p.stock_qty - in_transit))
                sup_id = best[1].id if best else None
                sup_name = best[1].name if best else "无可用供应商"

                reason = (
                    f"近7日销量{recent_sales}件(日均{avg_daily:.1f})，当前库存{p.stock_qty}件，"
                    f"在途{in_transit}件，安全库存{p.safety_stock}件，"
                    f"交期{lead_days}天，建议补货{suggested}件。"
                )

                self.session.add(
                    AdsReplenishmentSuggest(
                        product_id=p.id,
                        product_name=p.name,
                        recent_7d_sales=int(recent_sales),
                        current_stock=p.stock_qty,
                        in_transit=int(in_transit),
                        suggested_qty=suggested,
                        suggested_supplier_id=sup_id,
                        suggested_supplier_name=sup_name,
                        reason=reason,
                        dt=target_date,
                    )
                )
                rep_n += 1
            details["ads_replenishment_suggest"] = rep_n

            # ---- 经营日报 ----
            total_sales = (
                self.session.query(func.coalesce(func.sum(DwsSalesSkuDaily.sale_amount), 0))
                .filter(
                    DwsSalesSkuDaily.dt >= week_ago,
                    DwsSalesSkuDaily.dt <= target_date,
                )
                .scalar()
                or 0
            )

            total_qty = (
                self.session.query(func.coalesce(func.sum(DwsSalesSkuDaily.sale_qty), 0))
                .filter(
                    DwsSalesSkuDaily.dt >= week_ago,
                    DwsSalesSkuDaily.dt <= target_date,
                )
                .scalar()
                or 0
            )

            total_purchase = (
                self.session.query(func.coalesce(func.sum(PurchaseOrder.qty * PurchaseOrder.unit_price), 0))
                .filter(PurchaseOrder.status.in_(["confirmed", "received"]))
                .scalar()
                or 0
            )

            inventory_value = sum(float(p.cost_price or 0) * (p.stock_qty or 0) for p in products)

            top = (
                self.session.query(
                    DwsSalesSkuDaily.product_name,
                    func.sum(DwsSalesSkuDaily.sale_qty).label("total"),
                )
                .filter(
                    DwsSalesSkuDaily.dt >= week_ago,
                    DwsSalesSkuDaily.dt <= target_date,
                )
                .group_by(DwsSalesSkuDaily.product_name)
                .order_by(text("total DESC"))
                .first()
            )
            top_sku = top[0] if top else "-"

            low_count = sum(1 for p in products if (p.stock_qty or 0) < (p.safety_stock or 0))

            self.session.add(
                AdsDailyOpsReport(
                    dt=target_date,
                    total_sales_amount=float(total_sales),
                    total_purchase_amount=float(total_purchase),
                    total_sale_qty=int(total_qty),
                    inventory_value=float(inventory_value),
                    top_sku=top_sku,
                    low_stock_count=low_count,
                )
            )
            details["ads_daily_ops_report"] = 1

            self.session.commit()
        except Exception as e:
            self.session.rollback()
            logger.exception("ADS ETL failed: %s", e)
            self._update_etl_meta("ADS", "ads_replenishment_suggest", None, 0, "failed", str(e))
            self._update_etl_meta("ADS", "ads_daily_ops_report", None, 0, "failed", str(e))
            raise

        self._update_etl_meta(
            "ADS", "ads_replenishment_suggest", datetime.now(), details["ads_replenishment_suggest"], "success"
        )
        self._update_etl_meta("ADS", "ads_daily_ops_report", datetime.now(), details["ads_daily_ops_report"], "success")
        self._record_lineage("dws_sales_sku_daily", "ads_replenishment_suggest", "ADS", "补货测算")
        self._record_lineage("dws_inventory_snapshot_daily", "ads_replenishment_suggest", "ADS", "库存参考")
        self._record_lineage("dws_sales_sku_daily", "ads_daily_ops_report", "ADS", "经营日报")
        logger.info("ADS ETL done for %s: %s", target_date, details)
        return {
            "rows": details["ads_replenishment_suggest"] + details["ads_daily_ops_report"],
            "details": details,
            "dt": str(target_date),
        }

    # ==================== 治理：血缘 / 元数据 / 数据质量 ====================

    def _record_lineage(self, upstream, downstream, layer, transformation):
        """记录上游→下游表血缘（幂等：已存在则跳过）"""
        exists = self.session.query(DataLineage).filter_by(upstream_table=upstream, downstream_table=downstream).first()
        if exists:
            return
        self.session.add(
            DataLineage(
                upstream_table=upstream,
                downstream_table=downstream,
                layer=layer,
                transformation=transformation,
            )
        )
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()

    def _init_lineage(self):
        """初始化基础血缘链 ODS→DWD→DWS→ADS（幂等批量写入）"""
        existing = {
            (r[0], r[1]) for r in self.session.query(DataLineage.upstream_table, DataLineage.downstream_table).all()
        }
        added = False
        for up, down, layer, desc in self._LINEAGE_CHAINS:
            if (up, down) in existing:
                continue
            self.session.add(
                DataLineage(
                    upstream_table=up,
                    downstream_table=down,
                    layer=layer,
                    transformation=desc,
                )
            )
            added = True
        if added:
            self.session.commit()

    def _get_watermark(self, layer, table):
        """读取某表的增量水位线"""
        meta = self.session.query(EtlMeta).filter_by(layer=layer, table_name=table).first()
        return meta.last_watermark if meta else None

    def _update_etl_meta(self, layer, table, watermark, rows, status, error=None):
        """更新 ETL 元数据（水位线、行数、状态）；status=success 时记录 last_success_at"""
        meta = self.session.query(EtlMeta).filter_by(layer=layer, table_name=table).first()
        now = datetime.now()
        if meta is None:
            meta = EtlMeta(layer=layer, table_name=table)
            self.session.add(meta)
        meta.last_run_at = now
        meta.last_watermark = watermark
        meta.rows_processed = int(rows or 0)
        meta.status = status
        meta.error_msg = error
        if status == "success":
            meta.last_success_at = now
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def _log_dq(self, table, test_name, column, status, failures):
        """写一条数据质量测试日志"""
        self.session.add(
            DataQualityLog(
                table_name=table,
                test_name=test_name,
                column_name=column,
                status=status,
                failures=int(failures or 0),
                detail=f"{test_name} on {table}.{column}: {failures} failures",
                checked_at=datetime.now(),
            )
        )

    def _invalidate_ads_cache(self):
        """失效 get_ads_data 缓存"""
        try:
            cache.delete_memoized(WarehouseService.get_ads_data)
        except Exception as e:  # pragma: no cover - 缓存不可用时容错
            logger.debug("invalidate ads cache skipped: %s", e)
