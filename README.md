<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD041 -->

<div align="center">

# 🤖 AstrBot 主動訊息插件 (Plus Fork)

讓你的 Bot 不再只是被動回覆，而是能主動找人聊天。

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

一個為 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 設計的主動訊息插件。Bot 會在會話沉默後，以加權隨機間隔主動發起具有上下文感知、符合人設且包含動態情緒的對話。支援私聊與群聊，所有配置皆可透過 WebUI 操作。

## 🔄 運作流程

```
用戶發送訊息
    │
    ├─ 私聊 ──→ 立即排定下一次主動訊息（加權隨機間隔）
    │              │
    │              ├─ 語境感知開啟？──→ LLM 分析語境，額外排定一個預測任務
    │              │                    （例：「我在看電影」→ 90 分鐘後問好不好看）
    │              │
    │              └─ 等待觸發...
    │
    └─ 群聊 ──→ 等待群組沉默 N 分鐘
                   │
                   └─ 沉默達標 → 排定主動訊息
                                    │
                                    └─ 等待觸發...

APScheduler 定時觸發 check_and_chat()
    │
    ├─ 免打擾時段？ ──→ 跳過，排定下一次
    ├─ 未回覆衰減判定 ──→ 概率不通過？跳過，排定下一次
    ├─ 平台是否存活？ ──→ 未運行？延後重試
    │
    └─ 通過所有檢查
         │
         ├─ 構造 Prompt（注入時間、未回覆次數、語境提示）
         ├─ 呼叫 LLM 生成回應
         ├─ 狀態一致性檢查（LLM 生成期間用戶是否發了新訊息）
         ├─ 發送訊息（支援 TTS + 分段回覆）
         └─ 遞增未回覆計數，排定下一次
```

## ✨ 功能特色

### 核心功能

