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
    1. 當前時段匹配的 schedule_rule 中的 ``decay_rate``（逐次列表）
    2. ``default_decay_rate``（全域預設遞減步長）
    3. 若以上皆未配置，回退到 ``max_unanswered_times``（硬性上限）

    ``decay_rate`` 格式：逗號分隔的概率列表，每個值對應第 N 次未回覆的觸發概率。
    例如 ``"0.8,0.5,0.3,0.15"``：第 1 次 → 80%、第 2 次 → 50%、第 3 次 → 30%、第 4 次 → 15%。

    ``default_decay_rate`` 為遞減步長（0~1）：
    - 當 ``decay_rate`` 列表用盡時，從列表最後一個值開始，每次遞減此步長。
      例如列表為 ``"1,0.9,0.8"``、步長為 ``0.05``，則第 4 次 → 75%、第 5 次 → 70%…
    - 當未匹配到任何規則時，從 1.0 開始每次遞減此步長。
      例如步長 ``0.05`` → 第 1 次 100%、第 2 次 95%、第 3 次 90%…
    - 填 ``0`` 表示不衰減（維持 100% 或列表末尾概率）。
    - 留空表示不使用遞減衰減（回退到硬性上限邏輯）。

    Returns:
        (是否觸發, 原因描述)
    """
    if unanswered_count <= 0:
        return True, ""

    # 嘗試從當前時段的排程規則取得逐次概率列表
    prob_list = _resolve_decay_list(schedule_conf, timezone)
    idx = unanswered_count - 1  # 第 1 次未回覆 → index 0

    # 解析全域預設遞減步長（提前解析，後續多處使用）
    default_step = _parse_single_decay(
        (schedule_conf.get("default_decay_rate") or "").strip()
    )

    if prob_list is not None:
        if idx < len(prob_list):
            # 仍在 decay_rate 列表範圍內
            probability = prob_list[idx]
            return _roll_probability(probability, unanswered_count, "衰減")
        # 列表用盡 → 以 default_decay_rate 步長從列表末尾值接續遞減
        if default_step is not None:
            last_prob = prob_list[-1]
            if default_step <= 0.0:
                # step=0 → 不衰減，維持列表末尾概率
                return _roll_probability(
                    last_prob, unanswered_count, "全域預設衰減"
                )
            extra = _continue_decay_from(
                last_prob, default_step, idx - len(prob_list) + 1
            )
            return _roll_probability(
                extra[-1], unanswered_count, "全域預設衰減"
            )

    # 未匹配到任何規則的 decay_rate → 用 default_decay_rate 從 1.0 開始遞減
    if default_step is not None:
        generated = _generate_step_decay_list(default_step, idx + 1)
        if idx < len(generated):
            return _roll_probability(
                generated[idx], unanswered_count, "全域預設衰減"
            )

    # 回退到硬性上限
    max_unanswered = schedule_conf.get("max_unanswered_times", 3)
    if max_unanswered > 0 and unanswered_count >= max_unanswered:
        return False, (
            f"硬性上限：未回覆 {unanswered_count} 次，已達上限 {max_unanswered}，暫停"
        )
    return True, ""


def _roll_probability(
    probability: float, unanswered_count: int, label: str
) -> tuple[bool, str]:
    """根據概率擲骰判定是否觸發，回傳 (是否觸發, 原因描述)。"""
    if probability <= 0.0:
        return False, (f"{label}率為 0：未回覆第 {unanswered_count} 次，概率 0%，跳過")
    if probability >= 1.0:
        return True, (f"{label}觸發：未回覆第 {unanswered_count} 次，概率 100%，觸發")
    roll = random.random()
    if roll < probability:
        return True, (
            f"{label}觸發：未回覆第 {unanswered_count} 次，"
            f"概率 {probability:.0%}，擲骰 {roll:.2f}，觸發"
        )
    return False, (
        f"{label}跳過：未回覆第 {unanswered_count} 次，"
        f"概率 {probability:.0%}，擲骰 {roll:.2f}，跳過"
    )


def _resolve_decay_list(schedule_conf: dict, timezone=None) -> list[float] | None:
    """
    解析當前生效的逐次衰減概率列表。

    優先從匹配的時段規則取得，回傳 None 表示未配置。
    注意：此函數不處理 ``default_decay_rate``，由呼叫端自行回退。
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
        raw = (rule.get("decay_rate") or "").strip()
        if raw:
            return _parse_decay_list(raw)
        break  # 規則匹配但未設定 decay_rate → 回傳 None

    return None


def _parse_decay_list(raw: str) -> list[float] | None:
    """
    解析逐次衰減概率列表字串。

    格式：逗號分隔的 0~1 浮點數，如 ``"0.7,1,1,0.6"``。
    每個值對應第 N 次未回覆的觸發概率。
    解析失敗回傳 None。
    """
    try:
        result = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            val = float(part)
            result.append(max(0.0, min(1.0, val)))
        return result if result else None
    except (ValueError, TypeError):
        logger.warning(f"{_LOG_TAG} 解析衰減率列表失敗: {raw!r}，忽略。")
        return None


def _parse_single_decay(raw: str) -> float | None:
    """解析單一衰減率字串，回傳 0~1 的浮點數，解析失敗回傳 None。"""
    if not raw:
        return None
    try:
        val = float(raw)
        return max(0.0, min(1.0, val))
    except (ValueError, TypeError):
        logger.warning(f"{_LOG_TAG} 解析衰減率失敗: {raw!r}，忽略。")
        return None


def _continue_decay_from(
    last_prob: float, step: float, extra_count: int
) -> list[float]:
    """
    從 *last_prob* 開始，以 *step* 為遞減步長，生成 *extra_count* 個後續概率。

    用於 decay_rate 列表用盡後，以 default_decay_rate 步長接續遞減。
    概率下限為 0.0。
    """
    result: list[float] = []
    prob = last_prob
    for _ in range(extra_count):
        prob -= step
        result.append(max(0.0, round(prob, 10)))
    return result


def _generate_step_decay_list(step: float, min_length: int) -> list[float]:
    """
    根據遞減步長從 1.0 開始生成衰減概率列表。

    例如 step=0.05 → [1.0, 0.95, 0.9, 0.85, ...]，直到概率 ≤ 0。
    至少生成 *min_length* 個元素（不足時以 0.0 補齊）。
    step=0 時視為不衰減，回傳全 1.0 列表。
    """
    if step <= 0.0:
        return [1.0] * max(min_length, 1)
    result: list[float] = []
    prob = 1.0
    while prob > 0.0:
        result.append(round(prob, 10))
        prob -= step
    while len(result) < min_length:
        result.append(0.0)
    return result



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
