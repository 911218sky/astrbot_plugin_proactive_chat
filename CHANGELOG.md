<!-- markdownlint-disable MD024 -->
<!-- markdownlint-disable MD025 -->
<!-- markdownlint-disable MD033 -->
<!-- markdownlint-disable MD034 -->
<!-- markdownlint-disable MD041 -->
# ChangeLog

# 2026/06/12 v2.20.0

## What's Changed

### 新增 (Feature)

- 新增 `allow_all_sessions` 全域無白名單模式，私聊或群聊可在全域配置中直接套用到所有同類型會話。
- 新增 `habit_settings` 習慣時段主動出現，可設定星期、時間窗、出現機率、賴床/遲到機率、隨機偏移與 LLM 話題提示。
- 習慣時段訊息預設不累加未回覆次數，也不會因此排下一輪一般主動訊息；單條規則可用 `count_unanswered` 改為累加。
- Web 任務頁與 `/proactive tasks` 現在會顯示習慣時段任務，並支援改期、修改描述與刪除。

### 修復 (Fix)

- 習慣時段任務的 LLM 或發送失敗不再誤排一般主動訊息，已消耗任務會正確清理並安排下一個習慣任務。
- `count_unanswered=true` 的習慣時段任務會遵守未回覆硬性上限與衰減檢查。
- 習慣時段跨日規則會處理午夜後仍有效的前一日時間窗；單次未出現會繼續尋找下一個符合日期。

---

# 2026/06/08 v2.19.3

## What's Changed

### 修復 (Fix)

- 修復 `max_unanswered_times` 會被 `decay_rate` 或 `default_decay_rate` 分支繞過的問題；未回覆硬性上限現在會優先判斷，達到上限後不再發送主動訊息。
- 達到未回覆硬性上限時會清除 `next_trigger_time`，避免重啟後從插件 SQLite 恢復舊排程。
- 修復同一目標因平台前綴不同產生重複 session 狀態，導致重啟後重複排程的問題。
- 修復完整 UMO 會話配置被降成純 ID 比對，可能跨平台誤匹配的問題。
- 修復重啟後 `last_message_time` 幾乎不會恢復，導致自動觸發與一致性檢查失準的問題。
- 修復時段 `reset` / `custom` 會在同一時段內每次排程都重置未回覆計數的問題。
- 修復訊息發送失敗仍被當成成功、寫入歷史並增加未回覆計數的問題；發送失敗時會清除本輪排程並等待使用者新訊息。
- 修復 LLM 認證類錯誤被吞掉後持續重新排程的問題。
- 修復分段回覆門檻判斷反向，造成短訊息被拆、長訊息不拆的問題。
- 修復 LLM 回傳空白內容仍可能進入發送流程的問題。

---

# 2026/06/07 v2.19.1

## What's Changed

### 修復 (Fix)

- 建立、改期、語境任務與等待 timer 統一採用「先保存插件 DB，再掛排程」流程，降低重啟後任務消失的風險。
- Web 任務頁刪除任務改為先更新 `proactive_state.db`，保存成功後才移除 APScheduler job 或記憶體 timer，避免刪除後重啟復活。
- 語境任務自動取消改為先保存最新 `pending_context_tasks`，保存成功後才移除 APScheduler job。
- 修復完整 UMO 或 Telegram 會話 ID 在白名單/會話配置比對時可能不匹配的問題。

### 改進 (Improve)

- 任務頁新增指定會話 ID 篩選，刷新資料時會保留目前選取的新增任務會話。
- 改期彈窗預設帶入目前任務時間，不再預設填入 10 分鐘，避免誤改時間。
- 任務描述欄改為固定提示文字，若存在語境原始判斷會以輔助文字顯示，方便使用者理解與修改。
- `requirements.txt` 為 APScheduler、aiofiles、aiosqlite 加上大版本上限，降低未來依賴破壞 API 的風險。
- AI 維護指南補充 CI 使用的 Ruff 版本指令。

---

# 2026/06/07 v2.19.0

## What's Changed

### 新增 (Feature)

- 新增插件自有 SQLite 狀態庫 `proactive_state.db`，用 `aiosqlite>=0.20.0` 保存最新任務狀態。
- 任務頁「改期」改為彈窗操作，可在彈窗內選擇延遲分鐘或指定時間，並同步修改任務描述。

