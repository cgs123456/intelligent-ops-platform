"""多 Agent 采购博弈测试"""

import pytest


@pytest.fixture
def negotiator(app):
    from services.multiagent import MultiAgentNegotiator

    with app.app_context():
        return MultiAgentNegotiator()


class TestMultiAgentNegotiator:
    """改进7: 多 Agent 博弈评分"""

    def test_score_quotes_price_weight(self, negotiator):
        """价格权重 50%：低价者得分高"""
        quotes = [
            {"supplier_id": 1, "supplier_name": "A", "price": 80, "lead_days": 3, "rating": "A"},
            {"supplier_id": 2, "supplier_name": "B", "price": 100, "lead_days": 3, "rating": "A"},
        ]
        result = negotiator.score_quotes(quotes)
        assert len(result) == 2
        # score 越低越优，result[0] 是 best
        assert result[0]["supplier_id"] == 1  # 低价者胜

    def test_score_quotes_empty_list(self, negotiator):
        """空报价列表不崩溃"""
        result = negotiator.score_quotes([])
        assert result == []

    def test_score_quotes_single_supplier(self, negotiator):
        """单供应商也能评分"""
        quotes = [
            {"supplier_id": 1, "supplier_name": "独", "price": 100, "lead_days": 5, "rating": "B"},
        ]
        result = negotiator.score_quotes(quotes)
        assert len(result) == 1
        assert result[0]["supplier_id"] == 1

    def test_score_quotes_min_max_normalization(self, negotiator):
        """min-max 归一化：综合最优者排第一"""
        quotes = [
            {"supplier_id": 1, "supplier_name": "A", "price": 80, "lead_days": 3, "rating": "A"},
            {"supplier_id": 2, "supplier_name": "B", "price": 120, "lead_days": 7, "rating": "C"},
            {"supplier_id": 3, "supplier_name": "C", "price": 100, "lead_days": 5, "rating": "B"},
        ]
        result = negotiator.score_quotes(quotes)
        # 综合最优应是 A（最低价 + 最短交期 + 最高评级）
        assert result[0]["supplier_id"] == 1
        # 归一化分数应在 0-1 之间
        for q in result:
            assert 0.0 <= q.get("score", 0) <= 1.0


class TestMultiAgentHelpers:
    """工具方法单测：_get / _parse_json / _min_max / _mock_competitor"""

    def test_get_dict(self, negotiator):
        assert negotiator._get({"id": 1, "name": "x"}, "id") == 1
        assert negotiator._get({"id": 1}, "missing", "default") == "default"

    def test_get_obj_none(self, negotiator):
        assert negotiator._get(None, "id", "fallback") == "fallback"

    def test_get_orm_object(self, negotiator, app):
        """ORM 对象走 getattr"""
        from extensions import db
        from models.erp import Supplier

        with app.app_context():
            sup = Supplier(name="测试ORM", lead_days=4, rating="A")
            db.session.add(sup)
            db.session.commit()
            assert negotiator._get(sup, "name") == "测试ORM"
            assert negotiator._get(sup, "not_exist", "def") == "def"

    def test_parse_json_valid(self, negotiator):
        assert negotiator._parse_json('{"qty": 10}') == {"qty": 10}

    def test_parse_json_embedded(self, negotiator):
        """从带噪声文本中提取 JSON"""
        text = '前缀文字 {"qty": 5, "reason": "缺货"} 后缀'
        result = negotiator._parse_json(text)
        assert result == {"qty": 5, "reason": "缺货"}

    def test_parse_json_empty(self, negotiator):
        assert negotiator._parse_json("") is None
        assert negotiator._parse_json(None) is None

    def test_parse_json_invalid(self, negotiator):
        assert negotiator._parse_json("not json at all") is None
        assert negotiator._parse_json("{bad json}") is None

    def test_min_max_lower_better(self, negotiator):
        """值越小分数越低（越优）"""
        norm = negotiator._min_max([10, 20, 30], lower_better=True)
        assert norm[0] == 0.0  # 最小值归一化为 0
        assert norm[2] == 1.0  # 最大值归一化为 1

    def test_min_max_higher_better(self, negotiator):
        """值越大分数越低（越优）—— 评级场景"""
        norm = negotiator._min_max([3, 4, 5], lower_better=False)
        assert norm[0] == 1.0  # 3 最差 → 1.0
        assert norm[2] == 0.0  # 5 最优 → 0.0

    def test_min_max_all_equal(self, negotiator):
        """所有值相等时返回 0.5"""
        norm = negotiator._min_max([7, 7, 7])
        assert norm == [0.5, 0.5, 0.5]

    def test_mock_competitor(self, negotiator):
        """mock 竞争对手构造正确"""
        comp = negotiator._mock_competitor(None, [])
        assert comp["id"] == 9001
        assert comp["rating"] == "B"
        assert comp["lead_days"] == 7