- 多會話支援 — 私聊 + 群聊完全隔離，各自獨立配置
- 全域配置 + 個性化配置 — 全域設定作為預設，個別會話可覆蓋
- 動態會話管理 — 使用 `template_list`，不限會話數量，WebUI 自由新增/刪除
- 持久化 — 重啟後自動恢復排程任務，不遺失狀態
- 自動觸發 — 插件啟動後若會話無訊息，可自動建立首次排程
- 記憶整合 — 可選整合 [livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 插件，主動訊息生成時檢索長期記憶，讓對話更貼合用戶歷史

### 智慧排程

- 分時段加權隨機 — 透過 `schedule_rules` 定義不同時段的觸發間隔分佈
- 跨天時段 — 支援如 22:00-06:00 的跨日規則
- 語境感知排程 — LLM 根據對話內容預測最佳觸發時機（例：「晚安」→ 隔天早上問候）
- 自訂語境分析 LLM — 可指定獨立的 LLM 平台用於語境分析，節省主模型 token
- 語境補充提示 — 可附加自訂指示影響語境分析行為
- 免打擾時段 — 指定時間範圍內不發送主動訊息

### 擬人行為

- 未回覆概率衰減 — 對方不回就逐次降低觸發概率，避免騷擾
- 分段回覆 — 長文切分為多條短訊息，模擬真人打字節奏
- 動態情緒 — Prompt 中注入未回覆次數，讓 LLM 自然調整語氣
- TTS 語音 — 可選將文字轉為語音發送

## 🙏 致謝原作者

本專案基於 [DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) 修改而來，感謝原作者 **DBJD-CR** 及其協作者的出色工作。

> 如果你喜歡這個插件的核心理念，請務必去原倉庫給一顆 ⭐ Star。

### 相較原專案的改進

| 改進項目 | 說明 |
| :--- | :--- |
| 模組化重構 | 原本 2500+ 行的 `main.py` 拆分為 `core/` 子模組，職責清晰 |
| 效能優化 | `__slots__`、合併事件處理、減少重複查詢、預編譯正則 |
| 動態會話管理 | 從固定 5 個槽位改為 `template_list`，不限數量 |
| 分時段排程 | 新增 `schedule_rules`，不同時段可設定不同的間隔分佈 |
| 逐次概率衰減 | `decay_rate` 從單一指數衰減改為逐次概率列表，更精細 |
| 語境感知排程 | 新增 LLM 語境分析，根據對話內容智慧決定觸發時機 |
| 記憶整合 | 可選整合 livingmemory 插件，主動訊息帶入長期記憶上下文 |
| 獨立 LLM 平台 | 語境分析可指定獨立的 LLM 平台，節省主模型 token |
| Prompt 模板外置 | 語境預測的 prompt 模板抽離至 `core/prompts/`，方便自訂 |
| 配置精簡 | `_conf_schema.json` 從 ~2500 行縮減至 ~1200 行 |

## 🚀 安裝與使用

1. 從本倉庫下載 `.zip`，在 AstrBot WebUI 中選擇「從檔案安裝」
2. 核心依賴 `APScheduler` 和 `aiofiles` 通常已包含在 AstrBot 中
3. 進入 WebUI → 插件配置，設定目標會話和主動訊息動機
4. 儲存配置後即可開始使用

## ⚙️ 配置說明

所有配置皆可透過 AstrBot WebUI 操作，以下為主要配置結構：

### 配置層級

```
├─ private_settings          # 👤 私聊全域配置（作為所有私聊的預設值）
├─ group_settings            # 👥 群聊全域配置（作為所有群聊的預設值）
├─ private_sessions          # 👤 私聊會話列表（個別會話可覆蓋全域設定）
└─ group_sessions            # 👥 群聊會話列表（個別會話可覆蓋全域設定）
```

每個會話配置（無論全域或個別）都包含以下子項：

| 配置區塊 | 說明 |
| :--- | :--- |
| `enable` | 是否啟用此會話的主動訊息 |
| `auto_trigger_settings` | 插件啟動後自動觸發的設定 |
| `proactive_prompt` | 主動訊息的動機 Prompt（指導 LLM 如何發起對話） |
| `schedule_settings` | 排程相關：間隔、免打擾、衰減、分時段規則 |
| `tts_settings` | TTS 語音合成設定 |
| `context_aware_settings` | 語境感知排程設定 |
| `segmented_reply_settings` | 分段回覆設定 |

### Prompt 佔位符

在 `proactive_prompt` 中可使用以下佔位符：

| 佔位符 | 說明 | 範例值 |
| :--- | :--- | :--- |
| `{{current_time}}` | 當前時間 | `2025年06月15日 14:30` |
| `{{unanswered_count}}` | Bot 連續未被回覆的次數 | `2` |

### schedule_rules 分時段排程

在 `schedule_settings` 中可新增多條時段規則，每條規則定義該時段的觸發間隔分佈與衰減策略：

```
start_hour: 8
end_hour: 24
interval_weights: "30-60:0.3,60-120:0.4,120-240:0.2,240-480:0.1"
decay_rate: "0.8,0.5,0.3,0.15"
```

上述範例表示在 08:00-24:00 時段內：

- `interval_weights`（觸發間隔）：30% 機率等 30-60 分鐘、40% 等 60-120 分鐘、20% 等 120-240 分鐘、10% 等 240-480 分鐘
- `decay_rate`（未回覆衰減）：第 1 次未回覆 → 80% 觸發、第 2 次 → 50%、第 3 次 → 30%、第 4 次 → 15%

兩者的關係：`interval_weights` 決定「等多久」，`decay_rate` 決定「要不要發」。

### 未回覆衰減機制

`decay_rate` 是逗號分隔的概率列表，每個值對應第 N 次未回覆時的觸發概率：

| `decay_rate` 值 | 效果 |
| :--- | :--- |
| `""` (留空) | 不衰減，每次都 100% 觸發 |
| `"0.8,0.5,0.3,0.15"` | 逐次遞減：80% → 50% → 30% → 15% |
| `"0.7"` | 每次未回覆都用 70% 概率 |
| `"0"` | 只觸發一次就停止 |

衰減率解析優先順序：
1. 當前匹配的 `schedule_rules` 中的 `decay_rate` 列表
2. `default_decay_rate`（列表用盡後的回退概率）
3. 以上皆未配置 → 回退到 `max_unanswered_times` 硬性上限

### 語境感知排程

啟用 `context_aware_settings` 後，每次用戶發訊息時 LLM 會分析對話語境，預測最佳的跟進時機：

| 用戶訊息 | LLM 預測行為 |
| :--- | :--- |
| 「我在看電影」 | 約 90-120 分鐘後問「電影好看嗎？」 |
| 「晚安」 | 約 7-9 小時後早安問候 |
| 「我去開會了」 | 約 30-90 分鐘後關心會議情況 |
| 「在通勤」 | 約 20-60 分鐘後問是否到了 |
| 普通閒聊 | 不額外排程，使用原有隨機排程 |

此功能與原有的隨機排程並行運作。當用戶發新訊息時，會自動檢查已排定的語境任務是否應取消（例如用戶說「看完了」→ 取消「問電影好不好看」的排程）。

### 語境感知進階設定

| 設定項 | 說明 |
| :--- | :--- |
| `llm_provider_id` | 指定語境分析使用的 LLM 平台（WebUI 下拉選擇）。留空使用預設。建議指定較便宜的模型以節省 token |
| `extra_prompt` | 附加到語境分析 prompt 末尾的補充指示。例如：「如果用戶提到運動，延遲設為 60-90 分鐘」 |
| `enable_memory` | 啟用/停用 livingmemory 記憶檢索（需安裝 livingmemory 插件，未安裝時自動跳過） |
| `memory_top_k` | 每次檢索的記憶條數（1-20），啟用記憶後可見 |

## 📁 專案結構

```
astrbot_plugin_proactive_chat_plus/
├── main.py                    # 插件入口：生命週期、事件處理、核心調度
├── core/
│   ├── __init__.py            # 模組匯出
│   ├── utils.py               # 通用工具（免打擾判斷、UMO 解析、日誌格式化）
│   ├── config.py              # 配置管理（驗證、會話配置查詢、備份）
│   ├── scheduler.py           # 排程邏輯（加權隨機間隔、時段規則、衰減判定）
│   ├── context_predictor.py   # 語境感知（LLM 預測時機、任務取消判斷）
│   ├── messaging.py           # 訊息發送（裝飾鉤子、分段回覆、歷史清洗）
│   ├── llm_helpers.py         # LLM 輔助（請求準備、記憶檢索整合、LLM 呼叫封裝）
│   ├── send.py                # 主動訊息發送（TTS / 文字 / 分段發送）
│   └── prompts/               # LLM Prompt 模板（語境預測、任務取消判斷）
├── _conf_schema.json          # WebUI 配置結構定義
├── metadata.yaml              # 插件元資料
├── requirements.txt           # 依賴列表
├── CHANGELOG.md               # 更新日誌
└── LICENSE                    # AGPL-3.0
```

## 🌐 平台適配

| 平台 | 支援情況 |
| :--- | :--- |
| QQ 個人號 (aiocqhttp) | ✅ 完整支援 |
| Telegram | ❓ 理論支援（未測試） |
| 飛書 | ❓ 理論支援（未測試） |

## 📄 授權

GNU Affero General Public License v3.0 — 詳見 [LICENSE](LICENSE)。

## 💖 相關連結

- 原專案：[DBJD-CR/astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat)
- AstrBot：[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)
