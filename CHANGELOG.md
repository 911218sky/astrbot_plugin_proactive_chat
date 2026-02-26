<!-- markdownlint-disable MD024 -->
<!-- markdownlint-disable MD025 -->
<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD034 -->
<!-- markdownlint-disable MD041 -->
# ChangeLog

# 2026/02/26 v2.8.3

## What's Changed

### 重構 (Refactor)

- **型別標註改進**：將 `core/chat_executor.py` 與 `core/context_scheduling.py` 中所有函數參數的型別標註從 `Any` 改為 `ProactiveChatPlugin`，提升型別安全性與 IDE 支援。

---

# 2026/02/26 v2.8.2

## What's Changed

### 重構 (Refactor)

- **模組拆分**：從 `main.py` 中提取 `core/context_scheduling.py`（語境感知排程）與 `core/chat_executor.py`（核心執行流程），`main.py` 從約 1450 行精簡至約 830 行。
- 拆分後的模組以獨立函數形式接收插件實例，保持解耦的同時避免大量參數傳遞。

---

# 2026/02/26 v2.8.1

## What's Changed

### 優化 (Opt)

- **語境預測 prompt 改進**：移除固定延遲範圍，改為讓 LLM 根據對話上下文中的作息線索（上課、上班等）動態推斷最佳跟進時機。

---

# 2026/02/26 v2.8.0

## What's Changed

### 新增 (Feat)

- **`/proactive_tasks` 指令**：新增聊天指令，可即時查看所有待執行的主動訊息排程，包含一般排程任務、語境預測任務及未追蹤的語境排程。

---

# 2026/02/26 v2.7.1

## What's Changed

### 文件 (Docs)

- 更新 README.md / README_EN.md / README_JP.md，補充多語境任務並行、並行取消檢查等說明。
- 新增 `{{last_reply_time}}` Prompt 佔位符文件。

---

# 2026/02/25 v2.6.0

## What's Changed

### 新增 (Feat)

- **多語境任務並行**：同一會話現在可同時存在多個語境預測任務（如短期跟進 + 長期早安問候），不再互相覆蓋。
- 用戶發新訊息時會並行檢查所有已排定的語境任務是否應取消，避免逐一等待 LLM 回應。
- 語境任務使用唯一 `job_id`，支援獨立追蹤與移除。

---

# 2026/02/25 v2.5.1

## What's Changed

### 優化 (Opt)

- **語境預測日誌增強**：日誌中新增預計觸發時間與排程決策資訊，方便除錯與觀察。

---

# 2026/02/25 v2.5.0

## What's Changed

### 新增 (Feat)

- **語境感知獨立 LLM 平台**：新增 `llm_provider_id` 設定，可為語境預測指定不同於主對話的 LLM 平台（例如用較便宜的模型做預測）。
- **額外 Prompt 注入**：新增 `extra_prompt` 設定，可在語境預測時附加自訂指令。
- **記憶檢索開關**：新增 `enable_memory` 設定，可獨立控制是否在主動訊息中注入 livingmemory 記憶。
---

# 2026/02/24 v2.4.0

## What's Changed

### 新增 (Feat)

- **`default_decay_rate` 遞減步長模式**：`default_decay_rate` 從固定指數衰減改為遞減步長機制。有 `decay_rate` 列表時從末尾值接續遞減，無列表時從 1.0 開始遞減。填 `0` 表示不衰減，留空則回退到硬性上限邏輯。

---

# 2026/02/24 v2.3.0

## What's Changed

### 重構 (Refactor)

- **Prompt 模板外部化**：將語境預測與任務取消判斷的 LLM Prompt 從程式碼中提取至 `core/prompts/` 目錄下的獨立 `.txt` 檔案，方便調整而不需改動程式碼。
- 為 `core/send.py` 補充型別提示。

---

# 2026/02/24 v2.2.1

## What's Changed

### 修復 (Fix)

- 修復 README.md 中損壞的 Unicode 字元（亂碼 Emoji）。

---

# 2026/02/24 v2.2.0

## What's Changed

### 新增 (Feat)

- **livingmemory 記憶整合**：主動訊息生成時可從 [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 檢索相關長期記憶，注入到 system_prompt 中，讓 LLM 生成更貼合用戶歷史的主動訊息。
- **模組拆分**：從 `main.py` 提取 `core/llm_helpers.py`（LLM 請求準備、記憶檢索、呼叫封裝）與 `core/send.py`（TTS / 文字 / 分段發送邏輯）。

---

# 2026/02/24 v2.1.0

## What's Changed

### 新增 (Feat)

- **語境感知排程**：新增 `context_aware_settings`，啟用後每次用戶發訊息時會呼叫 LLM 分析對話語境，動態預測下一次主動訊息的最佳時機（例如用戶說「晚安」 7-9 小時後早安問候）。
- **逐次概率衰減**：`decay_rate` 從指數衰減改為逐次概率列表（如 `0.8,0.5,0.3,0.15`），每個值對應第 N 次未回覆的觸發概率，控制更精細。
- 新增 `core/context_predictor.py` 模組，負責 LLM 預測主動訊息時機與任務取消判斷。
- 更新 `interval_weights` 預設值為更貼近真人的隨機模式。
- 修正 `end_hour` 滑桿最大值從 23 改為 24。

---

# 2026/02/23 v2.0.0

## What's Changed

>  此版本為破壞性更新，配置格式與 v1.x 不相容，需重新設定。

### 架構 (Architecture)

- **模組化重構**：將原本集中在 `main.py` 的所有邏輯拆分為 `core/` 子模組架構：
  - `core/utils.py`  通用工具：免打擾判斷、UMO 解析、日誌格式化
  - `core/config.py`  配置管理：驗證、會話配置查詢、備份
  - `core/scheduler.py`  排程邏輯：加權隨機間隔、時段規則匹配
  - `core/messaging.py`  訊息發送：裝飾鉤子、分段回覆、歷史清洗
- 所有模組使用相對匯入，避免與 AstrBot 自身的 `core` 衝突。
- 插件主類使用 `__slots__` 減少記憶體開銷。

### 新增 (Feat)

- **`schedule_rules` 分時段加權排程**：取代原本的固定隨機範圍，支援按時段設定不同的觸發間隔與權重，實現更自然的主動訊息節奏。
- **配置自動備份**：每次插件重載時自動備份當前配置快照。
- **Prompt 佔位符**：支援 `{{current_time}}`（當前時間）與 `{{unanswered_count}}`（未回覆次數）。
- 全面轉換為繁體中文（台灣標準）的註解、日誌與配置描述。

### 效能 (Perf)

- 優先使用 `frozenset` / `tuple` 作為不可變常數。
- 正則表達式預編譯為模組級常數。
- 避免重複查詢 `get_session_config()`，改為傳遞已查詢的結果。

---

<details>
<summary>點擊查看 v1.x 歷史更新紀錄</summary>

> v1.x 版本基於原作者 [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) 的更新紀錄，本 Fork 從 v2.0.0 開始獨立維護。
> 完整的 v1.x 更新紀錄請參閱原作者的 [CHANGELOG](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat/blob/main/CHANGELOG.md)。

</details>