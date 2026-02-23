# core/scheduler.py — 排程邏輯
"""加權隨機間隔計算、時段規則匹配、未回覆概率衰減。"""

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
    timezone=None,
) -> tuple[bool, str]:
    """
    根據未回覆次數與衰減率，判斷是否應觸發主動訊息。

    衰減率查找順序：
    1. 當前時段匹配的 schedule_rule 中的 ``decay_rate``
    2. ``default_decay_rate``（全域預設衰減率）
    3. 若以上皆未配置，回退到 ``max_unanswered_times``（硬性上限）

    衰減公式：觸發概率 = decay_rate ^ unanswered_count
    例如 decay_rate=0.7, unanswered_count=2 → 概率 = 0.7² = 0.49

    Returns:
        (是否觸發, 原因描述)
    """
    if unanswered_count <= 0:
        return True, ""

    # 嘗試從當前時段的排程規則取得 decay_rate
    decay_rate = _resolve_decay_rate(schedule_conf, timezone)

    if decay_rate is not None:
        # decay_rate = 0 → 只觸發一次就停止
        if decay_rate <= 0.0:
            return False, (f"衰減率為 0：未回覆 {unanswered_count} 次，概率為 0%，跳過")

        # 計算衰減後的觸發概率
        probability = decay_rate**unanswered_count
        roll = random.random()
        if roll < probability:
            return True, (
                f"衰減觸發：未回覆 {unanswered_count} 次，"
                f"衰減率 {decay_rate}，概率 {probability:.1%}，擲骰 {roll:.2f}，觸發"
            )
        return False, (
            f"衰減跳過：未回覆 {unanswered_count} 次，"
            f"衰減率 {decay_rate}，概率 {probability:.1%}，擲骰 {roll:.2f}，跳過"
        )

    # 回退到硬性上限
    max_unanswered = schedule_conf.get("max_unanswered_times", 3)
    if max_unanswered > 0 and unanswered_count >= max_unanswered:
        return False, (
            f"硬性上限：未回覆 {unanswered_count} 次，已達上限 {max_unanswered}，暫停"
        )
    return True, ""


def _resolve_decay_rate(schedule_conf: dict, timezone=None) -> float | None:
    """
    解析當前生效的衰減率。

    優先從匹配的時段規則取得，其次使用 default_decay_rate。
    回傳 None 表示未配置任何衰減率。
    """
    now = datetime.now(timezone) if timezone else datetime.now()
    hour = now.hour

    # 1) 嘗試從匹配的時段規則取得 decay_rate
    for rule in schedule_conf.get("schedule_rules", ()):
        if not isinstance(rule, dict):
            continue
        start_h = rule.get("start_hour", 0)
        end_h = rule.get("end_hour", 24)
        if not _hour_in_range(hour, start_h, end_h):
            continue
        raw = (rule.get("decay_rate") or "").strip()
        if raw:
            return _parse_decay_rate(raw)
        break  # 規則匹配但未設定 decay_rate → 繼續查找 default

    # 2) 使用全域預設衰減率
    raw = (schedule_conf.get("default_decay_rate") or "").strip()
    if raw:
        return _parse_decay_rate(raw)

    return None


def _parse_decay_rate(raw: str) -> float | None:
    """解析衰減率字串，回傳 0~1 的浮點數，解析失敗回傳 None。"""
    try:
        val = float(raw)
        return max(0.0, min(1.0, val))
    except (ValueError, TypeError):
        logger.warning(f"{_LOG_TAG} 解析衰減率失敗: {raw!r}，忽略。")
        return None


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
