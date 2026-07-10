"""
FDE 时序异常检测（改进9）
========================
在现有闭环触发条件（手动 run_step + 安全库存阈值）基础上升级：

1. 读取 dws_sales_sku_daily 近 30 天销售数据
2. 计算 7 日移动平均 + 2σ 上/下限
3. 当前销量突破下限 → 自动触发闭环补货 + notifier.send_alert()
4. 通过 tasks.py 的 Celery Beat 定时任务每小时执行一次

设计要点：
- 独立模块，避免修改 warehouse_service.py（687 行）
- 复用现有 extensions.db / Notifier / ClosedLoop
- 数据不足（<7 天）时跳过，不报错
- 异常分级：critical（突破 3σ）/ warning（突破 2σ）/ info（连续 3 天低于均值）
"""
import logging
import statistics
from collections import defaultdict
from datetime import date, timedelta

logger = logging.getLogger(__name__)


class AnomalyDetector:
    """销售时序异常检测：7 日移动平均 + 2σ 上下限。"""

    # 移动平均窗口
    WINDOW = 7
    # 标准差倍数（2σ 为 warning，3σ 为 critical）
    SIGMA_WARNING = 2.0
    SIGMA_CRITICAL = 3.0
    # 最少历史天数（不足则跳过）
    MIN_HISTORY_DAYS = 7
    # 检测窗口（最近 N 天作为历史）
    LOOKBACK_DAYS = 30
    # 单次检测最大产品数（避免长时间阻塞）
    MAX_PRODUCTS = 200

    def __init__(self, db_session=None):
        from extensions import db
        self._db_session = db_session
        self.session = db_session or db.session

    # ---------------- 主入口 ----------------

    def detect_sales_anomalies(self, target_date=None):
        """检测所有产品的销售时序异常。

        :param target_date: 检测日期（默认今天，取该日销量与历史 7 日均值比对）
        :return: {
            target_date, checked, anomalies: [...],
            summary: {critical, warning, info, total}
        }
        """
        target_date = target_date or date.today()
        anomalies = []

        # 1. 拉取近 30 天销售数据
        sales_data = self._load_recent_sales(target_date)
        if not sales_data:
            logger.info('[Anomaly] 无销售数据，跳过检测 target=%s', target_date)
            return self._build_result(target_date, 0, anomalies)

        # 2. 逐产品计算 7 日移动平均 + 2σ
        checked = 0
        for product_id, daily_sales in sales_data.items():
            if checked >= self.MAX_PRODUCTS:
                logger.warning('[Anomaly] 达到单次检测上限 %d，剩余产品跳过', self.MAX_PRODUCTS)
                break
            checked += 1
            anomaly = self._check_product(product_id, daily_sales, target_date)
            if anomaly:
                anomalies.append(anomaly)

        # 3. 按严重程度排序
        severity_order = {'critical': 0, 'warning': 1, 'info': 2}
        anomalies.sort(key=lambda a: (severity_order.get(a['severity'], 3), -a.get('deviation', 0)))

        logger.info('[Anomaly] 检测完成 target=%s checked=%d anomalies=%d',
                    target_date, checked, len(anomalies))
        return self._build_result(target_date, checked, anomalies)

    # ---------------- 数据加载 ----------------

    def _load_recent_sales(self, target_date):
        """加载近 30 天所有产品的日销量。

        :return: {product_id: [(dt, sale_qty), ...]} 按 dt 升序
        """
        from models.warehouse import DwsSalesSkuDaily
        start = target_date - timedelta(days=self.LOOKBACK_DAYS - 1)
        rows = (
            self.session.query(
                DwsSalesSkuDaily.product_id,
                DwsSalesSkuDaily.product_name,
                DwsSalesSkuDaily.dt,
                DwsSalesSkuDaily.sale_qty,
            )
            .filter(DwsSalesSkuDaily.dt >= start)
            .filter(DwsSalesSkuDaily.dt <= target_date)
            .order_by(DwsSalesSkuDaily.product_id, DwsSalesSkuDaily.dt)
            .all()
        )
        data = defaultdict(list)
        for r in rows:
            data[r.product_id].append({
                'dt': r.dt, 'qty': r.sale_qty or 0, 'name': r.product_name
            })
        return data

    # ---------------- 单产品检测 ----------------

    def _check_product(self, product_id, daily_sales, target_date):
        """对单个产品做异常检测。

        :param daily_sales: [{'dt', 'qty', 'name'}, ...] 按 dt 升序
        :return: anomaly dict 或 None
        """
        if len(daily_sales) < self.MIN_HISTORY_DAYS:
            return None  # 历史数据不足

        # 取目标日销量
        target_sale = next((d['qty'] for d in daily_sales if d['dt'] == target_date), None)
        product_name = daily_sales[-1].get('name') or f'产品{product_id}'

        # 用 target_date 之前 7 天计算移动平均 + σ（不含 target_date 本身，避免泄露）
        history_before = [d['qty'] for d in daily_sales if d['dt'] < target_date]
        if len(history_before) < self.WINDOW:
            return None
        recent_7 = history_before[-self.WINDOW:]
        mean = statistics.mean(recent_7)
        stdev = statistics.stdev(recent_7) if len(recent_7) > 1 else 0
        # 如果均值为 0 或 σ 为 0，跳过（无法判断异常）
        if mean <= 0 or stdev <= 0:
            return None

        # ---------------- 异常判定 ----------------
        severity = None
        deviation = 0.0
        reason = ''

        if target_sale is not None:
            deviation = (target_sale - mean) / stdev
            # 突破 3σ 下限 → critical
            if deviation <= -self.SIGMA_CRITICAL:
                severity = 'critical'
                reason = (f'{target_date} 销量 {target_sale} 突破 3σ 下限'
                          f'（7日均值 {mean:.1f}，3σ 下限 {mean - self.SIGMA_CRITICAL * stdev:.1f}）')
            # 突破 2σ 下限 → warning
            elif deviation <= -self.SIGMA_WARNING:
                severity = 'warning'
                reason = (f'{target_date} 销量 {target_sale} 突破 2σ 下限'
                          f'（7日均值 {mean:.1f}，2σ 下限 {mean - self.SIGMA_WARNING * stdev:.1f}）')
            # 突破 3σ 上限 → critical（异常高，可能是刷单或数据错误）
            elif deviation >= self.SIGMA_CRITICAL:
                severity = 'critical'
                reason = (f'{target_date} 销量 {target_sale} 突破 3σ 上限'
                          f'（7日均值 {mean:.1f}，3σ 上限 {mean + self.SIGMA_CRITICAL * stdev:.1f}）')
            # 突破 2σ 上限 → warning
            elif deviation >= self.SIGMA_WARNING:
                severity = 'warning'
                reason = (f'{target_date} 销量 {target_sale} 突破 2σ 上限'
                          f'（7日均值 {mean:.1f}，2σ 上限 {mean + self.SIGMA_WARNING * stdev:.1f}）')

        # 连续 3 天低于均值 50% → info（销量下滑趋势）
        if severity is None:
            recent_3 = history_before[-3:] if len(history_before) >= 3 else []
            if len(recent_3) == 3 and mean > 0 and all(q < mean * 0.5 for q in recent_3):
                severity = 'info'
                deviation = (sum(recent_3) / 3 - mean) / mean
                reason = (f'连续 3 天销量低于均值 50%'
                          f'（近3日 {recent_3}，均值 {mean:.1f}）')

        if severity is None:
            return None

        return {
            'product_id': product_id,
            'product_name': product_name,
            'target_date': str(target_date),
            'target_sale': target_sale,
            'ma_7': round(mean, 2),
            'stdev_7': round(stdev, 2),
            'deviation': round(deviation, 2),
            'severity': severity,
            'reason': reason,
        }

    # ---------------- 结果构造 ----------------

    def _build_result(self, target_date, checked, anomalies):
        summary = {
            'critical': sum(1 for a in anomalies if a['severity'] == 'critical'),
            'warning': sum(1 for a in anomalies if a['severity'] == 'warning'),
            'info': sum(1 for a in anomalies if a['severity'] == 'info'),
            'total': len(anomalies),
        }
        return {
            'target_date': str(target_date),
            'checked': checked,
            'anomalies': anomalies,
            'summary': summary,
        }

    # ---------------- 触发闭环 + 告警 ----------------

    def detect_and_trigger(self, target_date=None):
        """检测异常并自动触发闭环补货 + 发送告警。

        - critical 异常：触发闭环 step 1（生成补货建议）
        - warning 异常：发送告警，不触发闭环（避免频繁触发）
        - info 异常：仅记录日志
        :return: detect_result + triggered_actions
        """
        result = self.detect_sales_anomalies(target_date)

        triggered = []
        critical_anomalies = [a for a in result['anomalies'] if a['severity'] == 'critical']
        warning_anomalies = [a for a in result['anomalies'] if a['severity'] == 'warning']

        # 1. critical 异常 → 触发闭环补货
        if critical_anomalies:
            try:
                from services.closed_loop import ClosedLoop
                trigger_result = ClosedLoop.check_auto_trigger_with_anomaly(critical_anomalies)
                triggered.append({
                    'action': 'trigger_loop',
                    'reason': f'{len(critical_anomalies)} 个 critical 异常',
                    'result': trigger_result,
                })
            except Exception as e:
                logger.error('[Anomaly] 触发闭环失败: %s', e)
                triggered.append({'action': 'trigger_loop', 'error': str(e)})

        # 2. warning + critical 异常 → 发送告警
        alert_anomalies = critical_anomalies + warning_anomalies
        if alert_anomalies:
            try:
                self._send_alert(alert_anomalies, result['target_date'])
                triggered.append({
                    'action': 'send_alert',
                    'reason': f'{len(alert_anomalies)} 个异常已告警',
                })
            except Exception as e:
                logger.error('[Anomaly] 发送告警失败: %s', e)
                triggered.append({'action': 'send_alert', 'error': str(e)})

        result['triggered_actions'] = triggered
        return result

    def _send_alert(self, anomalies, target_date):
        """通过 Notifier 发送多渠道告警。"""
        try:
            from services.notifier import Notifier
            notifier = Notifier()

            # 构造告警消息（markdown 格式，适合钉钉/企业微信）
            lines = [f'## 销售时序异常告警（{target_date}）']
            lines.append(f'检测到 **{len(anomalies)}** 个异常产品：\n')
            for a in anomalies[:10]:  # 最多展示 10 个
                emoji = {'critical': '🔴', 'warning': '🟡'}.get(a['severity'], '⚪')
                lines.append(
                    f"- {emoji} **{a['product_name']}**（{a['severity']}）"
                    f"  销量 {a.get('target_sale', 'N/A')}，"
                    f"7日均值 {a['ma_7']}，偏离 {a['deviation']}σ"
                )
            if len(anomalies) > 10:
                lines.append(f'\n...共 {len(anomalies)} 个，仅展示前 10 个')
            message = '\n'.join(lines)

            level = 'critical' if any(a['severity'] == 'critical' for a in anomalies) else 'warning'
            notifier.send_alert(
                title=f'销售异常告警 - {target_date}',
                message=message,
                level=level,
                context={'anomaly_count': len(anomalies), 'target_date': str(target_date)},
            )
            logger.info('[Anomaly] 告警已发送，异常数=%d', len(anomalies))
        except Exception as e:
            logger.error('[Anomaly] 告警发送异常: %s', e)
            raise