### 修復 (Fix)

- 修復自動觸發與群聊沉默等待任務重啟後可能消失、或只剩頁面資料但沒有實際 timer 的問題。
- 重啟時若一般排程、語境任務或等待計時器剛錯過觸發時間，會在 30 分鐘寬限內補跑。
- 任務頁現在可刪除或修改 DB 還原出來的等待任務，不再要求記憶體 timer 必須存在。
- 後端 API 改成指定時間優先於延遲分鐘，避免舊頁面或手動請求同時送兩者時排錯時間。

### 改進 (Improve)

- 升級時若新 DB 為空，會讀取一次舊 `session_data.json` 並寫入 SQLite；之後只保留最新狀態。
- `requirements.txt` 明確加入 `apscheduler>=3.10.0`、`aiofiles>=23.0.0`、`aiosqlite>=0.20.0`。
- 文件與 AI 維護指南同步補充插件 DB、重啟恢復與改期彈窗流程。

---

# 2026/06/06 v2.18.6

## What's Changed

### 修復 (Fix)

- 修復自動觸發與群聊沉默等待任務的描述只會顯示在頁面、但不會進入主動訊息 Prompt 的問題。
- 修復列表內編輯描述後直接改期時，沒有使用該列描述的操作不一致問題。
- 保存任務描述時會確認任務仍存在，避免舊頁面資料留下無對應任務的描述。

### 改進 (Improve)

- 任務管理頁改為參考 livingmemory 的 AstrBot Pages 風格，採用左側導覽、頁首、統計卡片、篩選卡片與資料表格版面。
- 新增頁面主題切換並支援 AstrBot 深色/淺色上下文。
- 自動觸發/群聊沉默任務改期成手動排程時，會把該列描述轉成正式手動排程描述。

---

# 2026/06/06 v2.18.5

## What's Changed

### 新增 (Feature)

- 任務頁新增「任務描述」欄位，建立手動排程時可填寫提醒目標或接續話題。
- 任務列表可直接修改並保存任務描述，也支援清空描述。
- 一般手動排程觸發時會把任務描述注入主動訊息 Prompt，讓實際發送內容能遵守頁面設定。

### 改進 (Improve)

- 新增任務表單補齊欄位說明，會話、延遲分鐘、指定時間與任務描述用途更清楚。
- 任務列表的描述欄改成可編輯區塊，降低修改任務時的操作成本。

---

# 2026/06/06 v2.18.4

## What's Changed

### 改進 (Improve)

- 重做 AstrBot Pages 任務管理頁視覺層級，摘要、篩選、手動排程與任務列表更容易掃描。
- 新增排程器狀態標籤、篩選結果數量與清除篩選按鈕。
- 任務列表改成會話 / 任務 / 時間 / 未回覆 / 描述 / 操作的管理台式版面，強化剩餘時間與操作按鈕可讀性。
- CSS 重整為更貼近 AstrBot 內嵌工具頁的淺色 / 深色主題。

---

# 2026/06/06 v2.18.3

## What's Changed

### 改進 (Improve)

- 任務頁新增會話欄位說明與「沒有已啟用會話」空狀態，避免新增任務時看不懂欄位用途。
- 任務頁把「執行」改為「檢查」，明確表示會先套用免打擾、衰減與未回覆上限等條件。
- 自動觸發與群組沉默等待任務改期時會轉成手動排程，前端提示已補充此語意。
- 配置提示補充 QQ、Telegram 與其他平台會話 ID 的寫法，避免誤以為只能填 QQ。
- README 同步說明 `decay_rate` 留空為不衰減，填 `0` 為觸發概率 0。

---

# 2026/06/06 v2.18.2

## What's Changed

### 修復 (Fix)

- 修復 AstrBot 重啟後，語境預測任務雖然會顯示在任務頁，但沒有重新掛回 APScheduler，導致觸發時間已過仍不發送的問題。
- 重啟時若語境預測任務剛錯過觸發時間，會安排立即補跑；太舊的過期任務會自動清理。
- 修正任務頁修改語境任務時的 `ctx_job_id` 傳遞方式，避免修改後觸發時無法正確清理語境任務。

### 改進 (Improve)

