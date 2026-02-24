# AGENTS.md — AI Agent 開發指南

本文件為 AI 代理（如 Copilot、Cursor、Kiro 等）提供專案上下文與開發規範。

## 專案概述

AstrBot 主動訊息插件（Plus Fork），讓 Bot 能在會話沉默後主動發起對話。
基於 [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) 修改。

## 技術棧

- Python 3.10+
- AstrBot 插件框架（繼承 `star.Star`）
- APScheduler（非同步定時任務）
- aiofiles（非同步檔案 I/O）

## 專案結構

```
├── main.py                  # 插件入口：生命週期、事件處理、定時調度
├── core/
│   ├── __init__.py          # 模組匯出（使用相對匯入）
│   ├── utils.py             # 通用工具：免打擾判斷、UMO 解析、日誌格式化
│   ├── config.py            # 配置管理：驗證、會話配置查詢、備份
│   ├── scheduler.py         # 排程邏輯：加權隨機間隔、時段規則匹配、未回覆概率衰減
│   ├── context_predictor.py # 語境感知：LLM 預測主動訊息時機、任務取消判斷
│   ├── messaging.py         # 訊息發送：裝飾鉤子、分段回覆、歷史清洗
│   ├── llm_helpers.py       # LLM 輔助：請求準備、記憶檢索整合、LLM 呼叫封裝
│   ├── send.py              # 主動訊息發送：TTS / 文字 / 分段發送邏輯
│   └── prompts/             # LLM Prompt 模板（語境預測、任務取消判斷）
├── _conf_schema.json        # WebUI 配置結構定義（AstrBot schema 格式）
├── metadata.yaml            # 插件元資料
└── requirements.txt         # 依賴列表
```

## 核心流程

1. 使用者發送訊息 → `_handle_message()` 記錄時間、重設計時器
2. 私聊：立即排定下一次主動訊息（`_schedule_next_chat_and_save`）
3. 群聊：等待沉默 N 分鐘後才排定（`_reset_group_silence_timer`）
4. APScheduler 觸發 `check_and_chat()` → 檢查條件（含衰減概率判定）→ 呼叫 LLM → 發送訊息

## 語境感知排程

`context_aware_settings` 啟用後，每次使用者發訊息時會呼叫 LLM 分析對話語境，動態預測下一次主動訊息的最佳時機：
- 例如用戶說「我在看電影」→ LLM 預測約 90-120 分鐘後問「電影好看嗎？」
- 例如用戶說「晚安」→ LLM 預測約 7-9 小時後早安問候
- 與原有的隨機排程並行運作，語境預測的任務會額外排定
- 用戶發新訊息時會檢查已排定的語境任務是否應取消（例如用戶說「看完了」）

相關函數：`core/context_predictor.py` 中的 `predict_proactive_timing()` 和 `check_should_cancel_task()`。

## livingmemory 記憶整合

