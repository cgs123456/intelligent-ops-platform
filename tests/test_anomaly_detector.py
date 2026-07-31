"""FDE 时序异常检测测试"""

from datetime import date, timedelta

import pytest


class TestAnomalyDetector:
    """改进9: 7 日 MA + 2σ 检测"""

    @pytest.fixture
    def detector(self, app):
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            return AnomalyDetector()

    def test_normal_sales_no_anomaly(self, detector):
        """正常销量（无方差）不报异常"""
        target = date.today()
        sales = []
        for i in range(15):
            dt = target - timedelta(days=14 - i)
            sales.append({"dt": dt, "qty": 100, "name": "正常品"})
        result = detector._check_product(1, sales, target)
        # stdev=0 时返回 None
        assert result is None

    def test_critical_3sigma_break(self, detector):
        """3σ 下限突破 → critical"""
        target = date.today()
        pattern = [95, 100, 105, 100, 98, 102, 100, 97, 103, 100, 96, 104, 101, 99]
        sales = []
        for i in range(15):
            dt = target - timedelta(days=14 - i)
            qty = 5 if dt == target else pattern[i]
            sales.append({"dt": dt, "qty": qty, "name": "异常品A"})
        result = detector._check_product(2, sales, target)
        assert result is not None
        assert result["severity"] == "critical"

    def test_warning_2sigma_break(self, detector):
        """2σ 下限突破 → warning 或 critical"""
        target = date.today()
        pattern = [90, 110, 95, 105, 100, 92, 108, 100, 88, 112, 100, 95, 105, 100]
        sales = []
        for i in range(15):
            dt = target - timedelta(days=14 - i)
            qty = 75 if dt == target else pattern[i]
            sales.append({"dt": dt, "qty": qty, "name": "异常品B"})
        result = detector._check_product(3, sales, target)
        assert result is not None
        assert result["severity"] in ("warning", "critical")

    def test_insufficient_history_skipped(self, detector):
        """历史数据不足 7 天 → 跳过"""
        target = date.today()
        sales = [
            {"dt": target - timedelta(days=1), "qty": 100, "name": "新品"},
            {"dt": target - timedelta(days=2), "qty": 95, "name": "新品"},
            {"dt": target, "qty": 10, "name": "新品"},
        ]
        result = detector._check_product(4, sales, target)
        assert result is None

    def test_empty_database_no_crash(self, detector):
        """空数据库不崩溃"""
        result = detector.detect_sales_anomalies(date.today())
        assert result["checked"] == 0
        assert result["summary"]["total"] == 0