- 任務頁「立即執行」改用統一的會話解析邏輯，支援 QQ、Telegram 與完整 UMO。
- `memory_top_k` 增加型別防呆，配置填成字串或異常值時會回退預設值。

---

# 2026/06/06 v2.18.1

## What's Changed

### 修復 (Fix)

- 修復 AstrBot Pages iframe 內原生確認視窗被沙盒阻擋時，任務「修改」與「刪除」按鈕沒有反應的問題。
- 任務操作改用頁面內確認視窗，保留取消、Esc 與背景點擊關閉。

### 改進 (Improve)

- 新增一次性任務區塊補上用途說明與欄位提示，讓會話、延遲分鐘、指定時間的用途更清楚。
- 任務按鈕只在確認後進入處理中狀態，避免取消操作時顯示誤導文字。

---

# 2026/06/06 v2.18.0

## What's Changed

### 新增 (Feat)

- **AstrBot Pages 任務管理**：
  - 儀表板新增會話、任務類型、啟用狀態與關鍵字過濾。
  - 新增建立一般排程任務、修改執行時間、立即執行與刪除任務操作。
  - 任務操作經由 `/page/tasks/action` API 執行，避免前端直接碰 scheduler 內部物件。

### 改進 (Improve)

- **AstrBot API 對齊**：
  - 主動訊息發送改用 `context.send_message(session_id, MessageChain)`，減少對平台私有發送介面的依賴。
  - Web API 回傳已配置會話清單，讓任務建立與篩選可同時支援 QQ、Telegram 等平台的 UMO。
- **可維護性**：
  - 前端任務表格與操作區維持單一資料流：刷新 snapshot 後重新渲染，降低狀態同步複雜度。

### 文件 (Docs)

- 更新 README、AGENTS 與 metadata，說明任務管理、過濾與新版 Web 介面能力。

---

# 2026/06/06 v2.17.0

## What's Changed

### 新增 (Feat)

- **AstrBot Pages 任務儀表板**：
  - 新增 `pages/dashboard/` Web 介面，可在 AstrBot 插件頁查看目前主動訊息任務。
  - 新增 `core/page_api.py`，提供 `/page/status` 與 `/page/tasks` API。
  - 儀表板支援任務摘要、搜尋、任務類型篩選、手動刷新與自動刷新。

### 改進 (Improve)

- **livingmemory 整合加強**：
  - 等待 livingmemory 初始化完成後再檢索，降低 AstrBot 啟動期間抓不到記憶的機率。
  - 對齊 livingmemory 的 `use_session_filtering` 與 `use_persona_filtering` 設定。
  - 無語境任務時，使用本次主動訊息 prompt 作為記憶查詢內容，避免只用時間字串查詢。
- **未回覆衰減預設調整**：
  - `decay_rate` 預設改為留空，代表不衰減、每次都允許觸發。
  - 配置提示補充說明空值、單一值、列表與 `0` 的差異。

### 文件 (Docs)

- 更新 README、AGENTS 與配置提示，補充 Pages 儀表板、Telegram ID 白名單與 livingmemory 過濾邏輯。

---

# 2026/03/25 v2.16.0

## What's Changed

### 新增 (Feat)

- **主動訊息歷史對話裁剪**：
  - `core/llm_helpers.py` 新增 `truncate_history_for_proactive_llm()`，在主動訊息送入 LLM 前先裁剪 history
  - 讀取 AstrBot `provider_settings.max_context_length` 與 `dequeue_context_length`，以對話輪數規則裁剪上下文
  - 透過 `ContextTruncator` 與 `Message.model_validate()` 保持與 AstrBot 既有上下文處理邏輯一致

### 改進 (Improve)

- **主動訊息執行流程優化**：
  - `core/chat_executor.py` 在 `_prepare_and_call_llm()` 中整合歷史紀錄裁剪步驟
  - 歷史清洗後再進行裁剪，降低主動訊息場景的上下文過長與記憶體壓力

### 文件 (Docs)

- **README 與 AGENTS 文件優化**：
  - `README.md` 新增「版本資訊」、「最近更新」與「維護者更新流程」章節
  - 修正聊天指令章節標題的顯示異常字元
  - `AGENTS.md` 新增「文件維護重點」與「版本更新標準流程」，明確規範版本與文件同步更新步驟

