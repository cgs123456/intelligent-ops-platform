"""需求预测内核（P2-5）
==============================
补货测算从"近7日均销量"升级为"加权移动平均 + 阻尼趋势"的轻量预测：

- 加权移动平均（WMA）：越近的日期权重越大，比简单均值更快响应需求变化
- 线性趋势：对窗口做最小二乘求斜率，乘阻尼系数 0.5 叠加（防止外推过猛）
- 缺失日按 0 销量补齐；窗口起点裁剪到首个有销量的日期，避免冷启动期 0 值稀释预测
- 置信度：由波动率（变异系数 CV）与数据覆盖度共同决定

纯函数、无 DB 依赖，便于单元测试；ADS ETL 与未来的 MRP 可复用。
"""

import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

# 趋势阻尼系数：外推越猛风险越大，取一半斜率
_TREND_DAMPING = 0.5
# 置信度分档：CV（变异系数）越低需求越稳定
_CV_CONFIDENCE = ((0.3, 0.9), (0.6, 0.75))  # CV < 0.3 → 0.9；CV < 0.6 → 0.75；否则 0.55
_LOW_CONFIDENCE = 0.55


def _linear_slope(values):
    """最小二乘斜率（件/天）。values 按时间升序。"""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, values, strict=True)) / denom


def forecast_daily_demand(series, target_date=None, window_days=14):
    """预测未来日均需求。

    :param series: [(date, qty), ...] 历史日销量，可稀疏（缺失日期按 0 补齐）
    :param target_date: 数据截止日（默认取序列最大日期）
    :param window_days: 回看窗口天数（默认 14）
    :return: {
        "forecast": float 预测日均需求（>=0）,
        "method": str "weighted_ma_trend" / "weighted_ma" / "insufficient_data",
        "confidence": float 0~1,
        "trend": float 阻尼后的趋势分量（件/天）,
        "recent_7d_sales": int 近 7 日销量合计（保持 ADS 列语义）,
        "samples": int 窗口内有销量的天数,
    }
    """
    if not series:
        return {
            "forecast": 0.0,
            "method": "insufficient_data",
            "confidence": 0.3,
            "trend": 0.0,
            "recent_7d_sales": 0,
            "samples": 0,
        }

    if target_date is None:
        target_date = max(d for d, _ in series)

    by_date = {d: int(q or 0) for d, q in series if d <= target_date}
    # 窗口按连续日期补齐（缺失 = 0 销量），days[0] 最旧、days[-1] = target_date
    days = [by_date.get(target_date - timedelta(days=i), 0) for i in range(window_days - 1, -1, -1)]

    # 裁剪冷启动：首个有销量日期之前的全 0 前缀不参与预测（避免上线初期稀释均值），
    # 数据长于窗口时只保留最近 window_days 天
    first_sale = next((i for i, q in enumerate(days) if q > 0), None)
    if first_sale is None:
        return {
            "forecast": 0.0,
            "method": "insufficient_data",
            "confidence": 0.3,
            "trend": 0.0,
            "recent_7d_sales": 0,
            "samples": 0,
        }
    days = days[first_sale:]
    if len(days) > window_days:
        days = days[-window_days:]

    recent_7d = sum(days[-7:])
    samples = sum(1 for q in days if q > 0)
    mean = sum(days) / len(days)

    # 数据不足 / 无需求：退化为窗口均值（等价旧行为的均值口径）
    if samples < 3 or mean <= 0:
        return {
            "forecast": round(max(0.0, mean), 2),
            "method": "insufficient_data",
            "confidence": 0.3,
            "trend": 0.0,
            "recent_7d_sales": recent_7d,
            "samples": samples,
        }

    n = len(days)
    weights = list(range(1, n + 1))
    wma = sum(q * w for q, w in zip(days, weights, strict=True)) / sum(weights)
    trend = _linear_slope(days) * _TREND_DAMPING
    forecast = max(0.0, wma + trend)

    # 置信度 = 波动率分档 × 数据覆盖度
    variance = sum((q - mean) ** 2 for q in days) / len(days)
    cv = (variance**0.5) / mean if mean > 0 else 1.0
    confidence = _LOW_CONFIDENCE
    for threshold, conf in _CV_CONFIDENCE:
        if cv < threshold:
            confidence = conf
            break
    coverage_cap = min(1.0, samples / max(1, min(7, len(days))))
    confidence = round(confidence * coverage_cap, 2)

    method = "weighted_ma_trend" if abs(trend) > 0.05 * max(1.0, wma) else "weighted_ma"
    return {
        "forecast": round(forecast, 2),
        "method": method,
        "confidence": confidence,
        "trend": round(trend, 2),
        "recent_7d_sales": recent_7d,
        "samples": samples,
    }


# 服务水位 → 正态分位 z 值（统计安全库存 SS = z × σ_日需求 × √提前期）
_Z_TABLE = {0.90: 1.2816, 0.95: 1.6449, 0.98: 2.0537, 0.99: 2.3263}


def safety_stock_recommendation(daily_series, lead_days, service_level=0.95, window_days=90):
    """基于需求波动与交期的统计安全库存建议。

    SS = z × σ_日需求 × √lead_days（离散需求的正态近似，业界通用口径，
    参照 stockpyl/Odoo/ERPNext 的 orderpoint 实践）。σ 直接取窗口内
    含零日的日销量标准差——缺销日也是真实的需求实现，不能用有销日子集
    低估波动。

    :param daily_series: [(date, qty), ...] 历史日销量（可稀疏，缺失按 0）
    :param lead_days: 补货提前期（天），<=0 视为 1
    :param service_level: 服务水位，仅支持 0.90/0.95/0.98/0.99
    :param window_days: 波动率回看窗口（默认 90 天）
    :return: {
        "safety_stock": int 建议安全库存（>=0）,
        "sigma_daily": float 日需求标准差,
        "z": float 分位值,
        "service_level": float 原样返回,
        "samples": int 窗口天数（含补零日）,
    }
    :raises ValueError: service_level 不在支持列表内
    """
    if service_level not in _Z_TABLE:
        raise ValueError(f"不支持的服务水位 {service_level}，可选：{sorted(_Z_TABLE)}")
    z = _Z_TABLE[service_level]

    if not daily_series:
        return {"safety_stock": 0, "sigma_daily": 0.0, "z": z, "service_level": service_level, "samples": 0}

    target_date = max(d for d, _ in daily_series)
    by_date = {d: int(q or 0) for d, q in daily_series if d <= target_date}
    days = [by_date.get(target_date - timedelta(days=i), 0) for i in range(window_days - 1, -1, -1)]

    n = len(days)
    mean = sum(days) / n
    variance = sum((q - mean) ** 2 for q in days) / n
    sigma = variance**0.5
    lead = max(1, int(lead_days or 1))
    ss = z * sigma * (lead**0.5)

    return {
        "safety_stock": max(0, int(round(ss))),
        "sigma_daily": round(sigma, 2),
        "z": z,
        "service_level": service_level,
        "samples": n,
    }