class TestAgentABuyer:
    """Agent A 采购需求生成测试（规则兜底）"""

    def test_buyer_rule_fallback_shortage(self, negotiator):
        """库存低于安全库存*2 时计算缺口"""
        product = {
            "id": 1,
            "name": "测试品",
            "cost_price": 10,
            "sale_price": 20,
            "stock_qty": 5,
            "safety_stock": 30,
        }
        demand = negotiator._agent_a_buyer(product)
        # 缺口 = 30*2 - 5 = 55
        assert demand["qty"] == 55
        assert demand["shortage"] == 55
        assert demand["max_unit_price"] == 11.0  # 10 * 1.1
        assert demand["agent"] == "buyer"

    def test_buyer_rule_fallback_no_shortage(self, negotiator):
        """库存充足时缺口为 0，qty 至少为 1"""
        product = {
            "id": 2,
            "name": "充足品",
            "cost_price": 10,
            "sale_price": 20,
            "stock_qty": 100,
            "safety_stock": 30,
        }
        demand = negotiator._agent_a_buyer(product)
        assert demand["shortage"] == 0
        assert demand["qty"] == 1  # max(0, 1)

    def test_buyer_with_suggested_qty(self, negotiator):
        """显式传入 suggested_qty 优先于缺口计算"""
        product = {
            "id": 3,
            "name": "指定品",
            "cost_price": 0,
            "sale_price": 0,
            "stock_qty": 0,
            "safety_stock": 0,
        }
        demand = negotiator._agent_a_buyer(product, suggested_qty=42)
        assert demand["qty"] == 42
        # cost=0 时 max_unit_price 为 None
        assert demand["max_unit_price"] is None

    def test_buyer_llm_parsed_success(self, negotiator, monkeypatch):
        """LLM 可用且返回合法 JSON 时使用 LLM 决策"""
        product = {
            "id": 4,
            "name": "LLM品",
            "cost_price": 10,
            "sale_price": 20,
            "stock_qty": 5,
            "safety_stock": 30,
        }
        # 模拟 LLM 可用 + 返回合法 JSON（实例属性不绑定 self）
        monkeypatch.setattr(negotiator._aigc, "_llm_available", lambda: True)

        def fake_call(prompt, temperature=0.3):
            return '{"qty": 99, "max_unit_price": 15.5, "reason": "LLM建议"}'

        monkeypatch.setattr(negotiator._aigc, "_call_llm", fake_call)
        demand = negotiator._agent_a_buyer(product)
        assert demand["qty"] == 99
        assert demand["max_unit_price"] == 15.5
        assert demand["reason"] == "LLM建议"

    def test_buyer_llm_invalid_json_fallback(self, negotiator, monkeypatch):
        """LLM 返回非法 JSON 时回退规则"""
        product = {
            "id": 5,
            "name": "降级品",
            "cost_price": 10,
            "sale_price": 20,
            "stock_qty": 5,
            "safety_stock": 30,
        }
        monkeypatch.setattr(negotiator._aigc, "_llm_available", lambda: True)
        monkeypatch.setattr(negotiator._aigc, "_call_llm", lambda p, temperature=0.3: "not json")
        demand = negotiator._agent_a_buyer(product)
        # 回退规则
        assert demand["agent"] == "buyer"
        assert demand["shortage"] == 55


