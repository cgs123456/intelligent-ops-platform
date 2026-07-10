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
            sales.append({'dt': dt, 'qty': 100, 'name': '正常品'})
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
            sales.append({'dt': dt, 'qty': qty, 'name': '异常品A'})
        result = detector._check_product(2, sales, target)
        assert result is not None
        assert result['severity'] == 'critical'

    def test_warning_2sigma_break(self, detector):
        """2σ 下限突破 → warning 或 critical"""
        target = date.today()
        pattern = [90, 110, 95, 105, 100, 92, 108, 100, 88, 112, 100, 95, 105, 100]
        sales = []
        for i in range(15):
            dt = target - timedelta(days=14 - i)
            qty = 75 if dt == target else pattern[i]
            sales.append({'dt': dt, 'qty': qty, 'name': '异常品B'})
        result = detector._check_product(3, sales, target)
        assert result is not None
        assert result['severity'] in ('warning', 'critical')

    def test_insufficient_history_skipped(self, detector):
        """历史数据不足 7 天 → 跳过"""
        target = date.today()
        sales = [
            {'dt': target - timedelta(days=1), 'qty': 100, 'name': '新品'},
            {'dt': target - timedelta(days=2), 'qty': 95, 'name': '新品'},
            {'dt': target, 'qty': 10, 'name': '新品'},
        ]
        result = detector._check_product(4, sales, target)
        assert result is None

    def test_empty_database_no_crash(self, detector):
        """空数据库不崩溃"""
        result = detector.detect_sales_anomalies(date.today())
        assert result['checked'] == 0
        assert result['summary']['total'] == 0
