"""Data Agent Text2SQL 测试"""
import pytest


class TestDataAgentASTValidation:
    """改进8: SQL AST 4 层安全校验测试"""

    @pytest.fixture
    def agent(self, app):
        from services.data_agent import DataAgent
        with app.app_context():
            return DataAgent()

    def test_select_single_statement(self, agent):
        """合法 SELECT 单语句通过"""
        sql = 'SELECT * FROM ads_daily_ops_report LIMIT 10'
        result = agent._validate_sql_ast(sql)
        assert result is not None, '合法 SELECT 被拒绝'

    def test_with_cte_statement(self, agent):
        """WITH ... SELECT 通过"""
        sql = 'WITH t AS (SELECT dt FROM ads_daily_ops_report) SELECT * FROM t'
        result = agent._validate_sql_ast(sql)
        assert result is not None, '合法 WITH 被拒绝'

    def test_insert_blocked(self, agent):
        """INSERT 被拒绝"""
        sql = "INSERT INTO ads_daily_ops_report (dt) VALUES ('2026-01-01')"
        result = agent._validate_sql_ast(sql)
        assert result is None, 'INSERT 应被拒绝'

    def test_update_blocked(self, agent):
        """UPDATE 被拒绝"""
        sql = "UPDATE ads_daily_ops_report SET total_sales_amount = 0"
        result = agent._validate_sql_ast(sql)
        assert result is None

    def test_delete_blocked(self, agent):
        """DELETE 被拒绝"""
        sql = "DELETE FROM ads_daily_ops_report"
        result = agent._validate_sql_ast(sql)
        assert result is None

    def test_drop_blocked(self, agent):
        """DROP 被拒绝"""
        sql = "DROP TABLE ads_daily_ops_report"
        result = agent._validate_sql_ast(sql)
        assert result is None

    def test_non_whitelist_table_blocked(self, agent):
        """非白名单表被拒绝"""
        sql = "SELECT * FROM users"
        result = agent._validate_sql_ast(sql)
        assert result is None, '非白名单表应被拒绝'

    def test_multi_statement_blocked(self, agent):
        """多语句（;）被拒绝"""
        sql = "SELECT * FROM ads_daily_ops_report; DROP TABLE users"
        result = agent._validate_sql_ast(sql)
        assert result is None

    def test_force_limit_added(self, agent):
        """无 LIMIT 时自动追加 LIMIT 100"""
        sql = 'SELECT * FROM ads_daily_ops_report'
        result = agent._validate_sql_ast(sql)
        assert result is not None
        assert 'LIMIT 100' in result.upper() or 'limit 100' in result
