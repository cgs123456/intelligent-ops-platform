"""Agent 评测框架测试 — Text2SQL gold SQL 回归测试"""

import pytest


class TestAgentEvaluator:
    """AgentEvaluator：gold SQL 回归测试框架"""

    @pytest.fixture
    def evaluator(self, app, tmp_path):
        """构造评测器实例（用 tmp_path 隔离报告目录，避免测试间污染）"""
        from services.data_agent import DataAgent
        from services.evaluator import AgentEvaluator

        with app.app_context():
            agent = DataAgent()
            return AgentEvaluator(data_agent=agent, reports_dir=tmp_path)

    def test_load_gold_cases(self, evaluator):
        """gold SQL 数据加载正确：12 条用例，字段完整，白名单表合法"""
        from services.aigc_service import AIGCService

        cases = evaluator.load_gold_cases()
        assert len(cases) >= 10, "gold 用例至少 10 条"

        whitelist = set(AIGCService.WHITELIST_TABLES.keys())
        required_fields = {"id", "question", "expected_sql", "expected_tables", "category"}

        for case in cases:
            # 必需字段齐全
            assert required_fields.issubset(case.keys()), f"用例 {case.get('id')} 缺少字段"
            # 白名单表必须与 AIGCService.WHITELIST_TABLES 一致
            for t in case["expected_tables"]:
                assert t in whitelist, f"用例 {case['id']} 的表 {t} 不在白名单内"

    def test_compare_sql_exact(self, evaluator):
        """完全匹配：相同 SQL 返回 exact"""
        sql = "SELECT SUM(total_sales_amount) FROM ads_daily_ops_report WHERE dt = '2026-01-01' LIMIT 100"
        match_type, passed = evaluator._compare_sql(sql, sql)
        assert match_type == "exact"
        assert passed is True

    def test_compare_sql_table_set(self, evaluator):
        """表名集合匹配：SQL 不同但表名相同返回 table_set"""
        expected = "SELECT total_sales_amount FROM ads_daily_ops_report WHERE dt = '2026-01-01' LIMIT 100"
        actual = "SELECT total_purchase_amount FROM ads_daily_ops_report ORDER BY dt DESC LIMIT 50"
        match_type, passed = evaluator._compare_sql(actual, expected)
        assert match_type == "table_set"
        assert passed is True

    def test_compare_sql_none(self, evaluator):
        """不匹配：表名不同且结果集不同返回 none"""
        expected = "SELECT * FROM ads_daily_ops_report LIMIT 10"
        actual = "SELECT * FROM dws_sales_sku_daily LIMIT 10"
        match_type, passed = evaluator._compare_sql(actual, expected)
        assert match_type == "none"
        assert passed is False

    def test_compare_sql_none_when_actual_is_none(self, evaluator):
        """LLM 未生成 SQL（None）时返回 none"""
        match_type, passed = evaluator._compare_sql(None, "SELECT * FROM ads_daily_ops_report LIMIT 10")
        assert match_type == "none"
        assert passed is False

    def test_run_evaluation_mock(self, evaluator):
        """mock LLM 返回 gold SQL，验证准确率 100%"""
        cases = evaluator.load_gold_cases()
        # 构造 question → expected_sql 映射，模拟 LLM 完美生成
        q_to_sql = {c["question"]: c["expected_sql"] for c in cases}
        evaluator._agent._generate_sql = lambda q: q_to_sql.get(q, "")

        report = evaluator.run_evaluation()
        assert report["total"] == len(cases)
        assert report["passed"] == report["total"]
        assert report["failed"] == 0
        assert report["accuracy"] == 1.0
        # 首次运行无历史对比
        assert report["comparison"]["vs_last_run"] is None
        # 所有 detail 均通过
        for d in report["details"]:
            assert d["passed"] is True
            assert d["match_type"] == "exact"

    def test_run_evaluation_mismatch(self, evaluator):
        """mock LLM 返回错误 SQL，验证准确率下降"""
        # 返回一条与所有 gold SQL 都不匹配的 SQL（无 FROM → 无表名）
        evaluator._agent._generate_sql = lambda q: "SELECT 1 AS x LIMIT 1"

        report = evaluator.run_evaluation()
        assert report["accuracy"] < 1.0
        assert report["failed"] > 0
        # SELECT 1 无表名，不可能是 exact / table_set / result_set
        for d in report["details"]:
            assert d["match_type"] == "none"
            assert d["passed"] is False

    def test_regression_detection(self, evaluator):
        """两次评测对比，检测退化：第一次全通过 → 第二次全失败"""
        cases = evaluator.load_gold_cases()
        q_to_sql = {c["question"]: c["expected_sql"] for c in cases}

        # 第一次：全部正确（100%）
        evaluator._agent._generate_sql = lambda q: q_to_sql.get(q, "")
        report1 = evaluator.run_evaluation()
        assert report1["accuracy"] == 1.0
        assert report1["comparison"]["vs_last_run"] is None

        # 第二次：全部错误（退化）
        evaluator._agent._generate_sql = lambda q: "SELECT 1 AS x LIMIT 1"
        report2 = evaluator.run_evaluation()
        assert report2["accuracy"] < report1["accuracy"]

        comp = report2["comparison"]["vs_last_run"]
        assert comp is not None
        # 准确率下降
        assert comp["accuracy_delta"] < 0
        # 之前通过的用例现在全部失败
        assert len(comp["new_failures"]) == len(cases)
        # 没有新增通过
        assert len(comp["new_passes"]) == 0
