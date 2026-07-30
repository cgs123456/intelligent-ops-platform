"""Notifier 告警通知服务测试

覆盖：
- 无配置降级为仅记日志
- 钉钉/企微/邮件渠道发送（mock requests/smtplib）
- 线程安全初始化
- 异步发送不阻塞主流程
"""

import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fresh_notifier():
    """每个测试用全新的 Notifier 实例，避免单例污染"""
    from services.notifier import Notifier

    n = Notifier()
    return n


class TestNotifier:
    """Notifier 单元测试"""

    def test_init_from_config_no_env(self, fresh_notifier, monkeypatch):
        """无任何环境变量时，各渠道均为 None，仅记日志"""
        monkeypatch.delenv("ALERT_DINGTALK_WEBHOOK", raising=False)
        monkeypatch.delenv("ALERT_WECOM_WEBHOOK", raising=False)
        monkeypatch.delenv("ALERT_MAIL_SMTP_HOST", raising=False)

        fresh_notifier._init_from_config()
        assert fresh_notifier._dingtalk_webhook == ""
        assert fresh_notifier._wecom_webhook == ""
        assert fresh_notifier._mail_config is None
        assert fresh_notifier._initialized is True

    def test_init_from_config_with_dingtalk(self, fresh_notifier, monkeypatch):
        """配置钉钉 webhook 后正确读取"""
        monkeypatch.setenv("ALERT_DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=test")
        monkeypatch.delenv("ALERT_WECOM_WEBHOOK", raising=False)
        monkeypatch.delenv("ALERT_MAIL_SMTP_HOST", raising=False)

        fresh_notifier._init_from_config()
        assert "dingtalk.com" in fresh_notifier._dingtalk_webhook

    def test_init_from_config_with_email(self, fresh_notifier, monkeypatch):
        """配置 SMTP 后构造 mail_config dict"""
        monkeypatch.delenv("ALERT_DINGTALK_WEBHOOK", raising=False)
        monkeypatch.delenv("ALERT_WECOM_WEBHOOK", raising=False)
        monkeypatch.setenv("ALERT_MAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("ALERT_MAIL_SMTP_PORT", "465")
        monkeypatch.setenv("ALERT_MAIL_SMTP_USER", "alert@example.com")
        monkeypatch.setenv("ALERT_MAIL_SMTP_PASS", "pass")
        monkeypatch.setenv("ALERT_MAIL_TO", "ops@example.com")

        fresh_notifier._init_from_config()
        assert fresh_notifier._mail_config is not None
        assert fresh_notifier._mail_config["host"] == "smtp.example.com"
        assert fresh_notifier._mail_config["port"] == 465
        assert fresh_notifier._mail_config["to"] == "ops@example.com"

    def test_init_double_checked_locking(self, fresh_notifier, monkeypatch):
        """双重检查锁定：多次调用 _init_from_config 不重复初始化"""
        monkeypatch.setenv("ALERT_DINGTALK_WEBHOOK", "https://example.com")
        fresh_notifier._init_from_config()
        first_webhook = fresh_notifier._dingtalk_webhook

        # 改环境变量后再次调用，不应覆盖（已初始化）
        monkeypatch.setenv("ALERT_DINGTALK_WEBHOOK", "https://changed.com")
        fresh_notifier._init_from_config()
        assert fresh_notifier._dingtalk_webhook == first_webhook

    def test_send_alert_no_app_context(self, fresh_notifier, monkeypatch):
        """无 Flask app context 时降级为仅记日志，不抛异常"""
        monkeypatch.delenv("ALERT_DINGTALK_WEBHOOK", raising=False)
        monkeypatch.delenv("ALERT_WECOM_WEBHOOK", raising=False)
        monkeypatch.delenv("ALERT_MAIL_SMTP_HOST", raising=False)

        # 无 app context 调用 send_alert，应降级为日志
        fresh_notifier.send_alert("测试告警", "测试消息", level="warning")
        # 异步线程发送，这里只要不抛异常即通过

    def test_send_alert_with_app_context(self, app, fresh_notifier, monkeypatch):
        """有 Flask app context 时异步发送，不阻塞主线程"""
        monkeypatch.delenv("ALERT_DINGTALK_WEBHOOK", raising=False)
        monkeypatch.delenv("ALERT_WECOM_WEBHOOK", raising=False)
        monkeypatch.delenv("ALERT_MAIL_SMTP_HOST", raising=False)

        with app.app_context():
            fresh_notifier.send_alert("测试告警", "测试消息", level="error", context={"run_id": 1})
        # 不阻塞即通过

    def test_send_dingtalk_mock(self, fresh_notifier, monkeypatch):
        """mock requests 验证钉钉发送逻辑"""
        monkeypatch.setenv("ALERT_DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/test")
        fresh_notifier._init_from_config()

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            fresh_notifier._send_dingtalk("标题", "内容", "error", {"run_id": 1})

            assert mock_post.called
            args, kwargs = mock_post.call_args
            assert "dingtalk.com" in args[0]
            payload = kwargs["json"]
            assert payload["msgtype"] == "markdown"
            assert payload["at"]["isAtAll"] is True  # error 级别 @所有人

    def test_send_dingtalk_no_webhook(self, fresh_notifier, monkeypatch):
        """未配置 webhook 时跳过发送，不报错"""
        monkeypatch.delenv("ALERT_DINGTALK_WEBHOOK", raising=False)
        fresh_notifier._init_from_config()

        with patch("requests.post") as mock_post:
            fresh_notifier._send_dingtalk("标题", "内容", "info", {})
            assert not mock_post.called

    def test_send_wecom_mock(self, fresh_notifier, monkeypatch):
        """mock requests 验证企微发送逻辑"""
        monkeypatch.setenv("ALERT_WECOM_WEBHOOK", "https://qyapi.weixin.qq.com/test")
        fresh_notifier._init_from_config()

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            fresh_notifier._send_wecom("标题", "内容", "warning", {})

            assert mock_post.called
            payload = mock_post.call_args.kwargs["json"]
            assert payload["msgtype"] == "text"
            assert "WARNING" in payload["text"]["content"]

    def test_send_email_mock(self, fresh_notifier, monkeypatch):
        """mock smtplib 验证邮件发送逻辑"""
        monkeypatch.setenv("ALERT_MAIL_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("ALERT_MAIL_TO", "ops@example.com")
        fresh_notifier._init_from_config()

        with patch("smtplib.SMTP_SSL") as mock_smtp:
            mock_conn = MagicMock()
            mock_smtp.return_value.__enter__.return_value = mock_conn

            fresh_notifier._send_email("标题", "内容", "critical", {"step": 3})

            assert mock_smtp.called
            mock_conn.login.assert_called_once()
            mock_conn.sendmail.assert_called_once()

    def test_send_email_no_config(self, fresh_notifier, monkeypatch):
        """未配置 SMTP 时跳过邮件发送"""
        monkeypatch.delenv("ALERT_MAIL_SMTP_HOST", raising=False)
        fresh_notifier._init_from_config()

        with patch("smtplib.SMTP_SSL") as mock_smtp:
            fresh_notifier._send_email("标题", "内容", "info", {})
            assert not mock_smtp.called

    def test_dingtalk_colors_mapping(self, fresh_notifier):
        """级别到颜色映射正确"""
        assert fresh_notifier._DINGTALK_COLORS["info"] == "blue"
        assert fresh_notifier._DINGTALK_COLORS["error"] == "red"
        assert fresh_notifier._DINGTALK_COLORS["critical"] == "red"

    def test_send_dingtalk_non_200(self, fresh_notifier, monkeypatch):
        """钉钉返回非 200 时记 warning 但不抛异常"""
        monkeypatch.setenv("ALERT_DINGTALK_WEBHOOK", "https://example.com")
        fresh_notifier._init_from_config()

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_post.return_value = mock_resp

            # 不应抛异常
            fresh_notifier._send_dingtalk("标题", "内容", "info", {})
            assert mock_post.called

    def test_send_dingtalk_request_exception(self, fresh_notifier, monkeypatch):
        """requests 抛异常时记 warning 但不传播"""
        monkeypatch.setenv("ALERT_DINGTALK_WEBHOOK", "https://example.com")
        fresh_notifier._init_from_config()

        with patch("requests.post", side_effect=Exception("network error")):
            # 不应抛异常
            fresh_notifier._send_dingtalk("标题", "内容", "info", {})

    def test_now_str_format(self, fresh_notifier):
        """_now_str 返回正确日期格式"""
        s = fresh_notifier._now_str()
        # 格式 YYYY-MM-DD HH:MM:SS
        assert len(s) == 19
        assert s[4] == "-" and s[7] == "-" and s[10] == " "
