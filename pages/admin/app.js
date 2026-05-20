const API_PREFIX = "admin";
const SCHEDULER_POLL_INTERVAL_MS = 5000;

const TABS = [
  { id: "dashboard", label: "Dashboard" },
  { id: "learning", label: "主动学习" },
  { id: "reviews", label: "表达审核" },
  { id: "memories", label: "记忆网络" },
  { id: "users", label: "用户画像" },
  { id: "personaSlices", label: "角色切片" },
];

function normalizeTabId(tab) {
  if (tab === "persona") return "personaSlices";
  if (tab === "settings") return "dashboard";
  return TABS.some((item) => item.id === tab) ? tab : "dashboard";
}

const SLICE_FIELDS = [
  ["memory_points", "长期记忆点"],
  ["identity_points", "身份认知点"],
  ["preference_points", "偏好点"],
  ["relationship_points", "关系点"],
  ["speech_style_points", "说话风格点"],
];

const state = {
  bridge: null,
  current: normalizeTabId(location.hash.replace("#", "") || "dashboard"),
  dashboardTab: "overview",
  reviewTab: "pending",
  memoryTab: "events",
  schedulerStatus: null,
  schedulerDueSelection: null,
  schedulerChatLoop: null,
  schedulerChatId: "",
  schedulerPollTimer: null,
  selectedReviews: new Set(),
  activeUserId: "",
  cache: {
    reviews: { pending: [], all: { items: [], total: 0, page: 1, page_size: 20 }, filters: { status: "", group_id: "", keyword: "" } },
    memories: { month: new Date().toISOString().slice(0, 7), events: [], reflections: [], nodes: [], jargon: [] },
    users: [],
    personaSlices: {},
    personaSlicesError: null,
    turns: [],
  },
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const content = () => $("#content");

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function attr(value) {
  return escapeHtml(value);
}

function segment(value) {
  return encodeURIComponent(String(value));
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(String(value));
  return String(value).replace(/["\\]/g, "\\$&");
}

function json(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function parseJsonSafe(text, fallback) {
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

function formatTime(value) {
  const ts = Number(value || 0);
  if (!ts) return "无";
  return new Date(ts * 1000).toLocaleString();
}

function formatPercent(value) {
  const num = Number(value || 0);
  return `${(num <= 1 ? num * 100 : num).toFixed(1)}%`;
}

function asItems(value) {
  if (Array.isArray(value)) return value;
  if (Array.isArray(value?.items)) return value.items;
  return [];
}

function schedulerReport() {
  return state.schedulerDueSelection?.data?.report || {};
}

function schedulerOverview() {
  return state.schedulerStatus?.data?.overview || {};
}

function clampPercent(value) {
  const num = Number(value || 0);
  return Math.max(0, Math.min(100, num <= 1 ? num * 100 : num));
}

function setBridgeStatus(text, kind = "") {
  const node = $("#bridge-status");
  node.textContent = text;
  node.className = `status-pill ${kind}`.trim();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2800);
}

function ensureBridge() {
  if (!state.bridge) throw new Error("AstrBotPluginPage bridge is not ready");
  return state.bridge;
}

function pluginEndpoint(path) {
  const [rawPath, rawQuery = ""] = String(path || "").split("?");
  const cleanPath = rawPath.replace(/^\/+/, "");
  const cleanPrefix = API_PREFIX.replace(/^\/+|\/+$/g, "");
  const endpoint = cleanPath ? `${cleanPrefix}/${cleanPath}` : cleanPrefix;
  const params = {};
  if (rawQuery) {
    new URLSearchParams(rawQuery).forEach((value, key) => {
      params[key] = value;
    });
  }
  return { endpoint, params: Object.keys(params).length ? params : undefined };
}

function apiErrorMessage(payload) {
  if (!payload) return "请求失败";
  if (typeof payload === "string") return payload;
  if (payload.message) return payload.message;
  if (payload.detail) return typeof payload.detail === "string" ? payload.detail : json(payload.detail);
  if (payload.error) return typeof payload.error === "string" ? payload.error : json(payload.error);
  if (Array.isArray(payload.errors) && payload.errors.length) {
    return payload.errors.map((item) => item.message || item.detail || json(item)).join("; ");
  }
  return json(payload);
}

function unwrapResponse(result) {
  if (result && (result.status === "error" || result.ok === false)) {
    throw new Error(apiErrorMessage(result));
  }
  return result;
}

async function readyBridge(bridge) {
  if (typeof bridge.ready === "function") {
    return bridge.ready();
  }
  if (typeof bridge.initialize === "function") {
    return bridge.initialize();
  }
  return bridge.getContext ? bridge.getContext() : {};
}

function waitForBridge(timeoutMs = 6000) {
  if (window.AstrBotPluginPage) return Promise.resolve(window.AstrBotPluginPage);
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const timer = setInterval(() => {
      if (window.AstrBotPluginPage) {
        clearInterval(timer);
        resolve(window.AstrBotPluginPage);
        return;
      }
      if (Date.now() - startedAt >= timeoutMs) {
        clearInterval(timer);
        reject(new Error("AstrBotPluginPage bridge was not injected"));
      }
    }, 60);
  });
}

const api = {
  async get(path) {
    const { endpoint, params } = pluginEndpoint(path);
    return unwrapResponse(await ensureBridge().apiGet(endpoint, params));
  },
  async post(path, body = {}) {
    const { endpoint } = pluginEndpoint(path);
    return unwrapResponse(await ensureBridge().apiPost(endpoint, body));
  },
};

function renderTabs() {
  $("#tabs").innerHTML = TABS.map((tab) => `
    <button class="tab-button ${state.current === tab.id ? "active" : ""}" data-tab="${tab.id}" type="button">${tab.label}</button>
  `).join("");
  $$("[data-tab]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.tab)));
}

function navigate(tab) {
  state.current = normalizeTabId(tab);
  location.hash = state.current;
  renderTabs();
  loadCurrent();
}

function pageHeader(title, subtitle, actions = "") {
  return `
    <div class="page-header">
      <div>
        <h2>${title}</h2>
        <p>${subtitle}</p>
      </div>
      <div class="row-actions">${actions}</div>
    </div>
  `;
}

function section(title, subtitle, body, actions = "") {
  return `
    <section class="panel">
      <div class="section-head">
        <div>
          <h3>${title}</h3>
          ${subtitle ? `<p>${subtitle}</p>` : ""}
        </div>
        <div class="row-actions">${actions}</div>
      </div>
      ${body}
    </section>
  `;
}

function subTabs(active, tabs, attrName = "subtab") {
  return `
    <div class="subtabs">
      ${tabs.map((tab) => `
        <button class="subtab-button ${active === tab.id ? "active" : ""}" data-${attrName}="${tab.id}" type="button">${tab.label}</button>
      `).join("")}
    </div>
  `;
}

function metric(label, value, hint = "", kind = "") {
  return `
    <div class="metric ${kind}">
      <span class="metric-label">${label}</span>
      <span class="metric-value">${escapeHtml(value ?? "-")}</span>
      ${hint ? `<span class="muted">${hint}</span>` : ""}
    </div>
  `;
}

function statusChip(text, kind = "") {
  return `<span class="chip ${kind}">${escapeHtml(text)}</span>`;
}

function progressBar(value, label = "") {
  const width = clampPercent(value);
  return `
    <div class="progress-wrap">
      <div class="progress-bar"><span style="width:${width}%"></span></div>
      ${label ? `<span class="progress-label">${escapeHtml(label)}</span>` : ""}
    </div>
  `;
}

function formField(name, label, value, type = "text") {
  return `<label>${escapeHtml(label)}<input data-user-field="${attr(name)}" type="${attr(type)}" value="${attr(value)}"></label>`;
}

function table(headers, rows, empty = "暂无数据") {
  if (!rows || rows.length === 0) {
    return `<div class="empty-state"><p>${empty}</p></div>`;
  }
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>${headers.map((item) => `<th>${item}</th>`).join("")}</tr></thead>
        <tbody>${rows.join("")}</tbody>
      </table>
    </div>
  `;
}

function showLoading(title = "正在读取数据...") {
  content().innerHTML = `<section class="empty-state"><h2>${escapeHtml(title)}</h2><p>请稍候，正在通过 AstrBot Plugin Page bridge 读取插件数据。</p></section>`;
}

function openModal(title, body, footer = "") {
  const root = $("#modal-root");
  root.hidden = false;
  root.innerHTML = `
    <div class="modal-backdrop" data-modal-close></div>
    <section class="modal-card">
      <header class="modal-head">
        <h3>${title}</h3>
        <button class="ghost-button" data-modal-close type="button">关闭</button>
      </header>
      <div class="modal-body">${body}</div>
      ${footer ? `<footer class="modal-foot">${footer}</footer>` : ""}
    </section>
  `;
  $$("[data-modal-close]", root).forEach((node) => node.addEventListener("click", closeModal));
}

function closeModal() {
  const root = $("#modal-root");
  root.hidden = true;
  root.innerHTML = "";
}

function confirmAction(message, title = "确认操作") {
  const modalTitle = title === "danger" ? "危险操作" : title;
  return new Promise((resolve) => {
    openModal(
      modalTitle,
      `<p class="confirm-message">${escapeHtml(message)}</p>`,
      `<button class="ghost-button" data-confirm-no type="button">取消</button><button class="danger-button" data-confirm-yes type="button">确认</button>`,
    );
    $("[data-confirm-no]").addEventListener("click", () => {
      closeModal();
      resolve(false);
    });
    $("[data-confirm-yes]").addEventListener("click", () => {
      closeModal();
      resolve(true);
    });
  });
}

function openFormModal(title, fields, initial, onSubmit, submitText = "保存") {
  const body = `
    <form id="modal-form" class="form-grid single">
      ${fields.map((field) => {
        const value = initial?.[field.name] ?? field.default ?? "";
        if (field.type === "textarea") {
          return `<label>${field.label}<textarea name="${field.name}" rows="${field.rows || 5}">${escapeHtml(value)}</textarea></label>`;
        }
        if (field.type === "select") {
          return `<label>${field.label}<select name="${field.name}">${(field.options || []).map((option) => `<option value="${attr(option.value)}" ${String(value) === String(option.value) ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select></label>`;
        }
        return `<label>${field.label}<input name="${field.name}" type="${field.type || "text"}" value="${attr(value)}"></label>`;
      }).join("")}
    </form>
  `;
  openModal(title, body, `<button class="ghost-button" data-modal-close type="button">取消</button><button class="primary-button" data-modal-submit type="button">${escapeHtml(submitText)}</button>`);
  $("[data-modal-submit]").addEventListener("click", async () => {
    const form = $("#modal-form");
    const data = {};
    fields.forEach((field) => {
      const node = form.elements[field.name];
      if (field.cast === "float") data[field.name] = Number.parseFloat(node.value || "0");
      else if (field.cast === "int") data[field.name] = Number.parseInt(node.value || "0", 10);
      else data[field.name] = node.value;
    });
    await onSubmit(data);
    closeModal();
  });
}

function openJsonModal(title, value, onSubmit) {
  openModal(
    title,
    `<textarea id="json-modal-editor" rows="18">${escapeHtml(json(value))}</textarea>`,
    `<button class="ghost-button" data-modal-close type="button">取消</button><button class="primary-button" data-json-save type="button">保存</button>`,
  );
  $("[data-json-save]").addEventListener("click", async () => {
    try {
      await onSubmit(JSON.parse($("#json-modal-editor").value));
      closeModal();
    } catch (error) {
      toast(`JSON 保存失败：${error.message || error}`);
    }
  });
}

async function loadDashboard() {
  showLoading("正在读取运行状态大盘...");
  if (state.dashboardTab === "overview") {
    stopSchedulerPolling();
    return renderDashboardOverview();
  }
  if (state.dashboardTab === "heartflow") {
    stopSchedulerPolling();
    return renderDashboardHeartflow();
  }
  if (state.dashboardTab === "cognition") {
    await renderDashboardCognition();
    startSchedulerPolling();
    return;
  }
  stopSchedulerPolling();
  return renderDashboardTools();
}

function dashboardShell(body) {
  content().innerHTML = `
    ${pageHeader("运行状态大盘", "实时监控 AstrMai 核心系统的生命周期、心流、认知和工具链。")}
    ${subTabs(state.dashboardTab, [
      { id: "overview", label: "运行概览" },
      { id: "heartflow", label: "心智流 Heartflow" },
      { id: "cognition", label: "主动决策池 Cognition" },
      { id: "tools", label: "工具链观测 Tools" },
    ], "dashboard-tab")}
    ${body}
  `;
  $$('[data-dashboard-tab]').forEach((button) => button.addEventListener("click", () => {
    state.dashboardTab = button.dataset.dashboardTab;
    loadDashboard();
  }));
}

async function renderDashboardOverview() {
  const [snapshot, health, capabilities, models] = await Promise.all([
    api.get("/dashboard").catch(() => ({})),
    api.get("/runtime/health").catch(() => ({})),
    api.get("/runtime/capabilities").catch(() => ({})),
    api.get("/runtime/models").catch(() => ({})),
  ]);
  const healthData = health.data || {};
  const running = Boolean(healthData.running);
  dashboardShell(`
    <div class="health-strip ${running ? "ok" : "warn"}">
      <span class="status-dot ${running ? "ok" : "warn"}"></span>
      <div>
        <strong>${running ? (healthData.degraded_count > 0 ? "部分降级运行" : "全系统健康运行") : "系统状态未确认"}</strong>
        <p>阶段：${escapeHtml(healthData.boot_phase || "unknown")}，降级组件：${healthData.degraded_count ?? 0}</p>
      </div>
    </div>
    <div class="grid">
      ${metric("总用户数", snapshot.total_users)}
      ${metric("待审核项", snapshot.pending_reviews)}
      ${metric("记忆事件", snapshot.total_memory_events)}
      ${metric("数据库大小", snapshot.database_size || "-")}
    </div>
    <div class="grid two">
      ${section("能力矩阵", "Capabilities", `<pre>${json(capabilities.data || capabilities)}</pre>`)}
      ${section("模型与健康诊断", "Models / Health", `<pre>${json({ models: models.data || models, health: healthData })}</pre>`)}
    </div>
  `);
}

async function renderDashboardHeartflow() {
  const [status, chats, impulses, timeline, digests, intents] = await Promise.all([
    api.get("/heartflow/status").catch(() => ({})),
    api.get("/heartflow/chats").catch(() => ({ items: [] })),
    api.get("/heartflow/impulses?limit=50").catch(() => ({ items: [] })),
    api.get("/heartflow/timeline?limit=80").catch(() => ({ items: [] })),
    api.get("/heartflow/topic-digests?limit=50").catch(() => ({ items: [] })),
    api.get("/proactive/intents?limit=50").catch(() => ({ items: [] })),
  ]);
  const impulseRows = asItems(impulses).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.pulse_type || "-")}</td>
      <td>${item.visible_candidate_allowed ? statusChip("allowed", "ok") : statusChip(item.blocked_reason || "hidden", "")}</td>
      <td>${item.requires_synthetic_event ? statusChip("synthetic", "warn") : statusChip("hidden", "")}</td>
      <td>${String(Boolean(item.dispatch_enabled))}</td>
      <td>${String(Boolean(item.synthetic_event_queued))}</td>
      <td><pre>${json(item.safety_checks || {})}</pre></td>
    </tr>
  `);
  const timelineRows = asItems(timeline).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.kind || "-")}</td>
      <td>${escapeHtml(item.label || "-")}</td>
      <td>${escapeHtml(item.summary || "-")}</td>
      <td><pre>${json(item.payload || {})}</pre></td>
    </tr>
  `);
  const digestRows = asItems(digests).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${item.status === "written" ? statusChip("written", "ok") : statusChip(item.skip_reason || "skipped", "warn")}</td>
      <td>${escapeHtml(item.summary || "-")}</td>
      <td>${escapeHtml((item.tags || []).join(", ") || "-")}</td>
      <td>${escapeHtml(item.importance ?? "-")}</td>
    </tr>
  `);
  const intentRows = asItems(intents).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp || item.created_at)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.source || "-")}</td>
      <td>${item.reply_sent ? statusChip("sent", "ok") : statusChip(item.blocked_reason || item.status || "queued", item.blocked_reason ? "warn" : "")}</td>
      <td>${escapeHtml(item.blocked_reason || "-")}</td>
      <td>${escapeHtml(item.reason || item.guidance || "-")}</td>
    </tr>
  `);
  const rows = asItems(chats).map((item) => {
    const session = item.session || {};
    const action = item.latest_action_decision || {};
    return `
    <tr>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${item.talk_willingness ?? "-"}</td>
      <td>${item.interest ?? "-"}</td>
      <td>${item.silence_pressure ?? "-"}</td>
      <td>${escapeHtml(action.action_type || session.last_impulse || "-")}</td>
      <td>${escapeHtml(session.tick_count ?? "-")}</td>
      <td>${escapeHtml(session.talk_frequency_adjust ?? "-")}</td>
      <td>${escapeHtml(session.insert_pressure ?? "-")}</td>
      <td>${escapeHtml(session.reply_pressure ?? "-")}</td>
      <td>${escapeHtml(session.visible_candidate_score ?? "-")}</td>
      <td>${escapeHtml(session.topic_heat ?? "-")}</td>
      <td>${escapeHtml([
        `observe:${session.consecutive_observe_count ?? 0}`,
        `no_reply:${session.consecutive_no_reply_count ?? 0}`,
        `prepare:${session.consecutive_prepare_count ?? 0}`,
      ].join(" / "))}</td>
      <td>${(item.cooldown_tags || []).map((tag) => statusChip(tag, "warn")).join(" ")}</td>
      <td class="row-actions">
        <button class="ghost-button" data-heartflow-impulses="${attr(item.chat_id)}" type="button">Impulse Safety</button>
        <button class="ghost-button" data-heartflow-timeline="${attr(item.chat_id)}" type="button">Timeline</button>
        <button class="ghost-button" data-hidden-context="${attr(item.chat_id)}" type="button">隐藏上下文</button>
        <button class="danger-button" data-clear-heartflow="${attr(item.chat_id)}" type="button">清理 cooldown</button>
      </td>
    </tr>
  `;
  });
  dashboardShell(`
    ${section("心智流状态", "Heartflow manager describe_status()", `<pre>${json(status.data || status)}</pre>`)}
    ${section("Heartflow Sessions", "Session / Rhythm / Hidden Action", table(["Chat", "Talk", "Interest", "Silence", "Hidden Action", "Session", "Talk Freq", "Insert", "Reply", "Score", "Topic", "Rhythm", "Cooldowns", "操作"], rows))}
  `);
  content().insertAdjacentHTML("beforeend", section("Impulse Safety", "Heartflow impulse safety decisions: v1 only records candidate state; dispatch_enabled=false means no visible message is sent.", table(["Time", "Chat", "Pulse", "Candidate", "Synthetic", "Dispatch", "Queued", "Safety"], impulseRows)));
  content().insertAdjacentHTML("beforeend", section("Heartflow Timeline", "observe / wait / no_reply / prepare_reply / proactive_candidate / dispatched / blocked 的安全轨迹。", table(["Time", "Chat", "Kind", "Label", "Summary", "Payload"], timelineRows)));
  content().insertAdjacentHTML("beforeend", section("Proactive Intents", "Heartflow / Wakeup 主动候选经过 ProactiveDispatcher 后的结果。", table(["Time", "Chat", "Source", "Status", "Blocked", "Preview"], intentRows)));
  content().insertAdjacentHTML("beforeend", section("topic-digests", "HeartflowTopicDigest 写入 cognitive feedback 的记录与跳过原因。", table(["Time", "Chat", "Status", "Summary", "Tags", "Importance"], digestRows)));
  $$('[data-hidden-context]').forEach((button) => button.addEventListener("click", async () => {
    const result = await api.get(`/heartflow/chats/${segment(button.dataset.hiddenContext)}/hidden-context`);
    openModal("Heartflow Hidden Context", `<pre>${json(result.data || result)}</pre>`);
  }));
  $$('[data-heartflow-impulses]').forEach((button) => button.addEventListener("click", async () => {
    const chatId = button.dataset.heartflowImpulses;
    const result = await api.get(`/heartflow/chats/${segment(chatId)}/impulses?limit=20`);
    openModal(`Impulse Safety: ${escapeHtml(chatId)}`, `<pre>${json(result.items || [])}</pre>`);
  }));
  $$('[data-heartflow-timeline]').forEach((button) => button.addEventListener("click", async () => {
    const chatId = button.dataset.heartflowTimeline;
    const result = await api.get(`/heartflow/chats/${segment(chatId)}/timeline?limit=50`);
    openModal(`Heartflow Timeline: ${escapeHtml(chatId)}`, `<pre>${json(result.items || [])}</pre>`);
  }));
  $$('[data-clear-heartflow]').forEach((button) => button.addEventListener("click", async () => {
    if (!await confirmAction("清理这个 chat 的 Heartflow cooldown？")) return;
    await api.post(`/heartflow/chats/${segment(button.dataset.clearHeartflow)}/cooldowns/clear`);
    toast("Heartflow cooldown 已清理");
    renderDashboardHeartflow();
  }));
}

async function loadSchedulerChatLoop(chatId = null) {
  const targetChat = String(chatId ?? state.schedulerChatId ?? "").trim();
  if (!targetChat) {
    state.schedulerChatLoop = null;
    return;
  }
  state.schedulerChatId = targetChat;
  state.schedulerChatLoop = await api.get(`/cognition/scheduler/chats/${segment(targetChat)}`).catch(() => null);
}

function shouldPollScheduler() {
  return state.current === "dashboard" && state.dashboardTab === "cognition";
}

function stopSchedulerPolling() {
  if (state.schedulerPollTimer) {
    clearInterval(state.schedulerPollTimer);
    state.schedulerPollTimer = null;
  }
}

function startSchedulerPolling() {
  stopSchedulerPolling();
  if (!shouldPollScheduler()) return;
  state.schedulerPollTimer = setInterval(() => {
    if (!shouldPollScheduler()) {
      stopSchedulerPolling();
      return;
    }
    renderDashboardCognition().catch(() => {});
  }, SCHEDULER_POLL_INTERVAL_MS);
}

function renderSchedulerDiagnosticsSection() {
  const overview = schedulerOverview();
  const report = schedulerReport();
  const selected = asItems(report.selected).slice(0, 6);
  const chatData = state.schedulerChatLoop?.data || {};
  const emptyState = chatData.state_present === false
    ? `<div class="empty-state compact"><p>暂无 loop state。该 chat 尚未进入 scheduler 跟踪。</p></div>`
    : "";
  const quickButtons = selected.length
    ? `<div class="chip-row">${selected.map((chatId) => `<button class="ghost-button" data-scheduler-chat="${attr(chatId)}" type="button">${escapeHtml(chatId)}</button>`).join("")}</div>`
    : `<div class="empty-state compact"><p>当前没有 due selection 选中的 chat。</p></div>`;
  return section(
    "Scheduler Diagnostics",
    "Chat Loop Kernel 在 AstrBot 插件页内的调度摘要、批次配额与单 chat 诊断。",
    `
      <div class="grid">
        ${metric("Profile", state.schedulerStatus?.data?.scheduler_policy?.active_profile || "balanced")}
        ${metric("Poll Mode", overview.scheduler_poll_mode || "-")}
        ${metric("Poll Interval", overview.scheduler_poll_interval ?? 0)}
        ${metric("Due Chats", overview.due_chat_count ?? 0)}
        ${metric("Forced Promotions", overview.forced_promotion_count ?? 0)}
        ${metric("Batch Fill", formatPercent(overview.batch_fill_rate ?? 0))}
      </div>
      <div class="grid two">
        ${section("Batch / Backpressure", "", `
          <div class="chip-row">
            ${statusChip(`busy_backpressure: ${state.schedulerStatus?.data?.proactive?.busy_backpressure_active ? "on" : "off"}`, state.schedulerStatus?.data?.proactive?.busy_backpressure_active ? "warn" : "ok")}
            ${statusChip(`maintenance_backpressure: ${state.schedulerStatus?.data?.proactive?.maintenance_backpressure_active ? "on" : "off"}`, state.schedulerStatus?.data?.proactive?.maintenance_backpressure_active ? "warn" : "ok")}
          </div>
          <pre>${json(state.schedulerStatus?.data?.proactive?.scheduler_batch_plan || {})}</pre>
          <pre>${json(state.schedulerStatus?.data?.proactive?.quota_skip_counts || {})}</pre>
          <pre>${json(state.schedulerStatus?.data?.proactive?.poll_mode_transition || {})}</pre>
        `)}
        ${section("Chat Loop Drill-down", "", `
          <div class="form-grid single">
            <label>chat_id<input id="scheduler-chat-id" value="${attr(state.schedulerChatId || "")}" placeholder="chat_id"></label>
          </div>
          <div class="row-actions">
            <button class="primary-button" data-scheduler-load type="button">加载 scheduler chat</button>
          </div>
          ${quickButtons}
          ${emptyState}
          <div class="grid">
            ${metric("phase", chatData.phase || "-")}
            ${metric("next_tick_at", chatData.next_tick_at ?? 0)}
            ${metric("missed_due_passes", chatData.missed_due_passes ?? 0)}
            ${metric("forced_promotion_count", chatData.forced_promotion_count ?? 0)}
          </div>
          <pre>${json(chatData.scheduler_pending_signals || {})}</pre>
          <pre>${json(report)}</pre>
        `)}
      </div>
    `,
  );
}

async function renderDashboardCognition() {
  const [decisions, turns, schedulerStatus, schedulerDueSelection] = await Promise.all([
    api.get("/cognition/recent-decisions?limit=50").catch(() => ({ items: [] })),
    api.get("/cognition/recent-turns?limit=50").catch(() => ({ items: [] })),
    api.get("/cognition/scheduler/status").catch(() => null),
    api.get("/cognition/scheduler/due-selection").catch(() => null),
  ]);
  state.schedulerStatus = schedulerStatus;
  state.schedulerDueSelection = schedulerDueSelection;
  state.cache.turns = asItems(turns);
  const selectedSchedulerChats = asItems(schedulerReport().selected);
  if (!state.schedulerChatId && selectedSchedulerChats.length > 0) {
    state.schedulerChatId = selectedSchedulerChats[0];
  }
  if (state.schedulerChatId) {
    await loadSchedulerChatLoop(state.schedulerChatId);
  } else {
    state.schedulerChatLoop = null;
  }
  const decisionRows = asItems(decisions).map((item) => `
    <tr>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.social_intent || item.action || "-")}</td>
      <td>${escapeHtml(item.action_tier || item.memory_policy || "-")}</td>
      <td>${escapeHtml(item.stance || item.style_policy || "-")}</td>
      <td class="row-actions"><button class="ghost-button" data-chat-trace="${attr(item.chat_id || "")}" type="button">chat 详情</button></td>
    </tr>
  `);
  const turnRows = state.cache.turns.map((item, index) => {
    const perception = item.perception || {};
    const attention = item.attention || {};
    const cognitive = item.cognitive || {};
    const continuity = item.continuity || {};
    const tools = item.tools || {};
    const toolCount = Array.isArray(tools.filtered_tools) ? tools.filtered_tools.length : 0;
    return `
      <tr>
        <td>${formatTime(item.created_at)}</td>
        <td>${escapeHtml(item.chat_id || perception.chat_id || "-")}</td>
        <td>${escapeHtml(perception.sender_name || perception.sender_id || "-")}</td>
        <td>${escapeHtml(attention.judge_action || "-")}</td>
        <td>${escapeHtml(cognitive.social_intent || cognitive.action || "-")}</td>
        <td>${escapeHtml(cognitive.think_level ?? "-")}</td>
        <td>${escapeHtml(tools.final_tier || cognitive.action_tier || "-")}</td>
        <td>${toolCount}</td>
        <td>${continuity.has_heartflow_context ? statusChip("Heartflow", "ok") : statusChip("none")}</td>
        <td class="row-actions"><button class="ghost-button" data-turn-detail="${index}" type="button">详情</button></td>
      </tr>
    `;
  });
  dashboardShell(`
    ${renderSchedulerDiagnosticsSection()}
    ${section("主动决策池 Cognition", "最近 CognitiveLoop 决策，支持按 chat 查看决策/工具轨迹。", table(["Chat", "意图", "动作层级", "姿态", "操作"], decisionRows))}
    ${section("Turn Context", "每轮心智状态摘要：感知、注意力、主观决策、工具决策和连续性来源。", table(["时间", "Chat", "Sender", "Judge", "Intent", "Budget", "Tier", "工具数", "Heartflow", "操作"], turnRows))}
  `);
  $('[data-scheduler-load]')?.addEventListener("click", async () => {
    const input = $("#scheduler-chat-id");
    await loadSchedulerChatLoop(input ? input.value : state.schedulerChatId);
    renderDashboardCognition();
  });
  $$('[data-scheduler-chat]').forEach((button) => button.addEventListener("click", async () => {
    await loadSchedulerChatLoop(button.dataset.schedulerChat);
    renderDashboardCognition();
  }));
  $$('[data-chat-trace]').forEach((button) => button.addEventListener("click", () => openChatTrace(button.dataset.chatTrace)));
  $$('[data-turn-detail]').forEach((button) => button.addEventListener("click", () => openTurnTrace(Number(button.dataset.turnDetail))));
}

function renderThinkLevelSummary(cognitive) {
  const signals = Array.isArray(cognitive.think_signals) ? cognitive.think_signals.join(", ") : "";
  const skipSignals = Array.isArray(cognitive.cognitive_loop_skip_signals) ? cognitive.cognitive_loop_skip_signals.join(", ") : "";
  return `
    <div class="chip-row">
      <span class="chip">think_level: ${escapeHtml(cognitive.think_level ?? "-")}</span>
      <span class="chip">reason: ${escapeHtml(cognitive.think_reason || "-")}</span>
      <span class="chip">signals: ${escapeHtml(signals || "none")}</span>
      <span class="chip">loop_ran: ${escapeHtml(cognitive.cognitive_loop_ran ? "yes" : "no")}</span>
      <span class="chip">loop_skip: ${escapeHtml(cognitive.cognitive_loop_skipped_reason || "none")}</span>
      <span class="chip">loop_skip_signals: ${escapeHtml(skipSignals || "none")}</span>
      <span class="chip">readonly_allowed: ${escapeHtml(cognitive.readonly_tools_allowed ? "yes" : "no")}</span>
      <span class="chip">readonly_skip: ${escapeHtml(cognitive.readonly_tools_skip_reason || "none")}</span>
    </div>
    <pre>${json({
      think_level: cognitive.think_level,
      think_reason: cognitive.think_reason,
      think_signals: cognitive.think_signals || [],
      cognitive_loop_ran: Boolean(cognitive.cognitive_loop_ran),
      cognitive_loop_skipped_reason: cognitive.cognitive_loop_skipped_reason || "",
      cognitive_loop_skip_signals: cognitive.cognitive_loop_skip_signals || [],
      readonly_tools_allowed: Boolean(cognitive.readonly_tools_allowed),
      readonly_tools_skip_reason: cognitive.readonly_tools_skip_reason || "",
    })}</pre>
  `;
}

function renderToolRemovalSummary(tools) {
  const groups = [
    ["removed_by_energy", "energy"],
    ["removed_by_mood", "mood"],
    ["removed_by_hostility", "hostility"],
    ["removed_by_cooldown", "cooldown"],
    ["removed_by_caution", "caution"],
    ["removed_by_social_intent", "social_intent"],
  ];
  const chips = groups.map(([key, label]) => {
    const values = Array.isArray(tools[key]) ? tools[key] : [];
    const text = values.length ? values.join(", ") : "none";
    return `<span class="chip">${escapeHtml(label)}: ${escapeHtml(text)}</span>`;
  }).join(" ");
  return `<div class="chip-row">${chips}</div>`;
}

function renderFollowUpSummary(followUp) {
  const signals = Array.isArray(followUp.signals) ? followUp.signals.join(", ") : "";
  return `
    <div class="chip-row">
      <span class="chip">eligible: ${escapeHtml(followUp.eligible ? "yes" : "no")}</span>
      <span class="chip">skip: ${escapeHtml(followUp.skipped_reason || "none")}</span>
      <span class="chip">probability: ${escapeHtml(followUp.probability ?? 0)}</span>
      <span class="chip">llm_checked: ${escapeHtml(followUp.llm_checked ? "yes" : "no")}</span>
      <span class="chip">followed: ${escapeHtml(followUp.followed ? "yes" : "no")}</span>
      <span class="chip">reason: ${escapeHtml(followUp.reason || "none")}</span>
      <span class="chip">signals: ${escapeHtml(signals || "none")}</span>
    </div>
    <pre>${json(followUp || {})}</pre>
  `;
}

function renderSideInputTimings(sideInputs) {
  const timings = Array.isArray(sideInputs?.timings) ? sideInputs.timings : [];
  if (!timings.length) {
    return `<div class="empty-state compact"><p>No side input timing records for this turn.</p></div>`;
  }
  const totalMs = timings.reduce((sum, item) => sum + Number(item.elapsed_ms || 0), 0);
  const failed = timings.filter((item) => item.ok === false).length;
  const skipped = timings.filter((item) => item.skipped_reason).length;
  const chips = `
    <div class="chip-row">
      <span class="chip">items: ${escapeHtml(timings.length)}</span>
      <span class="chip">total: ${escapeHtml(totalMs.toFixed(2))}ms</span>
      <span class="chip">failed: ${escapeHtml(failed)}</span>
      <span class="chip">skipped: ${escapeHtml(skipped)}</span>
    </div>
  `;
  const rows = timings.map((item) => {
    const ok = item.ok === false ? statusChip("error", "danger") : statusChip(item.skipped_reason ? "skipped" : "ok", "ok");
    return `
      <tr>
        <td>${escapeHtml(item.name || "-")}</td>
        <td>${escapeHtml(item.elapsed_ms ?? 0)}ms</td>
        <td>${ok}</td>
        <td>${escapeHtml(item.skipped_reason || "-")}</td>
        <td>${escapeHtml(item.error || "-")}</td>
      </tr>
    `;
  });
  return `${chips}${table(["Input", "Elapsed", "Status", "Skipped", "Error"], rows)}<pre>${json(sideInputs || {})}</pre>`;
}

function openTurnTrace(index, source = state.cache.turns) {
  const item = source[index];
  if (!item) return toast("缺少 Turn Context 详情");
  openModal(`Turn Context: ${escapeHtml(item.chat_id || "-")}`, `
    <div class="grid two">
      ${section("感知 Perception", "", `<pre>${json(item.perception || {})}</pre>`)}
      ${section("注意力 Attention", "", `<pre>${json(item.attention || {})}</pre>`)}
      ${section("主观决策 Cognitive", "", `<pre>${json(item.cognitive || {})}</pre>`)}
      ${section("Think Level Budget", "0 quick / 1 normal / 2 deep / 3 tools or ReAct.", renderThinkLevelSummary(item.cognitive || {}))}
      ${section("记忆裁决 Memory", "", `<pre>${json(item.memory || {})}</pre>`)}
      ${section("Follow-up", "Follow-up budget, cooldown, and skipped reason.", renderFollowUpSummary(item.follow_up || {}))}
      ${section("Side Inputs Timings", "Budgeted prompt inputs, degradation status, and per-input latency.", renderSideInputTimings(item.side_inputs || {}))}
      ${section("工具决策 Tools", "", `${renderToolRemovalSummary(item.tools || {})}<pre>${json(item.tools || {})}</pre>`)}
    </div>
    ${section("连续性来源 Continuity", "", `<pre>${json(item.continuity || {})}</pre>`)}
  `);
}

async function renderDashboardTools() {
  const [status, policy, calls] = await Promise.all([
    api.get("/tools/status").catch(() => ({})),
    api.get("/tools/policy").catch(() => ({})),
    api.get("/tools/recent-calls?limit=50").catch(() => ({ items: [] })),
  ]);
  const rows = asItems(calls).map((item) => `
    <tr>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.tool_tier || item.final_tier || "-")}</td>
      <td>${item.tool_count ?? (Array.isArray(item.filtered_tools) ? item.filtered_tools.length : "-")}</td>
      <td><pre>${json(item)}</pre></td>
    </tr>
  `);
  dashboardShell(`
    <div class="grid two">
      ${section("工具层级", "chat / guarded / full tool tier", `<pre>${json(status.data || status)}</pre>`)}
      ${section("工具策略", "Tool policy rules", `<pre>${json(policy.data || policy)}</pre>`)}
    </div>
    ${section("工具链观测 Tools", "最近工具调用轨迹。", table(["Chat", "Tier", "工具数", "详情"], rows))}
  `);
}

async function openChatTrace(chatId) {
  if (!chatId) return toast("缺少 chat_id");
  const [decisions, tools, turns] = await Promise.all([
    api.get(`/cognition/chats/${segment(chatId)}/recent-decisions?limit=20`).catch(() => ({ items: [] })),
    api.get(`/tools/chats/${segment(chatId)}/recent-calls?limit=20`).catch(() => ({ items: [] })),
    api.get(`/cognition/chats/${segment(chatId)}/turns?limit=20`).catch(() => ({ items: [] })),
  ]);
  openModal(`chat 轨迹：${escapeHtml(chatId)}`, `
    <h4>主动决策池</h4><pre>${json(decisions.items || [])}</pre>
    <h4>工具链观测</h4><pre>${json(tools.items || [])}</pre>
    <h4>Turn Context</h4><pre>${json(turns.items || [])}</pre>
  `);
}

async function loadLearning() {
  showLoading("正在读取主动学习与任务...");
  const [proactive, intents, dream, diary, wakeup, learning, feedback, sources, chats, cooldowns] = await Promise.all([
    api.get("/proactive/status").catch(() => ({})),
    api.get("/proactive/intents?limit=50").catch(() => ({ items: [] })),
    api.get("/proactive/dream/status").catch(() => ({})),
    api.get("/proactive/diary/status").catch(() => ({})),
    api.get("/proactive/wakeup/status").catch(() => ({})),
    api.get("/learning/status").catch(() => ({})),
    api.get("/memory-feedback?limit=50").catch(() => ({ items: [] })),
    api.get("/memory-feedback/sources").catch(() => ({ items: [] })),
    api.get("/chats/active?max_age_seconds=1800").catch(() => ({ items: [] })),
    api.get("/learning/cooldowns").catch(() => ({})),
  ]);
  const cards = [
    ["造梦空间", "Dream Agent", dream.data || dream, "执行造梦序列", "run-dream"],
    ["日记写作", "Diary Agent", diary.data || diary, "撰写今日日记", "run-diary"],
    ["沉淀审核", "Reflect / Learning", learning.data || learning, "基于会话触发", ""],
  ].map(([title, subtitle, data, action, key]) => `
    <article class="feature-card">
      <div><h3>${title}</h3><p>${subtitle}</p><pre>${json(data)}</pre></div>
      ${key ? `<button class="primary-button" data-${key} type="button">${action}</button>` : `<span class="chip ok">${action}</span>`}
    </article>
  `).join("");
  const sourceChips = asItems(sources).map((item) => statusChip(`${item.source}: ${item.count}`, "ok")).join(" ");
  const feedbackRows = asItems(feedback).map((item) => `
    <tr>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.source || "-")}</td>
      <td>${escapeHtml(item.summary || "-")}</td>
      <td>${escapeHtml(item.guidance || "-")}</td>
      <td><button class="danger-button" data-disable-feedback="${attr(item.id || "")}" type="button">禁用反馈</button></td>
    </tr>
  `);
  const intentRows = asItems(intents).map((item) => `
    <tr>
      <td>${formatTime(item.timestamp || item.created_at)}</td>
      <td>${escapeHtml(item.chat_id || "-")}</td>
      <td>${escapeHtml(item.source || "-")}</td>
      <td>${item.reply_sent ? statusChip("sent", "ok") : statusChip(item.blocked_reason || item.status || "queued", item.blocked_reason ? "warn" : "")}</td>
      <td>${escapeHtml(item.blocked_reason || "-")}</td>
      <td>${escapeHtml(item.reason || item.guidance || "-")}</td>
    </tr>
  `);
  const chatRows = asItems(chats).map((chatId) => `
    <tr>
      <td>${escapeHtml(chatId)}</td>
      <td class="row-actions">
        <button class="ghost-button" data-chat-runtime="${attr(chatId)}" type="button">运行态</button>
        <button class="primary-button" data-run-reflect="${attr(chatId)}" type="button">立即反思</button>
        <button class="danger-button" data-clear-runtime="${attr(chatId)}" type="button">清理状态</button>
      </td>
    </tr>
  `);
  content().innerHTML = `
    ${pageHeader("主动学习与任务", "监控 AI 的独立思考周期、夜间造梦及反思过滤池。")}
    <div class="feature-grid">${cards}</div>
    <div class="grid two">
      ${section("主动组件调度 Proactive", "Proactive / Wakeup 状态。", `<pre>${json({ proactive: proactive.data || proactive, wakeup: wakeup.data || wakeup })}</pre>`)}
      ${section("表达冷却", "Expression selector cooldowns", `<pre>${json(cooldowns.data || cooldowns)}</pre>`)}
    </div>
    ${section("主动意图轨迹", "Wakeup / Heartflow 候选经安全裁决后进入主链路的结果。", table(["时间", "Chat", "来源", "状态", "阻断", "预览"], intentRows))}
    ${section("记忆反馈", "Memory feedback sources 与反馈列表。", `<div class="chip-row">${sourceChips || "<span class='muted'>暂无来源</span>"}</div>${table(["Chat", "来源", "摘要", "指引", "操作"], feedbackRows)}`)}
    ${section("活跃会话", "可触发独立反思或清理 runtime。", table(["Chat", "操作"], chatRows))}
  `;
  bindLearningActions();
}

function bindLearningActions() {
  $('[data-run-dream]')?.addEventListener("click", async () => {
    await api.post("/proactive/dream/run-once");
    toast("Dream 已调度");
  });
  $('[data-run-diary]')?.addEventListener("click", async () => {
    await api.post("/proactive/diary/run-once");
    toast("Diary 已调度");
  });
  $$('[data-disable-feedback]').forEach((button) => button.addEventListener("click", async () => {
    if (!button.dataset.disableFeedback) return;
    if (!await confirmAction("禁用这条记忆反馈？")) return;
    await api.post(`/memory-feedback/${segment(button.dataset.disableFeedback)}/disable`);
    toast("反馈已禁用");
    loadLearning();
  }));
  $$('[data-chat-runtime]').forEach((button) => button.addEventListener("click", async () => {
    const result = await api.get(`/chats/${segment(button.dataset.chatRuntime)}/runtime`);
    openModal("Chat Runtime", `<pre>${json(result.data || result)}</pre>`);
  }));
  $$('[data-run-reflect]').forEach((button) => button.addEventListener("click", async () => {
    await api.post("/learning/reflect/run-once", { chat_id: button.dataset.runReflect });
    toast("反思任务已触发");
  }));
  $$('[data-clear-runtime]').forEach((button) => button.addEventListener("click", async () => {
    if (!await confirmAction("清理该 chat 的运行态？")) return;
    await api.post(`/chats/${segment(button.dataset.clearRuntime)}/runtime/clear`);
    toast("运行态已清理");
    loadLearning();
  }));
}

async function loadReviews() {
  showLoading("正在读取表达审核...");
  const [pending, all] = await Promise.all([
    api.get("/reviews/pending").catch(() => ({ items: [] })),
    api.get("/reviews?page_size=50").catch(() => ({ items: [] })),
  ]);
  const pendingItems = asItems(pending);
  const allItems = asItems(all);
  const rows = (state.reviewTab === "pending" ? pendingItems : allItems).map((item) => `
    <tr>
      <td>${escapeHtml(item.id || item.review_id || "-")}</td>
      <td>${escapeHtml(item.text || item.pattern || item.content || "-")}</td>
      <td>${escapeHtml(item.status || "pending")}</td>
      <td>${escapeHtml(item.weight ?? "-")}</td>
      <td class="row-actions">
        <button class="primary-button" data-approve-review="${attr(item.id || item.review_id || "")}" type="button">批准</button>
        <button class="danger-button" data-reject-review="${attr(item.id || item.review_id || "")}" type="button">驳回</button>
      </td>
    </tr>
  `);
  content().innerHTML = `
    ${pageHeader("表达语料审核 Reviews", "管理 AI 提取的话术习惯，决定是否加入核心表达网络。")}
    ${subTabs(state.reviewTab, [
      { id: "pending", label: "待审队列" },
      { id: "all", label: "全量库查阅" },
    ], "review-tab")}
    ${section(state.reviewTab === "pending" ? "待审队列" : "全量库查阅", "批准、驳回或查看表达语料。", table(["ID", "内容", "状态", "权重", "操作"], rows))}
  `;
  $$('[data-review-tab]').forEach((button) => button.addEventListener("click", () => {
    state.reviewTab = button.dataset.reviewTab;
    loadReviews();
  }));
  bindReviewActions();
}

function bindReviewActions() {
  $$('[data-approve-review]').forEach((button) => button.addEventListener("click", async () => {
    if (!button.dataset.approveReview) return;
    await api.post(`/reviews/${segment(button.dataset.approveReview)}/submit`, { action: "approve" });
    toast("已批准");
    loadReviews();
  }));
  $$('[data-reject-review]').forEach((button) => button.addEventListener("click", async () => {
    if (!button.dataset.rejectReview) return;
    await api.post(`/reviews/${segment(button.dataset.rejectReview)}/submit`, { action: "reject" });
    toast("已驳回");
    loadReviews();
  }));
}

async function loadMemories() {
  showLoading("正在读取记忆网络...");
  const [events, reflections, nodes, jargon] = await Promise.all([
    api.get("/memories/events").catch(() => ({ items: [] })),
    api.get(`/memories/reflections?month=${segment(state.cache.memories.month)}`).catch(() => ({ items: [] })),
    api.get("/memories/nodes").catch(() => ({ items: [] })),
    api.get("/memories/jargon").catch(() => ({ items: [] })),
  ]);
  state.cache.memories.events = asItems(events);
  state.cache.memories.reflections = asItems(reflections);
  state.cache.memories.nodes = asItems(nodes);
  state.cache.memories.jargon = asItems(jargon);
  const tabItems = {
    events: state.cache.memories.events,
    reflections: state.cache.memories.reflections,
    nodes: state.cache.memories.nodes,
    jargon: state.cache.memories.jargon,
  }[state.memoryTab] || [];
  const rows = tabItems.map((item) => `
    <tr>
      <td>${escapeHtml(item.id || item.date || item.term || "-")}</td>
      <td><pre>${json(item)}</pre></td>
      <td class="row-actions"><button class="danger-button" data-memory-delete="${attr(item.id || item.date || "")}" type="button">删除</button></td>
    </tr>
  `);
  content().innerHTML = `
    ${pageHeader("四维记忆网络 Memories", "管理 AI 的记忆碎片、反思日志、实体图谱与黑话字典。")}
    ${subTabs(state.memoryTab, [
      { id: "events", label: "记忆碎片 Events" },
      { id: "reflections", label: "每日反思 Reflections" },
      { id: "nodes", label: "实体图谱 Nodes" },
      { id: "jargon", label: "黑话字典 Jargon" },
    ], "memory-tab")}
    ${section("记忆数据", "当前子页签数据。", table(["ID", "详情", "操作"], rows))}
  `;
  $$('[data-memory-tab]').forEach((button) => button.addEventListener("click", () => {
    state.memoryTab = button.dataset.memoryTab;
    loadMemories();
  }));
  $$('[data-memory-delete]').forEach((button) => button.addEventListener("click", async () => {
    const id = button.dataset.memoryDelete;
    if (!id || !await confirmAction("删除这条记忆记录？", "danger")) return;
    if (state.memoryTab === "events") await api.post(`/memories/events/${segment(id)}/delete`);
    if (state.memoryTab === "reflections") await api.post(`/memories/reflections/${segment(id)}/delete`);
    if (state.memoryTab === "nodes") await api.post(`/memories/nodes/${segment(id)}/delete`);
    if (state.memoryTab === "jargon") await api.post(`/memories/jargon/${segment(id)}/delete`);
    toast("记忆记录已删除");
    loadMemories();
  }));
}

async function loadUsers() {
  showLoading("正在读取用户画像...");
  const users = await api.get("/users").catch(() => ({ items: [] }));
  state.cache.users = asItems(users);
  if (!state.activeUserId && state.cache.users[0]) state.activeUserId = state.cache.users[0].user_id || state.cache.users[0].id || "";
  renderUsers();
}

function renderUsers() {
  const users = state.cache.users || [];
  const active = users.find((item) => String(item.user_id || item.id || "") === String(state.activeUserId)) || users[0] || null;
  const list = users.map((user) => `
    <button class="list-item ${String(user.user_id || user.id || "") === String(state.activeUserId) ? "active" : ""}" data-select-user="${attr(user.user_id || user.id || "")}" type="button">
      <span class="avatar">${escapeHtml(String(user.nickname || user.name || user.user_id || "?").slice(0, 1).toUpperCase())}</span>
      <span><strong>${escapeHtml(user.nickname || user.name || "Unknown")}</strong><small>${escapeHtml(user.identity || "未定义身份")}</small>${progressBar(user.social_score || 0)}</span>
    </button>
  `).join("");
  content().innerHTML = `
    ${pageHeader("社交画像枢纽 Users", "查看和调整用户画像、关系切片与长期记忆点。")}
    <div class="dual-pane">
      <aside class="side-list">${list || "<p class='muted'>暂无用户</p>"}</aside>
      <section>${active ? renderUserDetail(active) : "<p class='muted'>请选择用户</p>"}</section>
    </div>
  `;
  $$('[data-select-user]').forEach((button) => button.addEventListener("click", () => {
    state.activeUserId = button.dataset.selectUser;
    renderUsers();
  }));
  bindUserActions(active);
}

function renderUserDetail(user) {
  return `
    ${section("基础画像", "基础字段与原始画像。", `
      <div class="grid two">
        ${formField("nickname", "系统称呼", user.nickname || "")}
        ${formField("identity", "底层身份", user.identity || "")}
        ${formField("social_score", "羁绊权重", user.social_score ?? "")}
        ${formField("tags", "全局标签", Array.isArray(user.tags) ? user.tags.join(", ") : (user.tags || ""))}
      </div>
      <label>画像分析概览<textarea data-user-field="persona_analysis">${escapeHtml(user.persona_analysis || "")}</textarea></label>
      <div class="row-actions">
        <button class="primary-button" data-save-user type="button">保存基础画像</button>
        <button class="danger-button" data-delete-user type="button">删除用户画像</button>
      </div>
    `)}
    ${SLICE_FIELDS.map(([field, label]) => renderSliceSection(user, field, label)).join("")}
  `;
}

function renderSliceSection(user, field, label) {
  const values = Array.isArray(user[field]) ? user[field] : [];
  return section(label, field, `
    <div class="chip-row">${values.map((value, index) => `<span class="chip">${escapeHtml(value)} <button data-delete-slice="${field}:${index}" type="button">×</button></span>`).join(" ") || "<span class='muted'>暂无数据</span>"}</div>
    <div class="inline-form"><input data-slice-input="${field}" placeholder="新增切片内容"><button class="ghost-button" data-add-slice="${field}" type="button">添加</button></div>
  `);
}

function bindUserActions(active) {
  if (!active) return;
  $('[data-save-user]')?.addEventListener("click", async () => {
    const body = {};
    $$('[data-user-field]').forEach((input) => { body[input.dataset.userField] = input.value; });
    await api.post(`/users/${segment(active.user_id || active.id)}`, body);
    toast("用户画像已保存");
    loadUsers();
  });
  $('[data-delete-user]')?.addEventListener("click", async () => {
    if (!await confirmAction("删除这个用户画像？该操作不可恢复。", "danger")) return;
    await api.post(`/users/${segment(active.user_id || active.id)}/delete`);
    state.activeUserId = "";
    toast("用户画像已删除");
    loadUsers();
  });
  $$('[data-add-slice]').forEach((button) => button.addEventListener("click", async () => {
    const field = button.dataset.addSlice;
    const input = $(`[data-slice-input="${field}"]`);
    if (!input?.value?.trim()) return;
    await api.post(`/users/${segment(active.user_id || active.id)}/slices`, { type: field, content: input.value.trim() });
    toast("切片已添加");
    loadUsers();
  }));
  $$('[data-delete-slice]').forEach((button) => button.addEventListener("click", async () => {
    const [field, index] = button.dataset.deleteSlice.split(":");
    await api.post(`/users/${segment(active.user_id || active.id)}/slices/${segment(index)}/delete`, { type: field });
    toast("切片已删除");
    loadUsers();
  }));
}

async function loadPersonaSlices() {
  showLoading("正在读取角色切片诊断...");
  state.cache.personaSlicesError = null;
  try {
    const result = await api.get("/persona/slices");
    state.cache.personaSlices = result.data || result;
  } catch (error) {
    state.cache.personaSlices = {};
    state.cache.personaSlicesError = error.message || String(error);
    toast("角色切片读取失败");
  }
  renderPersonaSlices();
}

function renderReadonlyText(value, empty = "暂无内容") {
  const text = String(value || "").trim();
  return text ? `<p class="readonly-text">${escapeHtml(text)}</p>` : `<div class="empty-state compact"><p>${escapeHtml(empty)}</p></div>`;
}

function renderShardCards(persona = {}) {
  const shards = persona.shards || {};
  const labels = persona.shard_labels || {};
  const keys = persona.shard_order || Object.keys(labels).concat(Object.keys(shards)).filter((value, index, array) => array.indexOf(value) === index);
  return `
    <div class="persona-shard-grid">
      ${keys.map((key) => {
        const value = shards[key] || "";
        const ready = String(value || "").trim().length > 0;
        return `
          <article class="persona-shard-card ${ready ? "" : "missing"}">
            <div class="field-head">
              <strong>${escapeHtml(labels[key] || key)}</strong>
              <code>${escapeHtml(key)}</code>
            </div>
            ${renderReadonlyText(value, "该切片尚未生成")}
          </article>
        `;
      }).join("") || `<div class="empty-state"><p>暂无切片定义</p></div>`}
    </div>
  `;
}

function renderPersonaSlices() {
  const error = state.cache.personaSlicesError;
  if (error) {
    content().innerHTML = `
      ${pageHeader("角色切片诊断 Persona Slices", "只读查看 AstrMai 从 AstrBot 人格中提炼出的角色理解结果。", `<button class="primary-button" data-retry-persona-slices type="button">重新读取</button>`)}
      ${section("读取失败", "这不是空数据，而是角色切片 API 没有成功返回。", `
        <div class="empty-state">
          <h3>无法读取 /persona/slices</h3>
          <p>${escapeHtml(error)}</p>
          <p>请确认插件已经重载、Plugin Page 后端路由已注册，或者查看 AstrBot 后台日志里的具体异常。</p>
        </div>
      `)}
      ${section("管理边界", "配置与原始人格不在本页管理，避免 AstrMai 插件页形成第二套入口。", `
        <div class="chip-row">
          ${statusChip("配置：AstrBot 插件配置页", "muted")}
          ${statusChip("原始人格：AstrBot 人格管理", "muted")}
          ${statusChip("本页：只读诊断", "ok")}
        </div>
      `)}
    `;
    $('[data-retry-persona-slices]')?.addEventListener("click", loadPersonaSlices);
    return;
  }
  const persona = state.cache.personaSlices || {};
  const summary = persona.summary || "";
  const firstPerson = persona.first_person_rewrite || "";
  const style = persona.style || "";
  const ready = Boolean(persona.is_full_ready);
  const pending = Boolean(persona.pending_task);
  content().innerHTML = `
    ${pageHeader("角色切片诊断 Persona Slices", "只读查看 AstrMai 从 AstrBot 人格中提炼出的摘要、第一人称自觉与 8 类角色切片。", `<button class="ghost-button" data-persona-slices-json type="button">诊断 JSON</button>`)}
    ${section("管理边界", "插件页只展示 AstrMai 的角色理解结果；基础配置与原始人格请继续使用 AstrBot 原生页面。", `
      <div class="chip-row">
        ${statusChip("配置：AstrBot 插件配置页", "muted")}
        ${statusChip("原始人格：AstrBot 人格管理", "muted")}
        ${statusChip("本页：只读诊断", "ok")}
      </div>
    `)}
    <div class="grid">
      ${metric("Persona ID", persona.persona_id || "-", "来自 _conf_schema.json 的 persona.persona_id")}
      ${metric("Cache Key", persona.cache_key || "-", "PersonaSummarizer 当前缓存键")}
      ${metric("切片状态", ready ? "ready" : (pending ? "building" : "partial"), ready ? "8 类切片已生成" : "可能仍在后台构建")}
      ${metric("缓存时间", formatTime(persona.timestamp), "persona_cache 时间戳")}
    </div>
    <div class="grid two">
      ${section("核心摘要 Summary", "用于压缩长人格，降低即时回复 token 成本。", renderReadonlyText(summary))}
      ${section("第一人称自觉 First Person Rewrite", "ContextEngine 优先使用这段短自述来稳定扮演视角。", renderReadonlyText(firstPerson))}
    </div>
    ${section("风格指南 Style", "由 PersonaSummarizer 提炼，不直接等同原始人格 prompt。", renderReadonlyText(style))}
    ${section("八维角色切片", "CognitiveLoop 可按 retrieve_keys 临时加载这些切片；缺失时不会阻断聊天。", renderShardCards(persona))}
    ${section("自我原典与缓存", "self_lore 是长期记忆中的角色原典索引，本页不展示原文，只展示安全摘要。", `
      <div class="chip-row">
        ${statusChip(`self_lore: ${persona.self_lore?.available ? "available" : "unavailable"}`, persona.self_lore?.available ? "ok" : "muted")}
        ${statusChip(`raw_length: ${persona.raw_length || 0}`, "muted")}
        ${statusChip(`cache_keys: ${(persona.cache_keys || []).length}`, "muted")}
      </div>
    `)}
  `;
  $('[data-persona-slices-json]')?.addEventListener("click", () => openModal("Persona Slices Diagnostic", `<pre>${json(state.cache.personaSlices || {})}</pre>`));
}

async function loadCurrent() {
  const loaders = {
    dashboard: loadDashboard,
    learning: loadLearning,
    reviews: loadReviews,
    memories: loadMemories,
    users: loadUsers,
    personaSlices: loadPersonaSlices,
  };
  if (state.current !== "dashboard") {
    stopSchedulerPolling();
  }
  try {
    await (loaders[state.current] || loadDashboard)();
  } catch (error) {
    content().innerHTML = `<section class="empty-state"><h2>加载失败</h2><p>${escapeHtml(error.message || error)}</p></section>`;
  }
}

async function init() {
  setBridgeStatus("Bridge 初始化中", "muted");
  renderTabs();
  $("#refresh-button").addEventListener("click", loadCurrent);
  window.addEventListener("hashchange", () => {
    const next = normalizeTabId(location.hash.replace("#", "") || "dashboard");
    if (next !== state.current) {
      state.current = next;
      if (location.hash.replace("#", "") !== next) location.hash = next;
      renderTabs();
      loadCurrent();
    }
  });

  try {
    state.bridge = await waitForBridge();
    await readyBridge(state.bridge);
  } catch (error) {
    setBridgeStatus("Bridge 连接失败", "danger");
    content().innerHTML = `<section class="empty-state"><h2>没有检测到 AstrBotPluginPage</h2><p>请从 AstrBot WebUI 的插件详情页打开本页面，不要直接打开 HTML 文件。</p><p>${escapeHtml(error.message || error)}</p></section>`;
    return;
  }

  setBridgeStatus("Bridge 已连接", "ok");
  await loadCurrent();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
