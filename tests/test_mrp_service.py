"""MRP + BOM 服务测试

覆盖：
- 创建 BOM（头 + 行）
- MRP 计算（净需求 = 毛需求 - 现有库存，最小 0）
- BOM 树查询（含子件产品信息）
"""

from datetime import datetime, timedelta

import pytest

from extensions import db
from models.bom import BOM, BOMItem, MRPPlan, MRPRequirement
from models.erp import Product
from services.mrp_service import MRPService


@pytest.fixture(autouse=True)
def _cleanup_mrp_tables(app):
    """每个测试前后清理 BOM/MRP 表，避免数据污染（service 方法会 commit）"""
    with app.app_context():
        for model in [MRPRequirement, MRPPlan, BOMItem, BOM]:
            db.session.query(model).delete()
        # 同时清理测试中创建的临时 Product
        db.session.query(Product).filter(Product.sku.like("TEST-MRP-%")).delete()
        db.session.commit()
    yield
    with app.app_context():
        for model in [MRPRequirement, MRPPlan, BOMItem, BOM]:
            db.session.query(model).delete()
        db.session.query(Product).filter(Product.sku.like("TEST-MRP-%")).delete()
        db.session.commit()


@pytest.fixture
def seed_products(app):
    """创建测试用产品：1 个成品 + 2 个子件（带初始库存）"""
    with app.app_context():
        finished = Product(
            sku="TEST-MRP-FIN-001",
            name="成品A",
            cost_price=100,
            sale_price=200,
            stock_qty=0,
            safety_stock=0,
            avg_cost=100,
        )
        comp1 = Product(
            sku="TEST-MRP-COMP-001",
            name="子件X",
            cost_price=10,
            sale_price=15,
            stock_qty=20,  # 现有库存 20
            safety_stock=0,
            avg_cost=10,
        )
        comp2 = Product(
            sku="TEST-MRP-COMP-002",
            name="子件Y",
            cost_price=5,
            sale_price=8,
            stock_qty=50,  # 现有库存 50（足够，无净需求）
            safety_stock=0,
            avg_cost=5,
        )
        db.session.add_all([finished, comp1, comp2])
        db.session.commit()
        return {"finished_id": finished.id, "comp1_id": comp1.id, "comp2_id": comp2.id}