主動訊息生成時可選從 [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 檢索相關長期記憶，注入到 system_prompt 中，讓 LLM 生成更貼合用戶歷史的主動訊息。

- 透過 `context_aware_settings.enable_memory` 開關記憶檢索
- 透過 `context_aware_settings.memory_top_k` 控制檢索數量（1-20）
- 為可選依賴：未安裝 livingmemory 時自動跳過，不影響主動訊息功能
- 檢索查詢優先使用語境任務的 hint/reason，無語境任務時使用當前時間
- 記憶內容截斷至 200 字元，避免 prompt 過長

相關函數：`core/llm_helpers.py` 中的 `get_livingmemory_engine()`、`recall_memories_for_proactive()`。

## 未回覆衰減機制

每條 `schedule_rules` 時段規則可選配置 `decay_rate`（逐次概率列表）：
- 格式：逗號分隔的 0~1 概率值，每個值對應第 N 次未回覆的觸發概率
- 例如 `decay_rate="0.8,0.5,0.3,0.15"`：第 1 次 → 80%、第 2 次 → 50%、第 3 次 → 30%、第 4 次 → 15%
- 填單一值如 `"0.7"` 則每次未回覆都用同一概率
- 留空表示不衰減（100% 觸發），填 `"0"` 表示只觸發一次就停止
- 超出列表長度時使用 `default_decay_rate`（全域預設遞減步長）從列表末尾值接續遞減
- `default_decay_rate` 為遞減步長（0~1）：填 `0.05` 表示每次遞減 5%
  - 有 `decay_rate` 列表時：列表用盡後從末尾值接續遞減（如列表末尾 0.8，步長 0.05 → 0.75, 0.70, ...）
  - 無 `decay_rate` 列表時：從 1.0 開始遞減（1.0, 0.95, 0.90, ...）
  - 填 `0` 表示不衰減（維持 100% 或列表末尾概率永遠觸發）
  - 留空表示不使用遞減衰減（回退到硬性上限邏輯）
- 以上皆未配置時，回退到 `max_unanswered_times` 硬性上限

相關函數：`core/scheduler.py` 中的 `should_trigger_by_unanswered()`、`_resolve_decay_list()`、`_roll_probability()`、`_continue_decay_from()`、`_generate_step_decay_list()`。

## 開發規範

### AI 回覆語言
- AI 代理與使用者對話時**一律使用英文回覆**，即使使用者以中文提問也必須用英文回答

### 語言與編碼
- 所有程式碼註解、日誌字串使用**繁體中文**（台灣標準：群 不是 羣、為 不是 爲、啟 不是 啓）
- `_conf_schema.json` 中的 description / hint 使用**繁體中文**
- 日誌前綴統一使用 `_LOG_TAG = "[主動訊息]"`
- 檔案編碼一律 UTF-8

### 匯入規則
- `main.py` 匯入 core 模組時使用**相對匯入**：`from .core.utils import ...`
- core 模組之間也使用相對匯入：`from .utils import ...`
- 這是因為插件在 AstrBot 中作為子包載入，絕對匯入 `from core.xxx` 會與 AstrBot 自身的 `core` 衝突

### 程式碼風格
- 插件主類使用 `__slots__` 減少記憶體開銷
- 優先使用 `frozenset` / `tuple` 作為不可變常數
- 不需要非同步的函數不要標記 `async`
- 正則表達式盡量預編譯為模組級常數
- 避免在同一方法中重複呼叫 `get_session_config()`，可傳遞已查詢的結果

### AstrBot API 注意事項
- `EventMessageType` 使用 `PRIVATE_MESSAGE`（不是 `FRIEND_MESSAGE`）
- UMO 格式為 `平台ID:訊息類型:目標ID`（如 `aiocqhttp:GroupMessage:123456`）
- 某些 AstrBot 版本的 API 對 UMO 格式有嚴格要求，需處理 `ValueError` 並回退

### 配置 Schema
- `_conf_schema.json` 使用 AstrBot 的 schema 格式
- 動態列表使用 `template_list` 類型（參考 https://docs.astrbot.app/dev/star/guides/plugin-config.html）
- Emoji 圖示需確保為完整的 Unicode 字元，避免出現亂碼 `�`
- Prompt 佔位符：`{{current_time}}`（當前時間）、`{{unanswered_count}}`（未回覆次數）

## 程式碼品質

提交前務必執行：

```bash
ruff format .        # 格式化
ruff check --fix .   # Lint 修復
ruff check .         # 確認零錯誤
```

專案已配置 GitHub Actions 自動執行 ruff 檢查，PR 未通過會被標記。

## 測試方式

本插件無獨立測試套件，測試方式為在 AstrBot 環境中載入插件並觀察日誌輸出。
啟動 AstrBot 後，檢查日誌中是否出現 `[主動訊息] 初始化完成。` 即表示載入成功。

## 版本管理

版本號位於 `metadata.yaml` 的 `version` 欄位，格式為 `vMAJOR.MINOR.PATCH`（如 `v2.1.0`）。

**每次提交到 GitHub 時，必須同步更新版本號。** AI 代理應根據變更內容自動判斷版本類型：

| 版本類型 | 何時使用 | 範例 |
| :--- | :--- | :--- |
| MAJOR（大版本） | 破壞性變更：配置格式不相容、移除功能、重大架構改動 | `v2.0.0` → `v3.0.0` |
| MINOR（中版本） | 新增功能、新增配置項、行為變更但向下相容 | `v2.0.0` → `v2.1.0` |
| PATCH（小版本） | Bug 修復、文件更新、效能優化、程式碼重構（不影響功能） | `v2.1.0` → `v2.1.1` |

判斷原則：
- 改了 `_conf_schema.json` 的結構（新增/刪除欄位）→ 至少 MINOR
- 只改了 README、AGENTS.md、註解、hint 文字 → PATCH
- 新增了 `.py` 檔案或新功能 → MINOR
- 改了現有功能的行為邏輯 → MINOR
- 刪除了配置項或改了配置格式導致舊配置不能用 → MAJOR

## Git 提交規範

- Commit message 使用**英文**
- 格式：`type: description`（如 `feat: add schedule_rules support`、`fix: resolve UMO parsing error`）
- **禁止自動提交**：AI 代理**絕對不得自動執行 `git add` / `git commit` / `git push`**，除非使用者明確同意提交。即使使用者要求「幫我提交」，也必須先列出變更摘要並等待使用者確認後才能執行。
- **版本號更新**：每次提交前，必須根據上方「版本管理」規則更新 `metadata.yaml` 中的 `version`。