class TestAgentSupplier:
    """Agent B/C 供应商报价测试（规则兜底）"""

    def test_supplier_rule_fallback(self, negotiator):
        """规则兜底：基于成本 + 评级因子 + 随机波动"""
        product = {
            "id": 1,
            "name": "测试品",
            "cost_price": 100,
            "sale_price": 200,
            "stock_qty": 5,
            "safety_stock": 30,
        }
        supplier = {"id": 1, "name": "A供应商", "lead_days": 5, "rating": "A"}
        buyer_demand = {"qty": 10, "max_unit_price": 120}
        quote = negotiator._agent_supplier(product, supplier, buyer_demand)
        assert quote["supplier_id"] == 1
        assert quote["supplier_name"] == "A供应商"
        assert quote["rating"] == "A"
        # A 级 rating_factor=1.05，base_price = max(105, 100)*1.05 = 110.25
        # 价格在 base_price ±3% 范围
        assert 100.0 <= quote["price"] <= 120.0
        # A 级 lead_adjust=-2，交期 5-2=3
        assert quote["lead_days"] == 3
        assert quote["agent"] == "supplier"

    def test_supplier_cost_zero_fallback(self, negotiator):
        """cost_price=0 时兜底为 100"""
        product = {"id": 2, "name": "零成本", "cost_price": 0, "sale_price": 0, "stock_qty": 0, "safety_stock": 0}
        supplier = {"id": 2, "name": "B供应商", "lead_days": 7, "rating": "C"}
        buyer_demand = {"qty": 5, "max_unit_price": None}
        quote = negotiator._agent_supplier(product, supplier, buyer_demand)
        # cost 兜底 100，C 级 rating_factor=0.95，base=105*0.95=99.75
        assert quote["price"] > 0
        # C 级 lead_adjust=3，交期 7+3=10
        assert quote["lead_days"] == 10

    def test_supplier_llm_parsed_success(self, negotiator, monkeypatch):
        """LLM 可用且返回合法 JSON 时使用 LLM 报价"""
        product = {"id": 3, "name": "LLM品", "cost_price": 100, "sale_price": 200, "stock_qty": 5, "safety_stock": 30}
        supplier = {"id": 3, "name": "LLM供", "lead_days": 5, "rating": "B"}
        buyer_demand = {"qty": 10, "max_unit_price": 120}
        monkeypatch.setattr(negotiator._aigc, "_llm_available", lambda: True)
        monkeypatch.setattr(
            negotiator._aigc,
            "_call_llm",
            lambda p, temperature=0.5: '{"price": 108.5, "lead_days": 4, "reason": "竞争报价"}',
        )
        quote = negotiator._agent_supplier(product, supplier, buyer_demand)
        assert quote["price"] == 108.5
        assert quote["lead_days"] == 4
        assert quote["reason"] == "竞争报价"

    def test_supplier_with_history_quotes(self, app):
        """有报价历史时按历史均价计算"""
        from datetime import date as _date

        from extensions import db
        from models.erp import Product, Supplier
        from models.external import ExtSupplierQuote
        from services.multiagent import MultiAgentNegotiator

        with app.app_context():
            # 清理避免污染
            db.session.query(ExtSupplierQuote).delete()
            sup = Supplier(name="历史供", lead_days=5, rating="A")
            db.session.add(sup)
            prod = Product(
                sku="HIST-001",
                name="历史品",
                cost_price=100,
                sale_price=200,
                stock_qty=5,
                safety_stock=30,
                avg_cost=100,
            )
            db.session.add(prod)
            db.session.flush()
            # 插入 2 条历史报价
            db.session.add(
                ExtSupplierQuote(
                    supplier_id=sup.id, product_id=prod.id, quote_price=90, quote_date=_date.today(), source="test"
                )
            )
            db.session.add(
                ExtSupplierQuote(
                    supplier_id=sup.id, product_id=prod.id, quote_price=110, quote_date=_date.today(), source="test"
                )
            )
            db.session.commit()

            neg = MultiAgentNegotiator(seed=42)
            quote = neg._agent_supplier(prod, sup, {"qty": 10, "max_unit_price": 120})
            # 历史均价 (90+110)/2 = 100，A 级因子 1.05 → base 105
            assert 95.0 <= quote["price"] <= 115.0


