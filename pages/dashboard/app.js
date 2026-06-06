(() => {
  "use strict";

  const state = {
    tasks: [],
    sessions: [],
    filter: "all",
    sessionFilter: "all",
    enabledFilter: "all",
    query: "",
    timer: null,
  };

  const TYPE_LABELS = {
    regular: "一般排程",
    context: "語境預測",
    auto_trigger: "自動觸發",
    group_idle: "群聊沉默",
    context_orphan: "未追蹤語境",
  };

  function endpoint(path) {
    return "page/" + String(path).replace(/^\/+/, "").replace(/\/+/g, "/");
  }

  async function apiGet(path, params) {
    const bridge = window.AstrBotPluginPage;
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error("目前頁面必須從 AstrBot 官方插件 Pages 開啟");
    }
    const response = await bridge.apiGet(endpoint(path), params || {});
    if (response && response.status === "error") {
      throw new Error(response.message || "API 請求失敗");
    }
    return response && response.status === "ok" ? response.data || {} : response || {};
  }

  async function apiPost(path, body) {
    const bridge = window.AstrBotPluginPage;
    if (!bridge || typeof bridge.apiPost !== "function") {
      throw new Error("目前頁面必須從 AstrBot 官方插件 Pages 開啟");
    }
    const response = await bridge.apiPost(endpoint(path), body || {});
    if (response && response.status === "error") {
      throw new Error(response.message || "API 請求失敗");
    }
    return response && response.status === "ok" ? response.data || {} : response || {};
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function showToast(message) {
    const el = byId("toast");
    el.textContent = message;
    el.classList.remove("visible");
    void el.offsetWidth;
    el.classList.add("visible");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => el.classList.remove("visible"), 2600);
  }

  function formatRemaining(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return "";
    let value = Math.max(0, Number(seconds));
    const days = Math.floor(value / 86400);
    value %= 86400;
    const hours = Math.floor(value / 3600);
    value %= 3600;
    const minutes = Math.floor(value / 60);
    if (days > 0) return `${days} 天 ${hours} 小時`;
    if (hours > 0) return `${hours} 小時 ${minutes} 分`;
    return `${minutes} 分`;
  }

  function searchableText(task) {
    return [
      task.title,
      task.session_label,
      task.session_id,
      task.target_id,
      task.detail,
      task.extra && task.extra.reason,
      task.extra && task.extra.hint,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  function filteredTasks() {
    const query = state.query.trim().toLowerCase();
    return state.tasks.filter((task) => {
      if (state.filter !== "all" && task.type !== state.filter) return false;
      if (state.enabledFilter === "enabled" && !task.enabled) return false;
      if (state.enabledFilter === "disabled" && task.enabled) return false;
      if (state.sessionFilter === "private" && String(task.message_type).toLowerCase().includes("group")) return false;
      if (state.sessionFilter === "group" && !String(task.message_type).toLowerCase().includes("group")) return false;
      if (!query) return true;
      return searchableText(task).includes(query);
    });
  }

  function renderSessionSelect() {
    const select = byId("session-select");
    if (!state.sessions.length) {
      select.innerHTML = '<option value="">沒有可用會話</option>';
      return;
    }
    select.innerHTML = state.sessions
      .filter((session) => session.enabled)
      .map((session) => {
        const label = `${session.label || session.session_id} · ${session.session_id}`;
        return `<option value="${escapeHtml(session.session_id)}">${escapeHtml(label)}</option>`;
      })
      .join("");
  }

  function renderSummary(summary) {
    byId("metric-total").textContent = summary.total_count || 0;
    byId("metric-regular").textContent = summary.regular_count || 0;
    byId("metric-context").textContent = summary.context_count || 0;
    byId("metric-timers").textContent =
      (summary.auto_trigger_count || 0) + (summary.group_idle_count || 0);
    byId("subtitle").textContent = [
      summary.scheduler_running ? "排程器運行中" : "排程器未運行",
      `時區：${summary.timezone || "local"}`,
      `更新：${summary.generated_at || ""}`,
    ].join(" · ");
  }

  function renderTasks() {
    const body = byId("task-body");
    const tasks = filteredTasks();
    if (!tasks.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty">沒有符合條件的任務</td></tr>';
      return;
    }

    body.innerHTML = tasks
      .map((task) => {
        const typeLabel = TYPE_LABELS[task.type] || task.type || "未知";
        const enabled = task.enabled ? "已啟用" : "未啟用";
        const messageType = task.message_type ? ` · ${escapeHtml(task.message_type)}` : "";
        const target = task.target_id ? ` · ${escapeHtml(task.target_id)}` : "";
        const detail = task.detail || (task.extra && task.extra.hint) || "";
        const canDelete = ["regular", "context", "context_orphan", "auto_trigger", "group_idle"].includes(task.type);
        const canReschedule = ["regular", "context", "auto_trigger", "group_idle"].includes(task.type);
        const canRunNow = Boolean(task.enabled);
        const lastMessage = task.last_message_time
          ? `<span class="meta">最後訊息：${escapeHtml(task.last_message_time)}</span>`
          : "";
        return `
          <tr data-task-id="${escapeHtml(task.id)}" data-task-type="${escapeHtml(task.type)}" data-session-id="${escapeHtml(task.session_id)}">
            <td><span class="badge ${escapeHtml(task.type)}">${escapeHtml(typeLabel)}</span></td>
            <td>
              <div class="session">
                <strong>${escapeHtml(task.session_label || task.session_id)}</strong>
                <span class="meta">${escapeHtml(enabled)}${messageType}${target}</span>
                ${lastMessage}
              </div>
            </td>
            <td>${escapeHtml(task.next_run_time || "未知")}</td>
            <td>${escapeHtml(formatRemaining(task.remaining_seconds) || "未知")}</td>
            <td>${escapeHtml(task.unanswered_count || 0)}</td>
            <td class="detail">${escapeHtml(detail || "無描述")}</td>
            <td>
              <div class="row-actions">
                <button type="button" data-action="run-now" ${canRunNow ? "" : "disabled"} title="${canRunNow ? "立即執行" : "會話未啟用"}">執行</button>
                <button type="button" data-action="reschedule" ${canReschedule ? "" : "disabled"} title="使用上方時間修改">改期</button>
                <button type="button" data-action="delete" ${canDelete ? "" : "disabled"} class="danger" title="刪除任務">刪除</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function readSchedulePayload() {
    const runAt = byId("run-at-input").value;
    const delay = byId("delay-input").value;
    return {
      run_at: runAt || "",
      delay_minutes: runAt ? "" : delay,
    };
  }

  function scheduleDescription() {
    const runAt = byId("run-at-input").value;
    if (runAt) return `指定時間 ${runAt}`;
    return `${byId("delay-input").value || 10} 分鐘後`;
  }

  async function createTask() {
    const sessionId = byId("session-select").value;
    if (!sessionId) {
      showToast("請先選擇會話");
      return;
    }
    await apiPost("tasks/action", {
      action: "create",
      session_id: sessionId,
      ...readSchedulePayload(),
    });
    showToast("任務已新增");
    await refresh(false);
  }

  async function handleTaskAction(button) {
    const row = button.closest("tr");
    if (!row) return;
    const action = button.dataset.action;
    const taskId = row.dataset.taskId;
    const taskType = row.dataset.taskType;
    const sessionId = row.dataset.sessionId;

    if (action === "delete" && !window.confirm("確定刪除此任務？")) return;

    const payload = {
      task_id: taskId,
      task_type: taskType,
      session_id: sessionId,
    };
    if (action === "run-now") {
      await apiPost("tasks/action", { action: "run_now", ...payload });
      showToast("已送出立即執行");
    } else if (action === "reschedule") {
      if (!window.confirm(`確定將此任務改到 ${scheduleDescription()}？`)) return;
      await apiPost("tasks/action", {
        action: "reschedule",
        ...payload,
        ...readSchedulePayload(),
      });
      showToast("任務時間已更新");
    } else if (action === "delete") {
      await apiPost("tasks/action", { action: "delete", ...payload });
      showToast("任務已刪除");
    }
    await refresh(false);
  }

  async function refresh(showDone) {
    try {
      const data = await apiGet("tasks", {});
      state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
      state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
      renderSummary(data.summary || {});
      renderSessionSelect();
      renderTasks();
      if (showDone) showToast("已刷新");
    } catch (error) {
      byId("subtitle").textContent = error.message || "載入失敗";
      byId("task-body").innerHTML =
        `<tr><td colspan="7" class="empty">${escapeHtml(error.message || "載入失敗")}</td></tr>`;
      showToast(error.message || "載入失敗");
    }
  }

  function syncAutoRefresh() {
    clearInterval(state.timer);
    state.timer = null;
    if (byId("auto-refresh").checked) {
      state.timer = setInterval(() => refresh(false), 10000);
    }
  }

  function bindEvents() {
    byId("refresh-button").addEventListener("click", () => refresh(true));
    byId("auto-refresh").addEventListener("change", syncAutoRefresh);
    byId("type-filter").addEventListener("change", (event) => {
      state.filter = event.target.value;
      renderTasks();
    });
    byId("session-filter").addEventListener("change", (event) => {
      state.sessionFilter = event.target.value;
      renderTasks();
    });
    byId("enabled-filter").addEventListener("change", (event) => {
      state.enabledFilter = event.target.value;
      renderTasks();
    });
    byId("search-input").addEventListener("input", (event) => {
      state.query = event.target.value;
      renderTasks();
    });
    byId("create-task-button").addEventListener("click", async () => {
      try {
        await createTask();
      } catch (error) {
        showToast(error.message || "新增失敗");
      }
    });
    byId("task-body").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button || button.disabled) return;
      try {
        await handleTaskAction(button);
      } catch (error) {
        showToast(error.message || "操作失敗");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    syncAutoRefresh();
    refresh(false);
  });
})();
