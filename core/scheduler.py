# core/scheduler.py — 排程邏輯
"""加權隨機間隔計算、時段規則匹配。"""

from __future__ import annotations

import random
from datetime import datetime

from astrbot.api import logger

_LOG_TAG = "[主動訊息]"


def compute_weighted_interval(schedule_conf: dict, timezone=None) -> int:
    """
    根據 ``schedule_settings`` 計算下一次觸發間隔（秒）。

    優先匹配 ``schedule_rules`` 中的時段規則並加權隨機；
    未匹配時回退到全域 min/max 均勻隨機。
    """
    now = datetime.now(timezone) if timezone else datetime.now()
    hour = now.hour

    for rule in schedule_conf.get("schedule_rules", ()):
        if not isinstance(rule, dict):
            continue
        start_h = rule.get("start_hour", 0)
        end_h = rule.get("end_hour", 24)
        if not _hour_in_range(hour, start_h, end_h):
            continue
        weights_str = (rule.get("interval_weights") or "").strip()
        if not weights_str:
            break  # 規則匹配但 weights 為空 → 回退全域
        interval = _pick_from_weights(weights_str)
        if interval is not None:
            logger.debug(
                f"{_LOG_TAG} 命中時段規則 {start_h}-{end_h}，"
                f"加權隨機間隔: {interval // 60} 分鐘。"
            )
            return interval
        break  # 解析失敗 → 回退全域

    # 回退到全域 min/max
    min_s = int(schedule_conf.get("min_interval_minutes", 30)) * 60
    max_s = max(min_s, int(schedule_conf.get("max_interval_minutes", 900)) * 60)
    return random.randint(min_s, max_s)


def should_trigger_by_unanswered(
    unanswered_count: int,
    schedule_conf: dict,
) -> tuple[bool, str]:
    """
    根據未回覆次數與衰減規則，判斷是否應觸發主動訊息。

    優先使用 ``unanswered_decay_rules``（概率衰減規則列表）；
    若未配置衰減規則，則回退到 ``max_unanswered_times``（硬性上限）。

    Returns:
        (是否觸發, 原因描述)
    """
    decay_rules = schedule_conf.get("unanswered_decay_rules", [])

    if decay_rules:
        # 解析衰減規則：按 min_count 排序，找到匹配的區間
        parsed: list[tuple[int, float]] = []
        for rule in decay_rules:
            if not isinstance(rule, dict):
                continue
            min_count = rule.get("min_count", 0)
            prob = rule.get("trigger_probability", 1.0)
            # schema 中 trigger_probability 為 string 類型，需轉換
            try:
                prob = float(prob)
            except (ValueError, TypeError):
                prob = 1.0
            # 將百分比值歸一化到 0~1
            if prob > 1.0:
                prob = prob / 100.0
            prob = max(0.0, min(1.0, prob))
            parsed.append((int(min_count), prob))

        if parsed:
            # 按 min_count 降序排列，找到第一個 <= unanswered_count 的規則
            parsed.sort(key=lambda x: x[0], reverse=True)
            matched_prob = 1.0
            for min_count, prob in parsed:
                if unanswered_count >= min_count:
                    matched_prob = prob
                    break

            if matched_prob <= 0.0:
                return False, f"衰減規則：未回覆 {unanswered_count} 次，概率為 0%，跳過"

            roll = random.random()
            if roll < matched_prob:
                return True, (
                    f"衰減規則：未回覆 {unanswered_count} 次，"
                    f"概率 {matched_prob:.0%}，擲骰 {roll:.2f}，觸發"
                )
            return False, (
                f"衰減規則：未回覆 {unanswered_count} 次，"
                f"概率 {matched_prob:.0%}，擲骰 {roll:.2f}，跳過"
            )

    # 回退到硬性上限
    max_unanswered = schedule_conf.get("max_unanswered_times", 3)
    if max_unanswered > 0 and unanswered_count >= max_unanswered:
        return False, (
            f"硬性上限：未回覆 {unanswered_count} 次，已達上限 {max_unanswered}，暫停"
        )
    return True, ""


def _hour_in_range(current: int, start: int, end: int) -> bool:
    """判斷 *current* 是否在 ``[start, end)``，支援跨日。"""
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _pick_from_weights(weights_str: str) -> int | None:
    """
    解析 ``interval_weights`` 並加權隨機選取間隔（回傳秒數）。

    格式: ``"20-30:0.2,30-50:0.5,50-90:0.3"``
    """
    try:
        buckets: list[tuple[float, float, float]] = []
        for part in weights_str.split(","):
            part = part.strip()
            if not part:
                continue
            range_str, w_str = part.split(":")
            lo_s, hi_s = range_str.split("-")
            lo, hi, w = float(lo_s), float(hi_s), float(w_str)
            if w > 0 and hi > lo:
                buckets.append((lo, hi, w))
        if not buckets:
            return None

        total = sum(w for _, _, w in buckets)
        r = random.uniform(0, total)
        acc = 0.0
        for lo, hi, w in buckets:
            acc += w
            if r <= acc:
                return int(random.uniform(lo, hi) * 60)
        # 兜底
        lo, hi, _ = buckets[-1]
        return int(random.uniform(lo, hi) * 60)
    except Exception as e:
        logger.warning(f"{_LOG_TAG} 解析 interval_weights 失敗: {e}，回退全域間隔。")
        return None