@pytest.mark.unit
class TestBOMCreate:
    """BOM 创建测试"""

    def test_create_bom_success(self, app, seed_products):
        """创建 BOM：生成头 + 行，状态 active"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[
                    {
                        "component_product_id": seed_products["comp1_id"],
                        "quantity": "2",
                        "scrap_rate": "0.05",
                    },
                    {
                        "component_product_id": seed_products["comp2_id"],
                        "quantity": "1",
                    },
                ],
                version="1.0",
                description="成品A 物料清单",
            )
            assert bom.id is not None
            assert bom.code.startswith("BOM-")
            assert bom.version == "1.0"
            assert bom.status == "active"
            assert bom.product_id == seed_products["finished_id"]
            # 行数
            items = BOMItem.query.filter_by(bom_id=bom.id).order_by(BOMItem.sequence).all()
            assert len(items) == 2
            # 行序号默认 10/20
            assert items[0].sequence == 10
            assert items[1].sequence == 20
            # 损耗率
            assert float(items[0].scrap_rate) == 0.05
            assert float(items[1].scrap_rate) == 0.0

    def test_create_bom_empty_items(self, app, seed_products):
        """items 为空时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="BOM 行项目不能为空"):
                svc.create_bom(product_id=seed_products["finished_id"], items=[])

    def test_create_bom_product_not_found(self, app):
        """产品不存在时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="产品不存在"):
                svc.create_bom(
                    product_id=99999,
                    items=[{"component_product_id": 1, "quantity": "1"}],
                )

    def test_create_bom_component_not_found(self, app, seed_products):
        """子件不存在时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="子件不存在"):
                svc.create_bom(
                    product_id=seed_products["finished_id"],
                    items=[{"component_product_id": 99999, "quantity": "1"}],
                )

    def test_create_bom_invalid_quantity(self, app, seed_products):
        """子件 quantity 非正时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="quantity 必须为正数"):
                svc.create_bom(
                    product_id=seed_products["finished_id"],
                    items=[{"component_product_id": seed_products["comp1_id"], "quantity": "0"}],
                )


@pytest.mark.unit
class TestMRPCalculate:
    """MRP 计算测试"""

    def test_calculate_mrp_net_requirement(self, app, seed_products):
        """MRP 计算：净需求 = 毛需求 - 现有库存，最小 0"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[
                    # 子件X：单位用量 2，无损耗，库存 20
                    {"component_product_id": seed_products["comp1_id"], "quantity": "2"},
                    # 子件Y：单位用量 1，无损耗，库存 50（足够）
                    {"component_product_id": seed_products["comp2_id"], "quantity": "1"},
                ],
            )
            # 计划生产 30 个成品 → 子件X 毛需求 60，库存 20，净需求 40
            #                       子件Y 毛需求 30，库存 50，净需求 0
            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=bom.id,
                planned_qty=30,
                planned_date=datetime.now() + timedelta(days=7),
            )
            result = svc.calculate_mrp(plan.id)
            assert result["plan_id"] == plan.id
            assert len(result["requirements"]) == 2

            # 按子件 ID 索引
            req_map = {r["component_product_id"]: r for r in result["requirements"]}
            # 子件X：毛需求 60，库存 20，净需求 40
            req_x = req_map[seed_products["comp1_id"]]
            assert req_x["required_qty"] == 60.0
            assert req_x["on_hand_qty"] == 20.0
            assert req_x["net_requirement"] == 40.0
            assert req_x["suggested_order_qty"] == 40.0

            # 子件Y：毛需求 30，库存 50，净需求 0
            req_y = req_map[seed_products["comp2_id"]]
            assert req_y["required_qty"] == 30.0
            assert req_y["on_hand_qty"] == 50.0
            assert req_y["net_requirement"] == 0.0
            assert req_y["suggested_order_qty"] == 0.0

            # 计划状态升级为 calculated
            plan_db = db.session.get(MRPPlan, plan.id)
            assert plan_db.status == "calculated"

            # MRPRequirement 已落库
            reqs = MRPRequirement.query.filter_by(plan_id=plan.id).all()
            assert len(reqs) == 2

    def test_calculate_mrp_with_scrap_rate(self, app, seed_products):
        """含损耗率的 MRP 计算：毛需求 = quantity * (1 + scrap_rate) * scale"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[
                    # 子件X：单位用量 2，损耗率 10%（毛需求 = 2 * 1.1 * 30 = 66）
                    {"component_product_id": seed_products["comp1_id"], "quantity": "2", "scrap_rate": "0.10"},
                ],
            )
            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=bom.id,
                planned_qty=30,
                planned_date=datetime.now() + timedelta(days=7),
            )
            result = svc.calculate_mrp(plan.id)
            req_x = result["requirements"][0]
            # 2 * 1.1 * 30 = 66
            assert req_x["required_qty"] == 66.0
            # 净需求 = 66 - 20 = 46
            assert req_x["net_requirement"] == 46.0
            assert req_x["suggested_order_qty"] == 46.0

    def test_calculate_mrp_plan_not_found(self, app):
        """计划不存在时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="MRP 计划不存在"):
                svc.calculate_mrp(99999)

    def test_calculate_mrp_no_bom(self, app, seed_products):
        """计划未关联 BOM 时抛 ValueError"""
        with app.app_context():
            # 直接造一条无 bom_id 的计划
            plan = MRPPlan(
                plan_no="MRP-TEST-NOBOM",
                product_id=seed_products["finished_id"],
                bom_id=None,
                planned_qty=10,
                planned_date=datetime.now(),
                status="draft",
            )
            db.session.add(plan)
            db.session.commit()
            svc = MRPService()
            with pytest.raises(ValueError, match="未关联 BOM"):
                svc.calculate_mrp(plan.id)

    def test_calculate_mrp_recalc_clears_old(self, app, seed_products):
        """重复计算时清空旧需求（避免重复）"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[{"component_product_id": seed_products["comp1_id"], "quantity": "2"}],
            )
            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=bom.id,
                planned_qty=10,
                planned_date=datetime.now() + timedelta(days=7),
            )
            svc.calculate_mrp(plan.id)
            assert MRPRequirement.query.filter_by(plan_id=plan.id).count() == 1

            # 再次计算（不同产量）
            plan.planned_qty = 20
            db.session.commit()
            svc.calculate_mrp(plan.id)
            # 仍只有 1 条（不是 2 条）
            assert MRPRequirement.query.filter_by(plan_id=plan.id).count() == 1


@pytest.mark.unit
class TestBOMTree:
    """BOM 树查询测试"""

    def test_get_bom_tree(self, app, seed_products):
        """查询 BOM 树：返回头信息 + 行列表（含子件 SKU/name）"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[
                    {
                        "component_product_id": seed_products["comp1_id"],
                        "quantity": "2",
                        "scrap_rate": "0.05",
                        "notes": "损耗件",
                    },
                    {
                        "component_product_id": seed_products["comp2_id"],
                        "quantity": "1",
                    },
                ],
                description="测试 BOM 树",
            )
            tree = svc.get_bom_tree(bom.id)
            assert tree["bom"]["id"] == bom.id
            assert tree["bom"]["code"] == bom.code
            assert tree["bom"]["product_id"] == seed_products["finished_id"]
            assert tree["bom"]["product_sku"] == "TEST-MRP-FIN-001"
            assert tree["bom"]["product_name"] == "成品A"
            assert tree["bom"]["description"] == "测试 BOM 树"

            assert len(tree["items"]) == 2
            # 子件信息已预取
            assert tree["items"][0]["component_sku"] == "TEST-MRP-COMP-001"
            assert tree["items"][0]["component_name"] == "子件X"
            assert float(tree["items"][0]["quantity"]) == 2.0
            assert float(tree["items"][0]["scrap_rate"]) == 0.05
            assert tree["items"][0]["notes"] == "损耗件"

            assert tree["items"][1]["component_sku"] == "TEST-MRP-COMP-002"

    def test_get_bom_tree_not_found(self, app):
        """BOM 不存在时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="BOM 不存在"):
                svc.get_bom_tree(99999)