class TestNegotiateFlow:
    """negotiate 主入口集成测试"""

    def test_negotiate_with_two_suppliers(self, negotiator):
        """两个供应商完整博弈流程"""
        product = {
            "id": 1,
            "name": "博弈品",
            "cost_price": 100,
            "sale_price": 200,
            "stock_qty": 5,
            "safety_stock": 30,
        }
        suppliers = [
            {"id": 1, "name": "供应商A", "lead_days": 3, "rating": "A"},
            {"id": 2, "name": "供应商B", "lead_days": 7, "rating": "C"},
        ]
        result = negotiator.negotiate(product, suppliers)
        assert result["product_id"] == 1
        assert result["product_name"] == "博弈品"
        assert "buyer_demand" in result
        assert len(result["quotes"]) == 2
        assert result["best"] is not None
        assert "negotiation_log" in result
        assert isinstance(result["negotiation_log"], list)
        # best 是 quotes[0]（按 score 升序）
        assert result["best"]["score"] == result["quotes"][0]["score"]

    def test_negotiate_single_supplier_mock_competitor(self, negotiator):
        """单供应商时自动补 mock 竞争对手"""
        product = {
            "id": 2,
            "name": "单供品",
            "cost_price": 50,
            "sale_price": 100,
            "stock_qty": 10,
            "safety_stock": 20,
        }
        suppliers = [{"id": 1, "name": "唯一供", "lead_days": 5, "rating": "B"}]
        result = negotiator.negotiate(product, suppliers)
        # 应有 2 个报价（含 mock 竞争对手）
        assert len(result["quotes"]) == 2
        supplier_names = [q["supplier_name"] for q in result["quotes"]]
        assert "竞争对手-模拟" in supplier_names

    def test_negotiate_empty_suppliers(self, negotiator):
        """空供应商列表时补 2 个 mock"""
        product = {
            "id": 3,
            "name": "无供品",
            "cost_price": 50,
            "sale_price": 100,
            "stock_qty": 10,
            "safety_stock": 20,
        }
        result = negotiator.negotiate(product, [])
        # 空列表补 mock，但 mock 只补到 1 个（sup_list < 2 补 1）
        assert len(result["quotes"]) >= 1

    def test_negotiate_with_suggested_qty(self, negotiator):
        """显式 suggested_qty 生效"""
        product = {
            "id": 4,
            "name": "定量品",
            "cost_price": 100,
            "sale_price": 200,
            "stock_qty": 100,
            "safety_stock": 30,
        }
        suppliers = [
            {"id": 1, "name": "A", "lead_days": 3, "rating": "A"},
            {"id": 2, "name": "B", "lead_days": 7, "rating": "C"},
        ]
        result = negotiator.negotiate(product, suppliers, suggested_qty=77)
        assert result["buyer_demand"]["qty"] == 77

    def test_negotiate_supplier_exception_handled(self, negotiator, monkeypatch):
        """供应商报价异常时被捕获，不影响其他供应商"""
        product = {
            "id": 5,
            "name": "异常品",
            "cost_price": 100,
            "sale_price": 200,
            "stock_qty": 5,
            "safety_stock": 30,
        }
        suppliers = [
            {"id": 1, "name": "正常供", "lead_days": 3, "rating": "A"},
            {"id": 2, "name": "异常供", "lead_days": 7, "rating": "C"},
        ]

        # 让第二个供应商报价抛异常
        original = negotiator._agent_supplier

        call_count = [0]

        def side_effect(prod, sup, demand):
            call_count[0] += 1
            if sup.get("id") == 2:
                raise RuntimeError("模拟报价失败")
            return original(prod, sup, demand)

        monkeypatch.setattr(negotiator, "_agent_supplier", side_effect)
        result = negotiator.negotiate(product, suppliers)
        # 异常供应商被跳过，正常供应商报价保留
        assert len(result["quotes"]) == 1
        assert result["quotes"][0]["supplier_id"] == 1


class TestBuildLog:
    """_build_log 日志生成测试"""

    def test_build_log_with_best(self, negotiator):
        """有 best 时日志含决策行"""
        buyer_demand = {"qty": 50, "max_unit_price": 120, "reason": "缺货"}
        quotes = [
            {"supplier_name": "供A", "rating": "A", "price": 110, "lead_days": 3, "reason": "低价"},
            {"supplier_name": "供B", "rating": "B", "price": 115, "lead_days": 5, "reason": "中价"},
        ]
        best = {**quotes[0], "score": 0.35}
        log = negotiator._build_log("测试品", buyer_demand, quotes, best)
        assert isinstance(log, list)
        assert any("采购方" in line for line in log)
        assert any("供A" in line for line in log)
        assert any("决策" in line for line in log)
        assert any("0.35" in line for line in log)

    def test_build_log_without_best(self, negotiator):
        """无 best 时日志含无可用报价"""
        buyer_demand = {"qty": 50, "max_unit_price": 120, "reason": "缺货"}
        quotes = []
        log = negotiator._build_log("无报价品", buyer_demand, quotes, None)
        assert any("无可用报价" in line for line in log)
