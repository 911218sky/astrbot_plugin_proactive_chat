# AGENTS.md — AI Agent 開發指南

本文件為 AI 代理（如 Copilot、Cursor、Kiro 等）提供專案上下文與開發規範。

## 專案概述

AstrBot 主動訊息插件（Enhanced Fork），讓 Bot 能在會話沉默後主動發起對話。
基於 [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) 修改。

## 技術棧

- Python 3.10+
- AstrBot 插件框架（繼承 `star.Star`）
- APScheduler（非同步定時任務）
- aiofiles（非同步檔案 I/O）

## 專案結構

```
├── main.py                # 插件入口：生命週期、事件處理、定時調度、LLM 呼叫
├── core/
│   ├── __init__.py        # 模組匯出（使用相對匯入）
│   ├── utils.py           # 通用工具：免打擾判斷、UMO 解析、日誌格式化
│   ├── config.py          # 配置管理：驗證、會話配置查詢、備份
│   ├── scheduler.py       # 排程邏輯：加權隨機間隔、時段規則匹配
│   └── messaging.py       # 訊息發送：裝飾鉤子、分段回覆、歷史清洗
├── _conf_schema.json      # WebUI 配置結構定義（AstrBot schema 格式）
├── metadata.yaml          # 插件元資料
└── requirements.txt       # 依賴列表
```

## 核心流程

1. 使用者發送訊息 → `_handle_message()` 記錄時間、重設計時器
2. 私聊：立即排定下一次主動訊息（`_schedule_next_chat_and_save`）
3. 群聊：等待沉默 N 分鐘後才排定（`_reset_group_silence_timer`）
4. APScheduler 觸發 `check_and_chat()` → 檢查條件 → 呼叫 LLM → 發送訊息

## 開發規範

### 語言與編碼
- 所有程式碼註解、日誌字串使用**繁體中文**（台灣標準：群 不是 羣、為 不是 爲、啟 不是 啓）
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

## Git 提交規範

- Commit message 使用**英文**
- 格式：`type: description`（如 `feat: add schedule_rules support`、`fix: resolve UMO parsing error`）
