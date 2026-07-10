"""告警通知服务

支持多渠道通知：钉钉 webhook、邮件（SMTP）、企业微信 webhook。
通过环境变量配置：
- ALERT_DINGTALK_WEBHOOK：钉钉机器人 webhook URL
- ALERT_WECOM_WEBHOOK：企业微信机器人 webhook URL
- ALERT_MAIL_TO：告警邮件收件人（逗号分隔）
- ALERT_MAIL_SMTP_HOST / ALERT_MAIL_SMTP_PORT / ALERT_MAIL_SMTP_USER / ALERT_MAIL_SMTP_PASS

调用方式：
    from services.notifier import notifier
    notifier.send_alert(
        title='闭环步骤3失败',
        message='Run #12 step3 RPA下单失败：供应商A 报价接口超时',
        level='error',
    )

设计要点：
- 所有渠道异步发送（不阻塞主流程）
- 单渠道失败不影响其他渠道
- 无任何配置时降级为仅记日志（dev 模式可接受）
"""

import logging
import os
import threading

from flask import current_app

logger = logging.getLogger(__name__)


class Notifier:
    """多渠道告警通知"""

    # 级别到钉钉颜色映射
    _DINGTALK_COLORS = {
        "info": "blue",
        "warning": "orange",
        "error": "red",
        "critical": "red",
    }

    def __init__(self):
        self._dingtalk_webhook = None
        self._wecom_webhook = None
        self._mail_config = None
        self._initialized = False

    def _init_from_config(self):
        """延迟初始化（需要 app context 读取环境变量）"""
        if self._initialized:
            return
        self._dingtalk_webhook = os.getenv("ALERT_DINGTALK_WEBHOOK", "")
        self._wecom_webhook = os.getenv("ALERT_WECOM_WEBHOOK", "")
        smtp_host = os.getenv("ALERT_MAIL_SMTP_HOST", "")
        if smtp_host:
            self._mail_config = {
                "host": smtp_host,
                "port": int(os.getenv("ALERT_MAIL_SMTP_PORT", "465")),
                "user": os.getenv("ALERT_MAIL_SMTP_USER", ""),
                "password": os.getenv("ALERT_MAIL_SMTP_PASS", ""),
                "to": os.getenv("ALERT_MAIL_TO", ""),
                "from": os.getenv("ALERT_MAIL_FROM", "ops-platform-alert@local"),
            }
        self._initialized = True

    def send_alert(self, title, message, level="error", context=None):
        """发送告警（异步，不阻塞主流程）

        :param title: 告警标题
        :param message: 告警正文
        :param level: info / warning / error / critical
        :param context: 可选的附加上下文 dict（如 run_id, step, trace_id）
        """
        try:
            self._init_from_config()
        except Exception:
            pass

        # 异步发送，避免阻塞业务线程
        thread = threading.Thread(
            target=self._send_sync,
            args=(title, message, level, context or {}),
            daemon=True,
        )
        thread.start()

    def _send_sync(self, title, message, level, context):
        """同步发送到所有已配置渠道"""
        # 在新线程中无 app context，需手动 push
        try:
            from app import create_app
            from config import config as default_config

            app = create_app(default_config)
            with app.app_context():
                self._send_dingtalk(title, message, level, context)
                self._send_wecom(title, message, level, context)
                self._send_email(title, message, level, context)
        except Exception as e:
            logger.error("告警发送失败 title=%s err=%s", title, e)

    def _send_dingtalk(self, title, message, level, context):
        if not self._dingtalk_webhook:
            return
        try:
            import requests

            ctx_str = "\n".join(f"- {k}: {v}" for k, v in context.items()) if context else ""
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": (
                        f"### {title}\n\n"
                        f"> **级别**: {level}\n\n"
                        f"> **时间**: {self._now_str()}\n\n"
                        f"**详情**:\n\n{message}\n\n"
                        f'{"**上下文**:" + chr(10) + chr(10) + ctx_str if ctx_str else ""}'
                    ),
                },
                "at": {"isAtAll": level in ("error", "critical")},
            }
            resp = requests.post(self._dingtalk_webhook, json=payload, timeout=5)
            if resp.status_code != 200:
                logger.warning("钉钉告警非 200：%s", resp.status_code)
        except Exception as e:
            logger.warning("钉钉告警发送失败：%s", e)

    def _send_wecom(self, title, message, level, context):
        if not self._wecom_webhook:
            return
        try:
            import requests

            ctx_str = " | ".join(f"{k}={v}" for k, v in context.items()) if context else ""
            content = (
                f"【{level.upper()}】{title}\n"
                f"时间: {self._now_str()}\n"
                f"详情: {message}\n"
                f'{("上下文: " + ctx_str) if ctx_str else ""}'
            )
            payload = {"msgtype": "text", "text": {"content": content}}
            resp = requests.post(self._wecom_webhook, json=payload, timeout=5)
            if resp.status_code != 200:
                logger.warning("企微告警非 200：%s", resp.status_code)
        except Exception as e:
            logger.warning("企微告警发送失败：%s", e)

    def _send_email(self, title, message, level, context):
        if not self._mail_config:
            return
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            cfg = self._mail_config
            ctx_str = "\n".join(f"  {k}: {v}" for k, v in context.items()) if context else ""
            body = (
                f"告警级别: {level}\n"
                f"告警时间: {self._now_str()}\n"
                f"告警标题: {title}\n\n"
                f"详情:\n{message}\n\n"
                f'{"上下文:" + chr(10) + ctx_str if ctx_str else ""}\n'
            )
            msg = MIMEMultipart()
            msg["From"] = cfg["from"]
            msg["To"] = cfg["to"]
            msg["Subject"] = f"[{level.upper()}] {title}"
            msg.attach(MIMEText(body, "plain", "utf-8"))
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10) as s:
                s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["from"], cfg["to"].split(","), msg.as_string())
        except Exception as e:
            logger.warning("邮件告警发送失败：%s", e)

    @staticmethod
    def _now_str():
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 单例
notifier = Notifier()
