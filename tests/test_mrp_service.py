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


@pytest.mark.unit
class TestMRPMultiLevelBOM:
    """多级 BOM 递归展开测试"""

    def test_multi_level_bom_expansion(self, app, seed_products):
        """多级 BOM：成品→半成品→原料，递归展开到叶子节点"""
        with app.app_context():
            svc = MRPService()
            # 新建半成品（由 comp1 组成），半成品再作为成品的子件
            sub_assembly = Product(
                sku="TEST-MRP-SUB-001",
                name="半成品Z",
                cost_price=30,
                sale_price=50,
                stock_qty=10,
                safety_stock=0,
                avg_cost=30,
            )
            db.session.add(sub_assembly)
            db.session.commit()
            sub_id = sub_assembly.id

            # 半成品的 BOM：1 个半成品需要 3 个 comp1
            svc.create_bom(
                product_id=sub_id,
                items=[{"component_product_id": seed_products["comp1_id"], "quantity": "3"}],
            )

            # 成品的 BOM：1 个成品需要 2 个半成品
            top_bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[{"component_product_id": sub_id, "quantity": "2"}],
            )

            # 计划生产 10 个成品 → 需要 20 个半成品 → 需要 60 个 comp1
            # comp1 库存 20 → 净需求 40
            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=top_bom.id,
                planned_qty=10,
                planned_date=datetime.now() + timedelta(days=7),
            )
            result = svc.calculate_mrp(plan.id)

            # 应只展开到叶子节点 comp1（半成品有 BOM 故不作为需求出现）
            req_map = {r["component_product_id"]: r for r in result["requirements"]}
            assert seed_products["comp1_id"] in req_map
            # 10 成品 * 2 半成品/成品 * 3 comp1/半成品 = 60
            assert req_map[seed_products["comp1_id"]]["required_qty"] == 60.0
            assert req_map[seed_products["comp1_id"]]["on_hand_qty"] == 20.0
            assert req_map[seed_products["comp1_id"]]["net_requirement"] == 40.0
            # 半成品不应作为叶子需求（已被展开）
            assert sub_id not in req_map

    def test_bom_cycle_detection(self, app, seed_products):
        """BOM 环路检测：组件自引用时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            # 构造环路：finished 的 BOM 包含 comp1，comp1 的 BOM 又包含 finished
            bom_top = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[{"component_product_id": seed_products["comp1_id"], "quantity": "1"}],
            )
            # comp1 的 BOM 引用 finished（形成环）
            svc.create_bom(
                product_id=seed_products["comp1_id"],
                items=[{"component_product_id": seed_products["finished_id"], "quantity": "1"}],
            )

            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=bom_top.id,
                planned_qty=5,
                planned_date=datetime.now() + timedelta(days=7),
            )
            with pytest.raises(ValueError, match="BOM 环路检测"):
                svc.calculate_mrp(plan.id)

    def test_expand_requirements_leaf_node(self, app, seed_products):
        """_expand_requirements：无下层 BOM 的叶子节点直接返回自身毛需求"""
        with app.app_context():
            svc = MRPService()
            from decimal import Decimal

            # comp2 无 BOM → 叶子节点
            result = svc._expand_requirements(seed_products["comp2_id"], Decimal("15"))
            assert seed_products["comp2_id"] in result
            assert float(result[seed_products["comp2_id"]]) == 15.0

    def test_expand_requirements_depth_limit(self, app, seed_products):
        """_expand_requirements：深度超过 10 层时按叶子处理，不报错"""
        with app.app_context():
            svc = MRPService()
            from decimal import Decimal

            # 构造 11 层链式 BOM：p1→p2→...→p12（每层都是下一层的子件）
            products = []
            for i in range(11):
                p = Product(
                    sku=f"TEST-MRP-DEPTH-{i:02d}",
                    name=f"深度品{i}",
                    cost_price=10,
                    sale_price=20,
                    stock_qty=0,
                    safety_stock=0,
                    avg_cost=10,
                )
                db.session.add(p)
                products.append(p)
            db.session.commit()

            # 每个产品有一个 BOM，包含下一个产品
            for i in range(10):
                svc.create_bom(
                    product_id=products[i].id,
                    items=[{"component_product_id": products[i + 1].id, "quantity": "1"}],
                )

            # 从 products[0] 展开，visited 长度达 10 后停止递归
            result = svc._expand_requirements(products[0].id, Decimal("5"))
            # 应返回某个叶子节点（深度截断处）
            assert isinstance(result, dict)
            assert len(result) >= 1


@pytest.mark.unit
class TestMRPReleasePlan:
    """release_plan 下达测试"""

    def test_release_plan_success(self, app, seed_products):
        """release_plan：净需求 > 0 的子件生成采购单，计划状态升级为 released"""
        with app.app_context():
            from models.erp import Supplier

            svc = MRPService()
            # 创建供应商
            sup = Supplier(name="测试供应商", lead_days=3, rating="A", is_active=True)
            db.session.add(sup)
            db.session.commit()

            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[
                    # comp1 库存 20，需求 60 → 净需求 40
                    {"component_product_id": seed_products["comp1_id"], "quantity": "2"},
                    # comp2 库存 50，需求 30 → 净需求 0（不生成采购单）
                    {"component_product_id": seed_products["comp2_id"], "quantity": "1"},
                ],
            )
            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=bom.id,
                planned_qty=30,
                planned_date=datetime.now() + timedelta(days=7),
            )
            svc.calculate_mrp(plan.id)

            result = svc.release_plan(plan.id)
            assert result["plan_id"] == plan.id
            assert result["released_count"] == 1  # 只有 comp1 有净需求
            assert len(result["orders"]) == 1
            assert result["orders"][0]["product_id"] == seed_products["comp1_id"]

            # 计划状态升级为 released
            plan_db = db.session.get(MRPPlan, plan.id)
            assert plan_db.status == "released"

    def test_release_plan_not_found(self, app):
        """release_plan：计划不存在时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="MRP 计划不存在"):
                svc.release_plan(99999)

    def test_release_plan_wrong_status(self, app, seed_products):
        """release_plan：计划状态非 calculated 时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[{"component_product_id": seed_products["comp1_id"], "quantity": "1"}],
            )
            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=bom.id,
                planned_qty=10,
                planned_date=datetime.now() + timedelta(days=7),
            )
            # plan.status == "draft"，未 calculate
            with pytest.raises(ValueError, match="不可下达"):
                svc.release_plan(plan.id)

    def test_release_plan_no_net_requirement(self, app, seed_products):
        """release_plan：所有子件净需求为 0 时不生成采购单"""
        with app.app_context():
            from models.erp import Supplier

            svc = MRPService()
            sup = Supplier(name="备选供应商", lead_days=3, rating="A", is_active=True)
            db.session.add(sup)
            db.session.commit()

            # comp2 库存 50，需求 10 → 净需求 0
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[{"component_product_id": seed_products["comp2_id"], "quantity": "1"}],
            )
            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=bom.id,
                planned_qty=10,
                planned_date=datetime.now() + timedelta(days=7),
            )
            svc.calculate_mrp(plan.id)

            result = svc.release_plan(plan.id)
            assert result["released_count"] == 0
            assert len(result["orders"]) == 0
            plan_db = db.session.get(MRPPlan, plan.id)
            assert plan_db.status == "released"


@pytest.mark.unit
class TestMRPCreatePlan:
    """create_plan 辅助方法测试"""

    def test_create_plan_success(self, app, seed_products):
        """create_plan：生成计划单，状态 draft"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[{"component_product_id": seed_products["comp1_id"], "quantity": "1"}],
            )
            plan = svc.create_plan(
                product_id=seed_products["finished_id"],
                bom_id=bom.id,
                planned_qty=20,
                planned_date="2026-12-31",
            )
            assert plan.id is not None
            assert plan.plan_no.startswith("MRP-")
            assert plan.status == "draft"
            assert plan.planned_qty == 20
            assert plan.bom_id == bom.id

    def test_create_plan_product_not_found(self, app):
        """create_plan：产品不存在时抛 ValueError"""
        with app.app_context():
            from models.bom import BOM

            bom = BOM(code="BOM-FAKE-001", product_id=99999, version="1.0", status="active", quantity=1, unit="个")
            db.session.add(bom)
            db.session.commit()
            svc = MRPService()
            with pytest.raises(ValueError, match="产品不存在"):
                svc.create_plan(product_id=99999, bom_id=bom.id, planned_qty=10, planned_date=datetime.now())

    def test_create_plan_bom_not_found(self, app, seed_products):
        """create_plan：BOM 不存在时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="BOM 不存在"):
                svc.create_plan(
                    product_id=seed_products["finished_id"],
                    bom_id=99999,
                    planned_qty=10,
                    planned_date=datetime.now(),
                )

    def test_create_plan_invalid_date_format(self, app, seed_products):
        """create_plan：planned_date 字符串格式非法时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[{"component_product_id": seed_products["comp1_id"], "quantity": "1"}],
            )
            with pytest.raises(ValueError, match="planned_date 格式非法"):
                svc.create_plan(
                    product_id=seed_products["finished_id"],
                    bom_id=bom.id,
                    planned_qty=10,
                    planned_date="2026/12/31",
                )