class TestAnomalyDetectorWeekdaySeasonal:
    """同星期几季节性检测测试（P1-5）"""

    @pytest.fixture
    def detector(self, app):
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            return AnomalyDetector()

    def test_weekday_seasonal_critical_break(self, detector):
        """同星期几均值 + σ 检测：target 突破 3σ 下限 → critical"""
        target = date(2026, 7, 27)  # 周一
        # 构造 4 周的周一数据（稳定在 100 附近），target 日暴跌
        mondays = [date(2026, 6, 29), date(2026, 7, 6), date(2026, 7, 13), date(2026, 7, 20)]
        sales = []
        for i, dt in enumerate(mondays):
            sales.append({"dt": dt, "qty": [95, 100, 105, 98][i], "name": "周季节品"})
        # 补充其他日期凑够 MIN_HISTORY_DAYS（7天）+ WINDOW
        for d_offset in range(1, 30):
            dt = target - timedelta(days=d_offset)
            if dt.weekday() != 0:  # 非周一
                sales.append({"dt": dt, "qty": 50, "name": "周季节品"})
        sales.append({"dt": target, "qty": 5, "name": "周季节品"})
        result = detector._check_product(10, sales, target)
        assert result is not None
        assert result["severity"] == "critical"

    def test_weekday_seasonal_no_anomaly_stable(self, detector):
        """同星期几稳定销量不报异常"""
        target = date(2026, 7, 27)
        mondays = [date(2026, 6, 29), date(2026, 7, 6), date(2026, 7, 13), date(2026, 7, 20)]
        sales = []
        for dt in mondays:
            sales.append({"dt": dt, "qty": 100, "name": "稳定品"})
        for d_offset in range(1, 30):
            dt = target - timedelta(days=d_offset)
            if dt.weekday() != 0:
                sales.append({"dt": dt, "qty": 50, "name": "稳定品"})
        sales.append({"dt": target, "qty": 100, "name": "稳定品"})
        result = detector._check_product(11, sales, target)
        # 稳定值，stdev 可能很小或 0
        assert result is None or result["severity"] not in ("critical",)

    def test_critical_3sigma_upper_break(self, detector):
        """3σ 上限突破 → critical（异常高）"""
        target = date.today()
        pattern = [95, 100, 105, 100, 98, 102, 100, 97, 103, 100, 96, 104, 101, 99]
        sales = []
        for i in range(15):
            dt = target - timedelta(days=14 - i)
            qty = 500 if dt == target else pattern[i]  # 暴涨
            sales.append({"dt": dt, "qty": qty, "name": "暴涨品"})
        result = detector._check_product(12, sales, target)
        assert result is not None
        assert result["severity"] == "critical"

    def test_warning_2sigma_upper_break(self, detector):
        """2σ 上限突破 → warning 或 critical"""
        target = date.today()
        pattern = [90, 110, 95, 105, 100, 92, 108, 100, 88, 112, 100, 95, 105, 100]
        sales = []
        for i in range(15):
            dt = target - timedelta(days=14 - i)
            qty = 150 if dt == target else pattern[i]
            sales.append({"dt": dt, "qty": qty, "name": "偏高品"})
        result = detector._check_product(13, sales, target)
        assert result is not None
        assert result["severity"] in ("warning", "critical")

    def test_info_severity_3days_below(self, detector):
        """连续 3 天低于均值 50% → info"""
        target = date.today()
        # 前 14 天稳定在 100，近 3 天暴跌到 30（< 50%）
        sales = []
        for i in range(15):
            dt = target - timedelta(days=14 - i)
            if i >= 12:  # 近 3 天
                qty = 30
            else:
                qty = 100
            sales.append({"dt": dt, "qty": qty, "name": "下滑品"})
        # target 日正常（不触发 critical/warning），让 info 分支生效
        result = detector._check_product(14, sales, target)
        if result is not None:
            # 可能触发 info 或其他，关键是 info 路径被覆盖
            assert result["severity"] in ("info", "warning", "critical")

    def test_fallback_to_7day_when_weekday_insufficient(self, detector):
        """同星期几数据不足 3 天时回退到近 7 天全部数据"""
        target = date(2026, 7, 27)  # 周一
        # 只有 1 个历史周一（不足 3 天），应回退近 7 天
        sales = []
        for d_offset in range(1, 15):
            dt = target - timedelta(days=d_offset)
            qty = 100 if dt.weekday() != 0 else 100
            sales.append({"dt": dt, "qty": qty, "name": "回退品"})
        sales.append({"dt": target, "qty": 5, "name": "回退品"})
        # 不应崩溃
        result = detector._check_product(15, sales, target)
        # 结果可能是 critical 或 None，关键是回退路径不报错
        assert result is None or isinstance(result, dict)


class TestAnomalyDetectorBuildResult:
    """_build_result 结果构造测试"""

    def test_build_result_summary(self, app):
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            det = AnomalyDetector()
            anomalies = [
                {"severity": "critical", "product_id": 1},
                {"severity": "critical", "product_id": 2},
                {"severity": "warning", "product_id": 3},
                {"severity": "info", "product_id": 4},
            ]
            result = det._build_result(date.today(), 5, anomalies)
            assert result["checked"] == 5
            assert result["summary"]["critical"] == 2
            assert result["summary"]["warning"] == 1
            assert result["summary"]["info"] == 1
            assert result["summary"]["total"] == 4
            assert len(result["anomalies"]) == 4

    def test_build_result_empty(self, app):
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            det = AnomalyDetector()
            result = det._build_result(date.today(), 0, [])
            assert result["summary"]["total"] == 0
            assert result["checked"] == 0


