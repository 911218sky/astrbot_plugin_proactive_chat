# core/scheduler.py — 排程邏輯
"""加權隨機間隔計算、時段規則匹配、未回覆概率衰減。"""

from __future__ import annotations

import random
import zoneinfo
from datetime import datetime

from astrbot.api import logger

_LOG_TAG = "[主動訊息]"


def compute_weighted_interval(
    schedule_conf: dict,
    timezone: zoneinfo.ZoneInfo | None = None,
    unanswered_count: int = 0,
) -> int:
    """
    根據 ``schedule_settings`` 計算下一次觸發間隔（秒）。

    優先匹配 ``schedule_rules`` 中的時段規則並加權隨機；
    未匹配時回退到全域 min/max 均勻隨機。
    支援分鐘級別的時段匹配。

    參數:
        schedule_conf: 排程配置字典
        timezone: 時區資訊
        unanswered_count: 當前未回覆次數（從 0 開始）

    回傳: 下一次觸發間隔（秒）
    """
    now = datetime.now(timezone) if timezone else datetime.now()
    current_hour = now.hour
    current_minute = now.minute

    for rule in schedule_conf.get("schedule_rules", ()):
        if not isinstance(rule, dict):
            continue
        start_h = rule.get("start_hour", 0)
        start_m = rule.get("start_minute", 0)
        end_h = rule.get("end_hour", 24)
        end_m = rule.get("end_minute", 0)

        if not _time_in_range(
            current_hour, current_minute, start_h, start_m, end_h, end_m
        ):
            continue

        weights_str = (rule.get("interval_weights") or "").strip()
        if not weights_str:
            break  # 規則匹配但 weights 為空 → 回退全域
        interval = _pick_from_weights(weights_str, unanswered_count)
        if interval is not None:
            logger.debug(
                f"{_LOG_TAG} 命中時段規則 {start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}，"
                f"加權隨機間隔: {interval // 60} 分鐘（未回覆次數: {unanswered_count + 1}）。"
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
    timezone: zoneinfo.ZoneInfo | None = None,
) -> tuple[bool, str]:
    """
    根據未回覆次數與衰減率，判斷是否應觸發主動訊息。

    衰減率查找順序：
    1. 當前時段匹配的 schedule_rule 中的 ``decay_rate``（逐次列表）
    2. ``default_decay_rate``（全域預設遞減步長）
    3. 若以上皆未配置，回退到 ``max_unanswered_times``（硬性上限）

    時段專屬上限：
    - 若當前時段規則配置了 ``max_unanswered_times`` 且大於 0，將覆蓋全域設定

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

    # 嘗試從當前時段的排程規則取得逐次概率列表和時段專屬上限
    prob_list, matched_rule = _resolve_decay_list_and_rule(schedule_conf, timezone)
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
                return _roll_probability(last_prob, unanswered_count, "全域預設衰減")
            extra = _continue_decay_from(
                last_prob, default_step, idx - len(prob_list) + 1
            )
            return _roll_probability(extra[-1], unanswered_count, "全域預設衰減")

    # 未匹配到任何規則的 decay_rate → 用 default_decay_rate 從 1.0 開始遞減
    if default_step is not None:
        generated = _generate_step_decay_list(default_step, idx + 1)
        if idx < len(generated):
            return _roll_probability(generated[idx], unanswered_count, "全域預設衰減")

    # 回退到硬性上限（優先使用時段專屬上限）
    max_unanswered = schedule_conf.get("max_unanswered_times", 3)
    if matched_rule:
        rule_max = matched_rule.get("max_unanswered_times", 0)
        if rule_max > 0:
            max_unanswered = rule_max
            logger.debug(f"{_LOG_TAG} 使用時段專屬上限: {max_unanswered} 次")

    if max_unanswered > 0 and unanswered_count >= max_unanswered:
        return False, (
            f"硬性上限：未回覆 {unanswered_count} 次，已達上限 {max_unanswered}，暫停"
        )
    return True, ""


def get_time_slot_reset_count(
    schedule_conf: dict, timezone: zoneinfo.ZoneInfo | None = None
) -> int | None:
    """
    取得當前時段的未回覆計數重置值。

    根據時段規則的 ``unanswered_reset_mode`` 決定：
    - ``"inherit"``（繼承）：回傳 None，表示不重置
    - ``"reset"``（重新計數）：回傳 0
    - ``"custom"``（自訂起始值）：回傳 ``unanswered_start_count`` 的值

    Returns:
        重置後的計數值，None 表示不重置（繼承上一時段）
    """
    _, matched_rule = _resolve_decay_list_and_rule(schedule_conf, timezone)
    if not matched_rule:
        return None

    reset_mode = matched_rule.get("unanswered_reset_mode", "inherit")
    if reset_mode == "reset":
        return 0
    if reset_mode == "custom":
        return max(0, int(matched_rule.get("unanswered_start_count", 0)))
    return None  # inherit


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


def _resolve_decay_list(
    schedule_conf: dict, timezone: zoneinfo.ZoneInfo | None = None
) -> list[float] | None:
    """
    解析當前生效的逐次衰減概率列表。

    優先從匹配的時段規則取得，回傳 None 表示未配置。
    注意：此函數不處理 ``default_decay_rate``，由呼叫端自行回退。

    已棄用：請使用 _resolve_decay_list_and_rule 以同時取得匹配的規則。
    """
    result, _ = _resolve_decay_list_and_rule(schedule_conf, timezone)
    return result


def _resolve_decay_list_and_rule(
    schedule_conf: dict, timezone: zoneinfo.ZoneInfo | None = None
) -> tuple[list[float] | None, dict | None]:
    """
    解析當前生效的逐次衰減概率列表，並回傳匹配的規則。

    優先從匹配的時段規則取得，回傳 (None, None) 表示未配置。
    注意：此函數不處理 ``default_decay_rate``，由呼叫端自行回退。
    支援分鐘級別的時段匹配。

    Returns:
        (decay_list, matched_rule)
    """
    now = datetime.now(timezone) if timezone else datetime.now()
    current_hour = now.hour
    current_minute = now.minute

    for rule in schedule_conf.get("schedule_rules", ()):
        if not isinstance(rule, dict):
            continue
        start_h = rule.get("start_hour", 0)
        start_m = rule.get("start_minute", 0)
        end_h = rule.get("end_hour", 24)
        end_m = rule.get("end_minute", 0)

        if not _time_in_range(
            current_hour, current_minute, start_h, start_m, end_h, end_m
        ):
            continue

        raw = (rule.get("decay_rate") or "").strip()
        if raw:
            return _parse_decay_list(raw), rule
        # 規則匹配但未設定 decay_rate → 回傳 None, rule
        return None, rule

    return None, None


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


def _time_in_range(
    current_hour: int,
    current_minute: int,
    start_hour: int,
    start_minute: int,
    end_hour: int,
    end_minute: int,
) -> bool:
    """判斷當前時間是否在指定時段內，支援跨日和分鐘級別精度。

    Args:
        current_hour: 當前小時（0-23）
        current_minute: 當前分鐘（0-59）
        start_hour: 開始小時（0-23）
        start_minute: 開始分鐘（0-59）
        end_hour: 結束小時（0-24）
        end_minute: 結束分鐘（0-59）

    Returns:
        當前時間是否在 [start, end) 區間內
    """
    # 將時間轉換為分鐘數（從午夜 00:00 開始計算）
    current_total = current_hour * 60 + current_minute
    start_total = start_hour * 60 + start_minute
    # end_hour 可能為 24（表示午夜），需特殊處理
    end_total = (end_hour * 60 + end_minute) if end_hour < 24 else 24 * 60

    if start_total <= end_total:
        # 不跨日：例如 08:30 到 23:45
        return start_total <= current_total < end_total
    # 跨日：例如 22:30 到 06:15
    return current_total >= start_total or current_total < end_total


def _hour_in_range(current: int, start: int, end: int) -> bool:
    """判斷 *current* 是否在 ``[start, end)``，支援跨日。

    已棄用：請使用 _time_in_range 以支援分鐘級別精度。
    """
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _pick_from_weights(weights_str: str, unanswered_count: int = 0) -> int | None:
    """
    解析 ``interval_weights`` 並加權隨機選取間隔（回傳秒數）。

    格式: ``"20-30:0.2,30-50:0.5,50-90:0.3"`` （預設分鐘）
    或: ``"30s-60s:0.3,2m-5m:0.5"`` （支援 s=秒, m=分鐘）
    或: ``"5-20:0.6@1-3,20-60:0.2@4+"`` （支援觸發條件：@N 表示第 N 次，@N-M 表示第 N 到 M 次，@N+ 表示第 N 次及以後）

    參數:
        weights_str: 權重配置字串
        unanswered_count: 當前未回覆次數（從 0 開始）

    回傳: 隨機選取的間隔秒數，解析失敗時回傳 None
    """
    try:
        buckets: list[tuple[float, float, float]] = []
        for part in weights_str.split(","):
            part = part.strip()
            if not part:
                continue

            # 檢查是否有觸發條件（@符號）
            if "@" in part:
                weight_part, condition_part = part.split("@", 1)
                # 檢查當前未回覆次數是否符合條件
                if not _match_trigger_condition(
                    unanswered_count, condition_part.strip()
                ):
                    continue  # 不符合條件，跳過此權重配置
            else:
                weight_part = part

            range_str, w_str = weight_part.split(":")
            lo_s, hi_s = range_str.split("-")

            # 解析數值和單位
            lo, lo_unit = _parse_time_value(lo_s.strip())
            hi, hi_unit = _parse_time_value(hi_s.strip())
            w = float(w_str)

            # 統一轉換為秒
            lo_seconds = _to_seconds(lo, lo_unit)
            hi_seconds = _to_seconds(hi, hi_unit)

            if w > 0 and hi_seconds > lo_seconds:
                buckets.append((lo_seconds, hi_seconds, w))

        if not buckets:
            return None

        total = sum(w for _, _, w in buckets)
        r = random.uniform(0, total)
        acc = 0.0
        for lo, hi, w in buckets:
            acc += w
            if r <= acc:
                return int(random.uniform(lo, hi))
        # 兜底
        lo, hi, _ = buckets[-1]
        return int(random.uniform(lo, hi))
    except Exception as e:
        logger.warning(f"{_LOG_TAG} 解析 interval_weights 失敗: {e}，回退全域間隔。")
        return None


def _parse_time_value(value_str: str) -> tuple[float, str]:
    """
    解析時間值和單位。

    範例:
        "30" -> (30.0, "m")  # 預設分鐘
        "30s" -> (30.0, "s")
        "2m" -> (2.0, "m")
        "0.5" -> (0.5, "m")

    回傳: (數值, 單位)，單位為 "s" 或 "m"
    """
    value_str = value_str.strip()
    if value_str.endswith("s"):
        return (float(value_str[:-1]), "s")
    elif value_str.endswith("m"):
        return (float(value_str[:-1]), "m")
    else:
        # 預設為分鐘（向下相容）
        return (float(value_str), "m")


def _to_seconds(value: float, unit: str) -> float:
    """
    將時間值轉換為秒。

    參數:
        value: 時間數值
        unit: 單位 ("s" 或 "m")

    回傳: 秒數
    """
    if unit == "s":
        return value
    elif unit == "m":
        return value * 60
    else:
        # 預設為分鐘
        return value * 60


def _match_trigger_condition(unanswered_count: int, condition: str) -> bool:
    """
    檢查未回覆次數是否符合觸發條件。

    支援格式:
        - "N": 只在第 N 次時觸發（如 "1" 表示第 1 次）
        - "N-M": 在第 N 到 M 次時觸發（如 "1-3" 表示第 1、2、3 次）
        - "N+": 在第 N 次及以後觸發（如 "4+" 表示第 4 次及以後）

    參數:
        unanswered_count: 當前未回覆次數（從 0 開始，第 1 次未回覆時為 0）
        condition: 觸發條件字串

    回傳: 是否符合條件
    """
    try:
        condition = condition.strip()

        # 格式: "N+" 表示第 N 次及以後
        if condition.endswith("+"):
            min_count = int(condition[:-1])
            # 注意：unanswered_count 從 0 開始，所以第 1 次未回覆時 unanswered_count=0
            # 條件 "1+" 表示第 1 次及以後，即 unanswered_count >= 0
            # 條件 "4+" 表示第 4 次及以後，即 unanswered_count >= 3
            return unanswered_count >= (min_count - 1)

        # 格式: "N-M" 表示第 N 到 M 次
        if "-" in condition:
            start_str, end_str = condition.split("-", 1)
            start = int(start_str)
            end = int(end_str)
            # 第 1 次對應 unanswered_count=0，第 M 次對應 unanswered_count=M-1
            return (start - 1) <= unanswered_count <= (end - 1)

        # 格式: "N" 表示只在第 N 次
        exact = int(condition)
        return unanswered_count == (exact - 1)

    except (ValueError, IndexError) as e:
        logger.warning(
            f"{_LOG_TAG} 解析觸發條件失敗: {condition}，錯誤: {e}，預設為符合條件。"
        )
        return True  # 解析失敗時預設為符合條件，避免影響正常運作