---

# 2026/03/12 v2.15.0

## What's Changed

### 新增 (Feat)

- **`interval_weights` 觸發條件支援**：間隔權重分佈現在支援基於未回覆次數的觸發條件。
  - 新增觸發條件語法：
    - `@N`：只在第 N 次未回覆時使用（如 `5-20:0.6@1` 表示第 1 次用 5-20 分鐘）
    - `@N-M`：在第 N 到 M 次未回覆時使用（如 `5-20:0.6@1-3` 表示第 1-3 次）
    - `@N+`：在第 N 次及以後使用（如 `20-60:0.4@4+` 表示第 4 次及以後）
    - 不加 `@` 表示不限制，任何時候都可使用
  - 範例：`"5-20:0.6@1-3,20-60:0.4@4+"` 表示第 1-3 次用 5-20 分鐘，第 4 次及以後用 20-60 分鐘
  - 新增 `_match_trigger_condition()` 函數處理觸發條件匹配邏輯
  - `compute_weighted_interval()` 和 `_pick_from_weights()` 現在接受 `unanswered_count` 參數
  - 更新所有呼叫點以傳遞正確的未回覆次數

### 改進 (Improve)

- **配置文件更新**：
  - 更新 `_conf_schema.json` 中所有 `interval_weights` 的 hint，說明新的觸發條件語法
  - 更新 `AGENTS.md` 文件，新增觸發條件的詳細說明和範例

---

# 2026/03/09 v2.14.2

## What's Changed

### 改進 (Improve)

- **優化 README 流程圖**：
  - 簡化主流程圖為橫向佈局，移除冗餘節點
  - 語境任務生命週期圖改用 flowchart 格式
  - 為所有流程圖節點添加顏色配置，提升可讀性

---

# 2026/03/08 v2.14.1

## What's Changed

### 修復 (Fix)

- **修復主線程訊息阻塞問題**：
  - 將 `_handle_message()` 中的阻塞操作改為背景執行（`asyncio.create_task`）
  - 修復的操作包括：取消自動觸發、重設沉默計時器、排程下次主動訊息
  - 在事件處理器中增加異常捕獲，確保錯誤不影響其他訊息處理
  - 大幅提升訊息回覆的響應速度

### 改進 (Improve)

- **簡化語境感知排程邏輯**：
  - 移除語境分析延遲配置（`analysis_delay_seconds`）
  - 移除不必要的 `asyncio.shield` 和異常處理
  - 優化並行執行流程，減少代碼複雜度

---

# 2026/03/08 v2.14.0

## What's Changed

### 新增 (Feat)

- **`interval_weights` 秒級精度支援**：間隔權重分佈現在支援秒級時間單位。
  - 新增單位後綴：`s`（秒）、`m`（分鐘）
  - 無後綴時預設為分鐘（向下相容舊配置）
  - 每個時間值獨立解析，支援混合寫法
  - 範例：
    - `"30s-60s:0.3,2m-5m:0.5"` → 30-60 秒佔 30% 權重，2-5 分鐘佔 50% 權重
    - `"30-60:0.2,60-120:0.8"` → 30-60 分鐘佔 20%，60-120 分鐘佔 80%（無後綴預設分鐘）
    - `"30s-2m:0.3,5m-10m:0.5,600-1200:0.2"` → 混合寫法：30 秒到 2 分鐘、5-10 分鐘、600-1200 分鐘

### 改進 (Improve)

- **排程邏輯優化**：
  - 新增 `_parse_time_value()` 函數：解析時間值和單位（s/m）
  - 新增 `_to_seconds()` 函數：統一轉換為秒數
  - 重寫 `_pick_from_weights()` 函數：支援單位後綴解析與混合格式

### 文件 (Docs)

- 更新 AGENTS.md，新增「間隔權重分佈」詳細說明與範例
- 更新 `.kiro/steering/scheduler-logic.md`，新增間隔權重分佈章節
- CONTRIBUTING.md 轉換為繁體中文（台灣標準）
- 配置 schema 的 4 處 `interval_weights` hint 文字更新，說明單位後綴用法

### 其他 (Chore)

- `.gitignore` 新增 `*.code-workspace` 規則，避免 VS Code 工作區文件被提交

---

# 2026/03/08 v2.13.0