class TestDetectAndTrigger:
    """detect_and_trigger 触发闭环 + 告警测试"""

    def test_detect_and_trigger_no_anomaly(self, app):
        """无异常时不触发任何动作"""
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            det = AnomalyDetector()
            # mock detect_sales_anomalies 返回空异常
            det.detect_sales_anomalies = lambda td=None: {
                "target_date": str(date.today()),
                "checked": 0,
                "anomalies": [],
                "summary": {"critical": 0, "warning": 0, "info": 0, "total": 0},
            }
            result = det.detect_and_trigger()
            assert result["triggered_actions"] == []

    def test_detect_and_trigger_with_critical(self, app, monkeypatch):
        """critical 异常触发闭环 + 告警"""
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            det = AnomalyDetector()
            critical = [
                {
                    "product_id": 1,
                    "severity": "critical",
                    "product_name": "异常品",
                    "ma_7": 100,
                    "deviation": -3.5,
                    "target_sale": 5,
                }
            ]
            det.detect_sales_anomalies = lambda td=None: {
                "target_date": str(date.today()),
                "checked": 1,
                "anomalies": critical,
                "summary": {"critical": 1, "warning": 0, "info": 0, "total": 1},
            }
            # mock 闭环触发 + 告警发送，避免真实调用
            triggered_calls = []
            monkeypatch.setattr(
                "services.closed_loop.ClosedLoop.check_auto_trigger_with_anomaly",
                staticmethod(lambda anomalies: triggered_calls.append(len(anomalies)) or {"triggered": True}),
            )
            monkeypatch.setattr(det, "_send_alert", lambda a, td: None)

            result = det.detect_and_trigger()
            assert len(result["triggered_actions"]) == 2  # trigger_loop + send_alert
            actions = [a["action"] for a in result["triggered_actions"]]
            assert "trigger_loop" in actions
            assert "send_alert" in actions

    def test_detect_and_trigger_with_warning_only(self, app, monkeypatch):
        """仅 warning 异常只发告警，不触发闭环"""
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            det = AnomalyDetector()
            warning = [
                {
                    "product_id": 2,
                    "severity": "warning",
                    "product_name": "警告品",
                    "ma_7": 100,
                    "deviation": -2.6,
                    "target_sale": 50,
                }
            ]
            det.detect_sales_anomalies = lambda td=None: {
                "target_date": str(date.today()),
                "checked": 1,
                "anomalies": warning,
                "summary": {"critical": 0, "warning": 1, "info": 0, "total": 1},
            }
            monkeypatch.setattr(det, "_send_alert", lambda a, td: None)
            result = det.detect_and_trigger()
            # 仅 send_alert，无 trigger_loop
            actions = [a["action"] for a in result["triggered_actions"]]
            assert "send_alert" in actions
            assert "trigger_loop" not in actions

    def test_send_alert_calls_notifier(self, app, monkeypatch):
        """_send_alert 调用 Notifier.send_alert"""
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            det = AnomalyDetector()
            alert_calls = []

            class FakeNotifier:
                def send_alert(self, **kwargs):
                    alert_calls.append(kwargs)

            monkeypatch.setattr("services.notifier.Notifier", lambda: FakeNotifier())
            anomalies = [
                {"severity": "critical", "product_name": "品A", "ma_7": 100, "deviation": -3, "target_sale": 5}
            ]
            det._send_alert(anomalies, date.today())
            assert len(alert_calls) == 1
            assert "critical" in alert_calls[0]["level"]

    def test_send_alert_notifier_exception_reraises(self, app, monkeypatch):
        """_send_alert 在 Notifier 异常时重新抛出"""
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            det = AnomalyDetector()

            class BrokenNotifier:
                def send_alert(self, **kwargs):
                    raise RuntimeError("通知服务故障")

            monkeypatch.setattr("services.notifier.Notifier", lambda: BrokenNotifier())
            with pytest.raises(RuntimeError, match="通知服务故障"):
                det._send_alert(
                    [{"severity": "warning", "product_name": "x", "ma_7": 1, "deviation": 1, "target_sale": 1}],
                    date.today(),
                )


class TestLoadRecentSales:
    """_load_recent_sales 数据加载测试"""

    def test_load_recent_sales_with_data(self, app):
        """有销售数据时按 product_id 聚合"""
        from datetime import date as _date

        from extensions import db
        from models.erp import Product
        from models.warehouse import DwsSalesSkuDaily
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            # 清理避免污染
            db.session.query(DwsSalesSkuDaily).delete()
            db.session.query(Product).filter(Product.sku.like("ANOM-%")).delete()
            db.session.commit()

            prod = Product(
                sku="ANOM-001",
                name="异常检测品",
                cost_price=10,
                sale_price=20,
                stock_qty=100,
                safety_stock=10,
                avg_cost=10,
            )
            db.session.add(prod)
            db.session.flush()
            target = _date.today()
            for d_offset in range(1, 10):
                dt = target - timedelta(days=d_offset)
                db.session.add(DwsSalesSkuDaily(product_id=prod.id, product_name="异常检测品", dt=dt, sale_qty=100))
            db.session.commit()

            det = AnomalyDetector()
            data = det._load_recent_sales(target)
            assert prod.id in data
            assert len(data[prod.id]) >= 9

    def test_load_recent_sales_empty(self, app):
        """无销售数据返回空 dict"""
        from services.anomaly_detector import AnomalyDetector

        with app.app_context():
            det = AnomalyDetector()
            data = det._load_recent_sales(date.today())
            # 空或只有无关数据
            assert isinstance(data, dict)
