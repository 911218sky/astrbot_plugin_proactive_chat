(() => {
  "use strict";

  const state = {
    tasks: [],
    filter: "all",
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
      if (!query) return true;
      return searchableText(task).includes(query);
    });
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
      body.innerHTML = '<tr><td colspan="6" class="empty">沒有符合條件的任務</td></tr>';
      return;
    }

    body.innerHTML = tasks
      .map((task) => {
        const typeLabel = TYPE_LABELS[task.type] || task.type || "未知";
        const enabled = task.enabled ? "已啟用" : "未啟用";
        const messageType = task.message_type ? ` · ${escapeHtml(task.message_type)}` : "";
        const target = task.target_id ? ` · ${escapeHtml(task.target_id)}` : "";
        const detail = task.detail || (task.extra && task.extra.hint) || "";
        const lastMessage = task.last_message_time
          ? `<span class="meta">最後訊息：${escapeHtml(task.last_message_time)}</span>`
          : "";
        return `
          <tr>
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
          </tr>
        `;
      })
      .join("");
  }

  async function refresh(showDone) {
    try {
      const data = await apiGet("tasks", {});
      state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
      renderSummary(data.summary || {});
      renderTasks();
      if (showDone) showToast("已刷新");
    } catch (error) {
      byId("subtitle").textContent = error.message || "載入失敗";
      byId("task-body").innerHTML =
        `<tr><td colspan="6" class="empty">${escapeHtml(error.message || "載入失敗")}</td></tr>`;
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
    byId("search-input").addEventListener("input", (event) => {
      state.query = event.target.value;
      renderTasks();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    syncAutoRefresh();
    refresh(false);
  });
})();