## What's Changed

### 新增 (Feat)

- **未回覆計數重置模式**：每個時段規則現在可以配置獨立的計數重置策略。
  - 新增 `unanswered_reset_mode` 配置項，支援三種模式：
    - `"inherit"`（繼承）：延續上一時段的計數，不重置（預設）
    - `"reset"`（重新計數）：切換到該時段時從 0 開始
    - `"custom"`（自訂起始值）：從 `unanswered_start_count` 指定的數字開始累加
  - 新增 `unanswered_start_count` 配置項：當選擇「自訂起始值」模式時，指定起始計數值（0-20）
  - 實現時段切換時的自動計數調整，並記錄日誌

### 改進 (Improve)

- **排程邏輯優化**：
  - 新增 `get_time_slot_reset_count()` 函數，根據當前時段規則取得重置計數值
  - `_schedule_next_chat_and_save()` 現在會在排程時檢查並應用時段重置邏輯
  - 時段切換時會記錄日誌，顯示計數變化（如「從 5 重置為 0」或「從 3 調整為 2」）

### 使用場景

- 白天時段達到上限後，切換到晚上時段可以重新開始（設為 `"reset"`）
- 深夜時段想從較高的計數開始，避免過於頻繁（設為 `"custom"` + `unanswered_start_count=2`）
- 全天保持連續計數，不因時段切換而重置（設為 `"inherit"`，預設值）

### 文件 (Docs)

- 更新 AGENTS.md，新增「未回覆計數重置模式」章節
- 配置 schema 的 hint 文字更新，說明三種重置模式的用法和使用場景

---

# 2026/03/08 v2.12.0

## What's Changed

### 新增 (Feat)

- **分鐘級別時段設定**：時段規則現在支援分鐘級別的精確時間設定。
  - 新增 `start_minute` 和 `end_minute` 配置項（0-59），可精確定義時段如 08:30-23:45。
  - 支援跨日時段，例如 22:30-06:15。
  - 時段匹配邏輯升級為 `_time_in_range()`，提供分鐘級別精度。

- **時段專屬最大未回覆次數**：每個時段規則可配置獨立的 `max_unanswered_times`。
  - 填 0 表示使用全域設定，填大於 0 的值則覆蓋全域設定。
  - 實現差異化策略：例如白天時段設為 5 次，深夜時段設為 2 次。
  - 優先級：時段專屬上限 > 全域上限。

### 改進 (Improve)

- **排程邏輯優化**：
  - `compute_weighted_interval()` 和 `should_trigger_by_unanswered()` 現在使用分鐘級別時段匹配。
  - 新增 `_resolve_decay_list_and_rule()` 函數，同時回傳衰減列表和匹配的規則，避免重複查找。
  - 日誌輸出改進，時段顯示格式為 `HH:MM-HH:MM`（如 `08:30-23:45`）。

### 文件 (Docs)

- 更新 AGENTS.md，新增「時段時間設定」和「時段專屬上限」章節。
- 配置 schema 的 hint 文字更新，說明分鐘級別時段和時段專屬上限的用法。

---

# 2026/03/08 v2.11.2

## What's Changed

### 修復 (Fix)

- **語境感知效能優化**：修復語境感知功能導致主要 AI 回覆延遲的效能問題。
  - 在 `handle_context_aware_scheduling()` 開頭加入可配置的延遲（預設 0.5 秒），確保語境分析在主要 AI 回覆啟動後才執行，避免資源競爭。
  - 優化 `check_should_cancel_tasks_batch()` 的快速路徑，無待執行任務時立即回傳，避免不必要的 LLM 請求。

### 新增 (Feat)

- **新增配置項 `analysis_delay_seconds`**：在 `context_aware_settings` 中新增 `analysis_delay_seconds` 配置項（預設 0.5 秒），允許使用者根據自己的 LLM API 速度調整延遲時間。較慢的 LLM API 建議設為 1.0 秒以上。
---

# 2026/02/27 v2.11.1

## What's Changed

### 文件 (Docs)

- 將 Telegram 平台支援狀態從「理論支援（未測試）」更新為「完整支援」。

---

# 2026/02/27 v2.11.0

## What's Changed

### 新增 (Feat)

