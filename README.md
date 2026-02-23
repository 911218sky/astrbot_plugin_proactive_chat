<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD041 -->

<div align="center">

# 🤖 AstrBot 主動訊息插件 (Enhanced Fork)

繁體中文 | [English](README_EN.md) | [日本語](README_JP.md)

</div>

<p align="center">
  <img src="https://img.shields.io/badge/License-AGPL_3.0-blue.svg" alt="License: AGPL-3.0">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/AstrBot-v4.8.0+-orange.svg" alt="AstrBot v4.8.0+">
</p>

<p align="center">
  <img src="logo.png" width="200" alt="logo" />
</p>

---

一個為 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 設計的主動訊息插件，讓你的 Bot 能在會話沉默後，以隨機間隔主動發起具有上下文感知、符合人設且包含動態情緒的對話。

## 🙏 致謝原作者

本專案基於 [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) 修改而來，感謝原作者 **DBJD-CR** 及其協作者的出色工作。原專案提供了完整的主動訊息框架，包括多會話支援、持久化、免打擾時段、TTS 整合、分段回覆等功能。

> 如果你喜歡這個插件的核心理念，請務必去原倉庫給一顆 ⭐ Star。

## ✨ 本 Fork 新增功能

在原專案的基礎上，本 Fork 進行了以下改進：

### 1. 模組化重構 + 效能優化

將原本 2500+ 行的 `main.py` 拆分為清晰的模組結構，並進行全面效能優化：

| 模組 | 職責 |
| :--- | :--- |
| `core/utils.py` | 通用工具函數（免打擾判斷、UMO 解析、日誌格式化） |
| `core/config.py` | 配置管理（驗證、會話配置查詢、備份） |
| `core/scheduler.py` | 排程邏輯（加權隨機間隔計算、時段規則匹配） |
| `core/messaging.py` | 訊息發送（裝飾鉤子、分段回覆、歷史記錄清洗） |
| `main.py` | 插件入口（生命週期、事件處理、核心調度） |

效能優化亮點：
- 插件主類使用 `__slots__` 減少記憶體開銷
- 合併私聊/群聊事件處理為共用 `_handle_message()`，消除約 80% 重複程式碼
- 提取 `_add_scheduled_job`、`_cancel_timer`、`_call_llm` 等輔助方法，減少冗餘邏輯
- `_is_chat_allowed` 支援傳入已查詢的配置，避免 `check_and_chat` 中重複查詢
- 預編譯正則表達式、`frozenset` 常數、同步化不需要非同步的函數
- 全檔加上詳細繁體中文註解，方便後續開發者理解流程

### 2. template_list 動態會話管理

將 `private_sessions` 和 `group_sessions` 從原本 5 個固定槽位（`session_1`..`session_5`）的 `object` 類型，改為 AstrBot 的 `template_list` 類型：

- 不再有會話數量限制，可自由新增/刪除
- 配置 JSON 從 ~2500 行縮減至 ~660 行（減少 74%）
- WebUI 載入更快、操作更流暢

### 3. schedule_rules 分時段加權隨機排程

在所有 `schedule_settings` 中新增 `schedule_rules`（`template_list` 類型），支援按時段設定觸發間隔的加權隨機分佈：

- 每條規則包含 `start_hour`、`end_hour`、`interval_weights`
- `interval_weights` 格式：`"20-30:0.2,30-50:0.5,50-90:0.3"`（分鐘:權重）
- 匹配當前時段後加權隨機選取間隔；未匹配則回退到全域最小/最大間隔
- 支援跨天時段（如 22-6）

## 🌟 繼承自原專案的功能

- 多會話支援（私聊 + 群聊，完全隔離）
- 全域配置 + 個性化配置系統
- 基於沉默時間的定時觸發
- 自動主動訊息（無需使用者輸入即可啟動）
- 上下文感知 + 完整人格支援
- 動態情緒（未回覆計數器）
- 持久化會話（重啟後恢復任務）
- 免打擾時段
- TTS 語音整合
- 分段回覆（模擬打字間隔）
- 裝飾鉤子（相容表情包、情緒等插件）
- 高度可配置（WebUI 操作，無需改程式碼）

## 🚀 安裝與使用

1. 從本倉庫下載 `.zip`，在 AstrBot WebUI 中選擇「從檔案安裝」
2. 核心依賴 `APScheduler` 和 `aiofiles` 通常已包含在 AstrBot 中
3. 進入 WebUI → 插件配置，設定目標會話和主動訊息動機
4. 儲存配置後即可開始使用

## 📂 專案結構

```
astrbot_plugin_proactive_chat/
├── core/                  # 核心模組
│   ├── __init__.py        # 模組匯出
│   ├── config.py          # 配置管理
│   ├── messaging.py       # 訊息發送
│   ├── scheduler.py       # 排程邏輯
│   └── utils.py           # 通用工具
├── assets/                # 靜態資源
├── main.py                # 插件入口（含詳細註解）
├── _conf_schema.json      # 配置結構定義
├── metadata.yaml          # 插件元資料
├── requirements.txt       # 依賴列表
├── CHANGELOG.md           # 更新日誌
├── LICENSE                # AGPL-3.0
└── README.md
```

## ⚙️ 配置說明

配置項與原專案基本一致，主要差異：

- `private_sessions` / `group_sessions`：現為 `template_list`，可動態新增會話
- `schedule_rules`：新增於每個 `schedule_settings` 中，用於分時段加權排程

其餘配置項（主動訊息動機、免打擾時段、TTS、分段回覆等）請參考原專案文件。

### schedule_rules 配置範例

```
start_hour: 8
end_hour: 23
interval_weights: "20-30:0.2,30-50:0.5,50-90:0.3"
```

表示在 8:00-23:00 時段內，有 20% 機率選取 20-30 分鐘間隔、50% 機率選取 30-50 分鐘、30% 機率選取 50-90 分鐘。

## 🌐 平台適配

| 平台 | 支援情況 |
| :--- | :--- |
| QQ 個人號 (aiocqhttp) | ✅ 完整支援 |
| Telegram | ❓ 理論支援 |
| 飛書 | ❓ 理論支援 |

## 📄 授權

GNU Affero General Public License v3.0 — 詳見 [LICENSE](LICENSE)。

## 💖 相關連結

- 原專案：[DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat)
- AstrBot：[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)
