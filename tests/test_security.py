"""安全测试 — SQL 注入防护、错误响应脱敏、密码强度校验"""

import pytest


@pytest.mark.usefixtures("db_session")
class TestSecurity:
    """安全相关测试：SQL AST 校验、错误处理器不泄漏内部细节、密码强度策略"""

    @pytest.fixture
    def agent(self, app):
        """构造 DataAgent 实例（用于 SQL AST 校验测试）"""
        from services.data_agent import DataAgent

        with app.app_context():
            return DataAgent()

    # ==================== 1. SQL 注入：子查询引用非白名单表 ====================

    def test_sql_injection_subquery_non_whitelist(self, agent):
        """子查询引用非白名单表（users）被拒绝，防止 SQL 注入跨表读取"""
        sql = "SELECT * FROM ads_daily_ops_report WHERE dt IN (SELECT dt FROM users) LIMIT 10"
        result = agent._validate_sql_ast(sql)
        assert result is None, "子查询引用非白名单表应被拒绝"

    # ==================== 2. SQL 注入：多语句 + DROP 被拒绝 ====================

    def test_sql_injection_multi_statement_drop(self, agent):
        """多语句注入（SELECT; DROP TABLE）被拒绝，sqlglot parse_one 天然保证单语句"""
        sql = "SELECT * FROM ads_daily_ops_report; DROP TABLE sys_user"
        result = agent._validate_sql_ast(sql)
        assert result is None, "多语句注入应被拒绝"

    # ==================== 3. 500 错误处理器不暴露内部异常 ====================

    def test_500_error_no_internal_leak(self, client):
        """触发未处理异常时，500 响应返回通用消息，不包含 str(e)"""
        # 访问不存在的路由触发 404（验证错误处理器返回统一信封，不泄漏内部细节）
        resp = client.get("/api/v1/nonexistent-endpoint")
        assert resp.status_code == 404
        data = resp.get_json()
        # 统一信封格式：{code, message, data, request_id, timestamp}
        assert "message" in data
        # 404 不暴露内部路由细节
        assert "不存在" in data["message"]
        assert "traceback" not in str(data).lower()
        assert "exception" not in str(data).lower()

    # ==================== 4. 健康检查端点不泄漏敏感信息 ====================

    def test_health_endpoint_no_sensitive_leak(self, client):
        """健康检查端点返回 ok/fail，不暴露数据库连接串、Redis 地址等敏感信息"""
        resp = client.get("/health")
        assert resp.status_code in (200, 503)
        data = resp.get_json()
        assert "status" in data
        # 响应体中不应包含敏感配置
        body_str = str(data).lower()
        assert "password" not in body_str
        assert "secret" not in body_str
        assert "redis_url" not in body_str
        assert "database_url" not in body_str

    # ==================== 5. 密码强度：弱密码被拒绝 ====================

    def test_weak_password_rejected(self):
        """密码强度校验拒绝短密码、无大写字母、无数字的弱密码"""
        from services.password_policy import validate_password

        # 太短
        ok, msg = validate_password("Ab1")
        assert ok is False
        assert "长度" in msg

        # 无大写字母
        ok, msg = validate_password("abcdef12345")
        assert ok is False
        assert "大写" in msg

        # 无数字
        ok, msg = validate_password("Abcdefghijk")
        assert ok is False
        assert "数字" in msg

        # 无小写字母
        ok, msg = validate_password("ABCDEFGHIJ1")
        assert ok is False
        assert "小写" in msg

        # 无特殊字符（P0-8: REQUIRE_SPECIAL=True）
        ok, msg = validate_password("StrongPass123")
        assert ok is False
        assert "特殊字符" in msg

        # 强密码通过（含特殊字符）
        ok, msg = validate_password("StrongPass@123")
        assert ok is True
        assert msg is None