@pytest.mark.unit
class TestMRPCreateBOMEdgeCases:
    """create_bom 边界场景测试"""

    def test_create_bom_invalid_item_type(self, app, seed_products):
        """items 元素非 dict 时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="非 dict"):
                svc.create_bom(
                    product_id=seed_products["finished_id"],
                    items=["not_a_dict"],
                )

    def test_create_bom_missing_component_id(self, app, seed_products):
        """item 缺少 component_product_id 时抛 ValueError"""
        with app.app_context():
            svc = MRPService()
            with pytest.raises(ValueError, match="缺少 component_product_id"):
                svc.create_bom(
                    product_id=seed_products["finished_id"],
                    items=[{"quantity": "1"}],
                )

    def test_create_bom_custom_code_and_sequence(self, app, seed_products):
        """自定义 code 和 sequence"""
        with app.app_context():
            svc = MRPService()
            bom = svc.create_bom(
                product_id=seed_products["finished_id"],
                items=[
                    {
                        "component_product_id": seed_products["comp1_id"],
                        "quantity": "1",
                        "sequence": 100,
                        "unit": "件",
                        "notes": "自定义序号",
                    }
                ],
                code="BOM-CUSTOM-001",
                version="2.0",
                quantity=5,
                unit="箱",
                description="自定义 BOM",
            )
            assert bom.code == "BOM-CUSTOM-001"
            assert bom.version == "2.0"
            assert float(bom.quantity) == 5.0
            assert bom.unit == "箱"
            assert bom.description == "自定义 BOM"
            item = BOMItem.query.filter_by(bom_id=bom.id).first()
            assert item.sequence == 100
            assert item.unit == "件"
            assert item.notes == "自定义序号"