- **`/proactive help` 指令**：新增幫助子指令，顯示所有可用的主動訊息管理指令。輸入 `/proactive`（不帶子指令）時預設執行 help。

### 文件 (Docs)

- 三語 README（繁中 / EN / JP）新增「聊天指令」章節，列出所有可用指令。

---

# 2026/02/27 v2.10.3

## What's Changed

### 重構 (Refactor)

- **通用 JSON 解析器**：新增 `parse_llm_json()` 統一處理 LLM 回應的 JSON 解析，取代各模組中的重複邏輯。
- **UMO 容錯包裝器**：新增 `with_umo_fallback()` / `async_with_umo_fallback()` 統一處理 UMO 格式相容問題。
- **對話歷史載入器**：新增 `load_conversation_history()` 封裝對話歷史取得與解析流程。
- **訊息類型常數**：新增 `MSG_TYPE_FRIEND`、`MSG_TYPE_GROUP` 等常數，取代散落各處的字串字面量。
- **Session ID 精確比對**：新增 `_is_target_match()` 避免數字 ID 的子字串誤匹配。
- **正則快取**：`messaging.py` 的分段正則改用 `lru_cache` 快取編譯結果。
- **型別標註**：為所有公開函數補齊參數與回傳值型別標註。
- **未使用變數清理**：移除 `check_and_chat` 中未使用的 `session_config` 初始化、`_deliver_and_finalize` 中未使用的 `ctx_task` 參數。
- **錯誤處理統一**：所有 `except` 區塊補齊函數名稱與 session_id 上下文，消除裸露的 `except: pass`。
- **持久化修正**：`restore_pending_context_tasks()` 回傳 `bool`，`initialize()` 據此決定是否持久化。
- **匯出清單修正**：移除 `core/__init__.py` 中不存在的 `finalize_and_reschedule` 匯出。
- `ruff format` + `ruff check` 零錯誤。

---

# 2026/02/27 v2.10.2

## What's Changed

### 重構 (Refactor)

- **`chat_executor.py` 結構重構**：將巨型 `check_and_chat()` 拆分為獨立的子步驟函數，提升可讀性與可維護性：
  - `_check_preconditions()` — 免打擾 / 衰減 / 硬性上限檢查
  - `_resolve_session_umo()` — 動態修正 UMO（平台重啟容錯）
  - `_prepare_and_call_llm()` — 準備請求、構造 Prompt、呼叫 LLM
  - `_deliver_and_finalize()` — 發送訊息、存檔歷史、重新排程
  - `_handle_fatal_error()` — 統一錯誤恢復邏輯
- 提取 `_format_last_reply_time()`、`_find_context_task()`、`_state_changed_during_generation()` 等輔助函數。
- 使用 `frozenset` 常數管理無效回應與認證錯誤關鍵字。

---

# 2026/02/27 v2.10.1

## What's Changed

### 修復 (Fix)

- **修復死鎖導致機器人無回應**：`check_and_chat()` 步驟 2 在持有 `data_lock` 的情況下呼叫 `_schedule_next_chat_and_save()`（內部再次取鎖），因 `asyncio.Lock` 不可重入而造成永久死鎖。將判定結果處理移至鎖外修復。

---

# 2026/02/27 v2.10.0

## What's Changed

### 重構 (Refactor)

- **指令組化**：將 `/proactive_tasks` 改為指令組模式 `/proactive tasks`，方便後續擴充更多子指令。

---

# 2026/02/26 v2.9.0

## What's Changed

### 新增功能 (Feat)

- **批量語境任務取消檢查**：新增 `check_should_cancel_tasks_batch()` 函數，將多個語境任務的取消判斷合併為單一 LLM 請求，大幅降低 API 呼叫次數與成本。
- 新增 `check_cancel_batch.txt` 與 `check_cancel_batch_system.txt` prompt 模板，支援批量取消判斷。
- 新增 `_parse_json_array_response()` 輔助函數，用於解析 LLM 回傳的 JSON 陣列。

### 優化 (Opt)

- **預設使用批量檢查**：`maybe_cancel_pending_context_task()` 現在預設使用批量 LLM 請求，取代原本的並行多次請求模式。
  - 例如：5 個待檢查任務從 5 次 LLM 請求減少為 1 次
  - 保持相同的功能，但更高效且成本更低

---

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
