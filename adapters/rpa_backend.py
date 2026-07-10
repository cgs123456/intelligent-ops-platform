"""RPA 后端抽象 + Mock 实现

设计目的：让 RPAService 不直接耦合具体采集方式（影刀/Selenium/Playwright），
便于从模拟实现平滑切换到真实驱动。

接口契约：
- login(site_name) -> bool
- locate_element(strategy, value) -> dict
- handle_captcha() -> bool
- fetch_quote_page(supplier_id) -> list[dict]   # 抓取供应商报价
- place_order_on_supplier_portal(...) -> str    # 在供应商系统提交订单，返回外部订单号

切换实现：
    RPA_BACKEND=mock      默认，使用 MockRpaBackend（当前模拟逻辑）
    RPA_BACKEND=selenium  使用 SeleniumRpaBackend（需 pip install selenium）
    RPA_BACKEND=yidao     使用影刀 SDK 调用（需影刀运行时）
"""

import logging
import os
import random
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class RPABackend(ABC):
    """RPA 后端抽象基类"""

    @abstractmethod
    def login(self, site_name: str) -> bool:
        """登录外部站点"""

    @abstractmethod
    def locate_element(self, strategy: str, value: str) -> dict:
        """元素定位（id / xpath / text）"""

    @abstractmethod
    def handle_captcha(self) -> bool:
        """验证码识别"""

    @abstractmethod
    def fetch_quote_page(self, supplier_id: int) -> list:
        """抓取供应商报价页"""

    @abstractmethod
    def place_order_on_supplier_portal(self, supplier_name: str, product_sku: str, qty: int, unit_price: float) -> str:
        """在供应商采购平台提交订单，返回外部订单号"""


class MockRpaBackend(RPABackend):
    """Mock 实现（当前项目使用的模拟逻辑，从 services/rpa_service.py 抽出）"""

    def login(self, site_name: str) -> bool:
        logger.info("[RPA-login] 开始登录 %s", site_name)
        self.locate_element("id", f"{site_name}:username")
        self.locate_element("xpath", '//input[@type="password"]')
        self.handle_captcha()
        self.locate_element("text", "登录")
        logger.info("[RPA-login] %s 登录成功", site_name)
        return True

    def locate_element(self, strategy: str, value: str) -> dict:
        canonical = ["id", "xpath", "text"]
        if strategy not in canonical:
            strategy = "id"
        for s in canonical:
            logger.info("[RPA-locate] 尝试策略 %s 定位元素: %s", s, value)
            if s == strategy:
                logger.info("[RPA-locate] ✓ 策略 %s 命中: %s", s, value)
                return {"located": True, "strategy": s, "value": value}
            logger.info("[RPA-locate] 策略 %s 未命中，降级下一策略", s)
        logger.warning("[RPA-locate] 所有策略均未命中: %s", value)
        return {"located": False, "strategy": None, "value": value}

    def handle_captcha(self) -> bool:
        logger.warning("[RPA-captcha] 检测到验证码，切换OCR识别")
        logger.info("[RPA-captcha] OCR 识别成功，验证码已通过")
        return True

    def fetch_quote_page(self, supplier_id: int) -> list:
        """Mock：返回空列表，由 RPAService 用 ±5% 波动逻辑兜底"""
        logger.info("[RPA-fetch] 抓取供应商 %s 报价页", supplier_id)
        return []

    def place_order_on_supplier_portal(self, supplier_name: str, product_sku: str, qty: int, unit_price: float) -> str:
        """Mock：生成外部订单号"""
        from datetime import datetime

        ext_no = (
            f"EXT-{supplier_name[:4]}-" f'{datetime.now().strftime("%Y%m%d%H%M%S")}-' f"{random.randint(1000, 9999)}"
        )
        logger.info("[RPA-order] 在 %s 提交订单 sku=%s qty=%s", supplier_name, product_sku, qty)
        return ext_no


class SeleniumRpaBackend(RPABackend):
    """Selenium 真实实现（占位骨架，需安装 selenium + 配置 driver）

    使用方式：
        1. pip install selenium
        2. 下载对应浏览器 driver 到 PATH
        3. 设置环境变量 RPA_BACKEND=selenium

    注意：当前仅提供骨架，真实业务字段定位需根据具体站点调整。
    """

    def __init__(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.common.by import By

            self._By = By
            self._driver = webdriver.Chrome()  # 或 Firefox/Edge
            logger.info("[RPA-selenium] driver 已初始化")
        except ImportError:
            raise ImportError("SeleniumRpaBackend 需要 pip install selenium")
        except Exception as e:
            raise RuntimeError(f"Selenium driver 初始化失败：{e}")

    def login(self, site_name: str) -> bool:
        # TODO: 真实登录逻辑，按 site_name 加载配置
        raise NotImplementedError("SeleniumRpaBackend.login 待实现，需根据站点定制")

    def locate_element(self, strategy: str, value: str) -> dict:
        by_map = {
            "id": self._By.ID,
            "xpath": self._By.XPATH,
            "text": self._By.PARTIAL_LINK_TEXT,
        }
        by = by_map.get(strategy, self._By.ID)
        try:
            el = self._driver.find_element(by, value)
            return {"located": True, "strategy": strategy, "value": value, "element": el}
        except Exception:
            return {"located": False, "strategy": None, "value": value}

    def handle_captcha(self) -> bool:
        # TODO: 接入打码平台（如超级鹰/图鉴）或 OCR
        logger.warning("[RPA-selenium] 验证码识别未实现")
        return False

    def fetch_quote_page(self, supplier_id: int) -> list:
        # TODO: 按供应商 ID 加载 URL，遍历报价表抓取
        raise NotImplementedError("SeleniumRpaBackend.fetch_quote_page 待实现")

    def place_order_on_supplier_portal(self, supplier_name: str, product_sku: str, qty: int, unit_price: float) -> str:
        # TODO: 填表 + 提交 + 抓取外部订单号
        raise NotImplementedError("SeleniumRpaBackend.place_order_on_supplier_portal 待实现")

    def __del__(self):
        try:
            self._driver.quit()
        except Exception:
            pass


def get_rpa_backend() -> RPABackend:
    """根据 RPA_BACKEND 环境变量返回对应后端实例"""
    backend = os.getenv("RPA_BACKEND", "mock").lower()
    if backend == "selenium":
        return SeleniumRpaBackend()
    if backend == "mock":
        return MockRpaBackend()
    logger.warning("未知 RPA_BACKEND=%s，回退 mock", backend)
    return MockRpaBackend()
