"""多 Agent 采购博弈测试"""

import pytest


class TestMultiAgentNegotiator:
    """改进7: 多 Agent 博弈评分"""

    @pytest.fixture
    def negotiator(self, app):
        from services.multiagent import MultiAgentNegotiator

        with app.app_context():
            return MultiAgentNegotiator()

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
