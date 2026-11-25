"use strict";

const state = {
  runs: [],
  currentRunId: "",
  events: [],
  eventSource: null,
  filter: "all",
  knobs: [],
  profile: null,
  knownSequences: new Set(),
  refreshTimer: null,
};

const $ = (id) => document.getElementById(id);

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function shortId(value, length = 12) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function formatTime(timestamp) {
  if (!timestamp) return "—";
  return new Date(timestamp * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function formatDate(timestamp) {
  if (!timestamp) return "—";
  return new Date(timestamp * 1000).toLocaleString([], {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function formatNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number.toLocaleString() : "0";
}

function formatDuration(ms) {
  const value = Number(ms || 0);
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60000) return `${(value / 1000).toFixed(1)}s`;
  return `${(value / 60000).toFixed(1)}m`;
}

function showError(message) {
  const banner = $("error-banner");
  if (!message) {
    banner.classList.add("hidden");
    banner.textContent = "";
    return;
  }
  banner.textContent = message;
  banner.classList.remove("hidden");
}

async function fetchJSON(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return body;
}

async function checkServer() {
  try {
    const status = await fetchJSON("/api/v1/status");
    $("server-status").textContent = `local · v${status.schema_version}`;
    $("server-dot").classList.add("online");
  } catch (error) {
    $("server-status").textContent = "disconnected";
    $("server-dot").classList.remove("online");
    showError(`Web Gateway 连接失败：${error.message}`);
  }
}

async function loadRuns() {
  try {
    const payload = await fetchJSON("/api/v1/runs");
    state.runs = payload.runs || [];
    renderRuns();
    if (!state.currentRunId && state.runs.length) {
      await selectRun(state.runs[0].run_id);
    }
  } catch (error) {
    showError(`Run 列表加载失败：${error.message}`);
  }
}

function renderRuns() {
  const root = $("run-list");
  root.replaceChildren();
  if (!state.runs.length) {
    root.append(make("div", "empty", "暂无 Agent run\n启动 krelcode 后刷新"));
    return;
  }
  for (const run of state.runs) {
    const button = make("button", "run-item");
    if (run.run_id === state.currentRunId) button.classList.add("active");
    button.type = "button";
    button.title = run.run_id;
    button.append(
      make("span", "run-model", `${run.provider || "provider"} · ${run.model || "model"}`),
    );
    const meta = make("span", "run-meta");
    meta.append(
      make("span", "run-mode", String(run.run_mode || "run").toUpperCase()),
      make("span", "", formatDate(run.started_at)),
    );
    button.append(meta);
    button.addEventListener("click", () => selectRun(run.run_id));
    root.append(button);
  }
}

async function selectRun(runId) {
  if (!runId) return;
  state.currentRunId = runId;
  state.events = [];
  state.knownSequences.clear();
  state.filter = "all";
  document.querySelectorAll(".filter").forEach((button) => {
    button.classList.toggle("active", button.dataset.kind === "all");
  });
  renderRuns();
  $("timeline").replaceChildren(make("div", "empty", "Loading events…"));
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  try {
    const [runPayload, eventPayload] = await Promise.all([
      fetchJSON(`/api/v1/runs/${encodeURIComponent(runId)}`),
      fetchJSON(`/api/v1/runs/${encodeURIComponent(runId)}/events?after=0&limit=50000`),
    ]);
    renderRunHeader(runPayload);
    for (const event of eventPayload.events || []) appendEvent(event, false);
    renderTimeline();
    renderContext();
    connectStream(runId);
  } catch (error) {
    showError(`Run 加载失败：${error.message}`);
  }
}

function renderRunHeader(payload) {
  const manifest = payload.manifest || {};
  const metrics = payload.metrics || {};
  $("run-title").textContent = `${manifest.provider || "provider"} / ${manifest.model || "model"}`;
  $("run-subtitle").textContent = [
    `run ${shortId(manifest.run_id, 18)}`,
    manifest.session_id ? `session ${shortId(manifest.session_id, 14)}` : "",
    manifest.experiment_name ? `experiment ${manifest.experiment_name}` : "",
  ].filter(Boolean).join(" · ");
  const badge = $("run-badge");
  badge.textContent = String(metrics.status || "running").toUpperCase();
  badge.className = `run-badge ${metrics.status || "running"}`;
  $("m-status").textContent = metrics.status || "—";
  $("m-tokens").textContent = formatNumber(metrics.total_tokens);
  $("m-tools").textContent = formatNumber(metrics.tool_calls);
  $("m-duration").textContent = formatDuration(metrics.duration_ms);
  $("m-events").textContent = formatNumber(metrics.event_count);
  $("m-health").textContent = `${formatNumber(metrics.sequence_gaps)} / ${formatNumber(metrics.errors)}`;
}

function connectStream(runId) {
  const after = state.events.length
    ? Math.max(...state.events.map((event) => Number(event.sequence || 0)))
    : 0;
  const source = new EventSource(
    `/api/v1/runs/${encodeURIComponent(runId)}/stream?after=${after}`,
  );
  state.eventSource = source;
  source.addEventListener("agent-event", (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.run_id !== state.currentRunId) return;
      appendEvent(event, true);
    } catch (error) {
      console.warn("bad SSE event", error);
    }
  });
  source.addEventListener("error", () => {
    if (state.eventSource === source) {
      $("server-status").textContent = "reconnecting…";
      $("server-dot").classList.remove("online");
    }
  });
  source.addEventListener("open", () => {
    $("server-status").textContent = "live · SSE";
    $("server-dot").classList.add("online");
  });
}

function appendEvent(event, live) {
  const sequence = Number(event.sequence || 0);
  if (state.knownSequences.has(sequence)) return;
  state.knownSequences.add(sequence);
  state.events.push(event);
  state.events.sort((left, right) => left.sequence - right.sequence);
  if (live) {
    renderTimeline();
    renderContext();
    scheduleMetricsRefresh();
  }
}

function scheduleMetricsRefresh() {
  if (state.refreshTimer) return;
  state.refreshTimer = window.setTimeout(async () => {
    state.refreshTimer = null;
    if (!state.currentRunId) return;
    try {
      const payload = await fetchJSON(`/api/v1/runs/${encodeURIComponent(state.currentRunId)}`);
      renderRunHeader(payload);
    } catch (error) {
      console.warn(error);
    }
  }, 900);
}

function eventSummary(event) {
  const payload = event.payload || {};
  if (typeof payload.input === "string") return payload.input;
  if (typeof payload.text === "string" && payload.text) return payload.text;
  if (typeof payload.message === "string") return payload.message;
  if (typeof payload.reason === "string" && payload.reason) return payload.reason;
  if (payload.tool_call && payload.tool_call.name) {
    return `${payload.tool_call.name} ${JSON.stringify(payload.tool_call.arguments || {})}`;
  }
  if (payload.message && payload.message.content) return payload.message.content;
  if (payload.snapshot) {
    return `${formatNumber(payload.snapshot.estimated_input_tokens)} tokens · ${payload.snapshot.context_engine || "context"}`;
  }
  if (payload.error) return payload.error;
  const compact = JSON.stringify(payload);
  return compact === "{}" ? "" : compact;
}

function renderTimeline() {
  const root = $("timeline");
  root.replaceChildren();
  const filtered = state.filter === "all"
    ? state.events
    : state.events.filter((event) => event.kind === state.filter);
  if (!filtered.length) {
    root.append(make("div", "empty", state.events.length ? "当前筛选无事件" : "等待 Agent 事件…"));
    return;
  }
  for (const event of filtered) {
    const row = make("div", "event-row");
    row.append(make("div", "event-time", formatTime(event.timestamp)));
    row.append(make("div", `kind-pill ${event.kind}`, event.kind));
    const main = make("div", "event-main");
    main.append(make("div", "event-name", String(event.name || "event").replaceAll("_", " ")));
    const summary = eventSummary(event);
    if (summary) main.append(make("div", "event-summary", summary));
    const details = document.createElement("details");
    const toggle = document.createElement("summary");
    toggle.textContent = "查看脱敏 payload";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(event, null, 2);
    details.append(toggle, pre);
    main.append(details);
    row.append(main);
    row.append(make("div", "span-id", event.span_id || ""));
    root.append(row);
  }
  if ($("auto-scroll").checked) root.scrollTop = root.scrollHeight;
}

function renderContext() {
  const contextEvents = state.events.filter(
    (event) => event.name === "context_assembled",
  );
  const latest = contextEvents[contextEvents.length - 1];
  if (!latest || !latest.payload || !latest.payload.snapshot) {
    return;
  }
  const snapshot = latest.payload.snapshot;
  const summary = $("context-summary");
  summary.className = "kv";
  summary.replaceChildren();
  const entries = [
    ["Engine", snapshot.context_engine || "—"],
    ["Estimated input", `${formatNumber(snapshot.estimated_input_tokens)} tok`],
    ["Budget", snapshot.budget_tokens ? `${formatNumber(snapshot.budget_tokens)} tok` : "unbounded"],
    ["Stable / Dynamic", `${formatNumber(snapshot.stable_tokens)} / ${formatNumber(snapshot.volatile_tokens)}`],
    ["Tool schemas", `${formatNumber(snapshot.tool_schema_tokens)} tok`],
    ["Memory", `${formatNumber(snapshot.memory_tokens)} tok`],
    ["Fragments / sources", `${formatNumber(snapshot.fragment_count)} / ${formatNumber(snapshot.source_count)}`],
    ["Dropped", formatNumber((snapshot.dropped_items || []).length)],
  ];
  for (const [key, value] of entries) {
    summary.append(make("span", "key", key), make("span", "value", value));
  }

  const cache = $("cache-summary");
  cache.className = "kv";
  cache.replaceChildren();
  const cacheEntries = [
    ["Cache mode", snapshot.cache_mode || "estimated"],
    ["Stable hash", shortId(snapshot.stable_hash || "—", 18)],
    ["Dynamic hash", shortId(snapshot.dynamic_hash || "—", 18)],
    ["Full hash", shortId(snapshot.full_hash || "—", 18)],
    ["Breakpoints", `${formatNumber(snapshot.cache_breakpoint_count)} / max ${formatNumber(snapshot.cache_max_breakpoints)}`],
    ["Fallback", snapshot.cache_fallback ? "yes" : "no"],
    ["Stable blocks", formatNumber(snapshot.stable_system_block_count)],
    ["Evidence blocks", formatNumber(snapshot.evidence_block_count)],
  ];
  for (const [key, value] of cacheEntries) {
    cache.append(make("span", "key", key), make("span", "value", value));
  }

  const sources = snapshot.source_tokens || {};
  renderSourceBars(sources);
  renderContextEvents();
}

function renderSourceBars(sources) {
  const root = $("context-sources");
  root.replaceChildren();
  const entries = Object.entries(sources)
    .sort((left, right) => Number(right[1]) - Number(left[1]));
  if (!entries.length) {
    root.className = "source-bars empty";
    root.textContent = "暂无来源分布";
    return;
  }
  root.className = "source-bars";
  const max = Math.max(...entries.map(([, value]) => Number(value || 0)), 1);
  for (const [name, value] of entries) {
    const row = make("div", "source-row");
    row.append(make("span", "", name));
    const track = make("div", "source-track");
    const fill = make("div", "source-fill");
    fill.style.width = `${Math.max(1, (Number(value) / max) * 100)}%`;
    track.append(fill);
    row.append(track, make("span", "value", `${formatNumber(value)}`));
    root.append(row);
  }
}

function renderContextEvents() {
  const root = $("context-events");
  root.replaceChildren();
  const relevant = state.events.filter((event) => [
    "context_compressed", "context_microcompacted", "memory_loaded",
    "memory_candidate_observed", "memory_written", "compact_checkpoint_created",
  ].includes(event.name)).slice(-12);
  if (!relevant.length) {
    root.className = "mini-events empty";
    root.textContent = "暂无 Context / Memory 事件";
    return;
  }
  root.className = "mini-events";
  for (const event of relevant) {
    const row = make("div", "mini-event");
    row.append(
      make("span", "", String(event.name).replaceAll("_", " ")),
      make("span", "muted", eventSummary(event)),
    );
    root.append(row);
  }
}

async function loadExperiments() {
  try {
    const [knobsPayload, profilePayload] = await Promise.all([
      fetchJSON("/api/v1/experiments/knobs"),
      fetchJSON("/api/v1/experiments/active"),
    ]);
    state.knobs = knobsPayload.knobs || [];
    state.profile = profilePayload.profile;
    renderExperiments();
    $("profile-meta").textContent = `文件：${profilePayload.path} · profile ${shortId(profilePayload.profile.profile_hash, 16)} · 只影响下一轮`;
    renderWarnings(profilePayload.warnings || []);
  } catch (error) {
    showError(`实验配置加载失败：${error.message}`);
  }
}

function renderExperiments() {
  const root = $("knobs");
  root.replaceChildren();
  const knobs = state.profile.knobs || {};
  for (const knob of state.knobs) {
    const card = make("article", "knob");
    const head = make("div", "knob-head");
    head.append(make("label", "", knob.label));
    const value = knobs[knob.id] ?? knob.default;
    let control;
    if (knob.kind === "boolean") {
      control = document.createElement("input");
      control.type = "checkbox";
      control.checked = Boolean(value);
      control.dataset.knobId = knob.id;
      control.dataset.kind = knob.kind;
      head.append(control);
    }
    card.append(head);
    card.append(make("p", "", knob.description));
    if (knob.kind === "select") {
      control = document.createElement("select");
      control.dataset.knobId = knob.id;
      control.dataset.kind = knob.kind;
      for (const option of knob.options || []) {
        const item = document.createElement("option");
        item.value = option;
        item.textContent = option;
        item.selected = option === value;
        control.append(item);
      }
      card.append(control);
    } else if (knob.kind === "multi_select") {
      if ((knob.options || []).length) {
        const options = make("div", "multi-options");
        for (const option of knob.options) {
          const label = document.createElement("label");
          const input = document.createElement("input");
          input.type = "checkbox";
          input.value = option;
          input.checked = Array.isArray(value) && value.includes(option);
          input.dataset.knobId = knob.id;
          input.dataset.kind = knob.kind;
          label.append(input, document.createTextNode(option));
          options.append(label);
        }
        card.append(options);
      } else {
        control = document.createElement("input");
        control.type = "text";
        control.dataset.knobId = knob.id;
        control.dataset.kind = knob.kind;
        control.value = Array.isArray(value) ? value.join(", ") : "";
        control.placeholder = "逗号分隔，例如 skill_catalog_v2";
        card.append(control);
      }
    }
    root.append(card);
  }
}

function collectKnobs() {
  const values = {};
  for (const knob of state.knobs) {
    if (knob.kind === "boolean") {
      const input = document.querySelector(`input[data-knob-id="${CSS.escape(knob.id)}"][data-kind="boolean"]`);
      values[knob.id] = Boolean(input && input.checked);
    } else if (knob.kind === "select") {
      const input = document.querySelector(`select[data-knob-id="${CSS.escape(knob.id)}"]`);
      values[knob.id] = input ? input.value : knob.default;
    } else if (knob.kind === "multi_select") {
      if ((knob.options || []).length) {
        values[knob.id] = [...document.querySelectorAll(`input[data-kind="multi_select"][data-knob-id="${CSS.escape(knob.id)}"]:checked`)]
          .map((input) => input.value);
      } else {
        const input = document.querySelector(`input[data-kind="multi_select"][data-knob-id="${CSS.escape(knob.id)}"]`);
        values[knob.id] = input
          ? input.value.split(",").map((item) => item.trim()).filter(Boolean)
          : [];
      }
    }
  }
  return values;
}

async function saveProfile() {
  const button = $("save-profile");
  button.disabled = true;
  button.textContent = "保存中…";
  showError("");
  try {
    const payload = await fetchJSON("/api/v1/experiments/active", {
      method: "PUT",
      body: JSON.stringify({
        schema_version: 1,
        id: state.profile.id,
        name: state.profile.name || "Web experiment",
        description: "Edited from Krelcode Web Observatory",
        knobs: collectKnobs(),
      }),
    });
    state.profile = payload.profile;
    $("profile-meta").textContent = `文件已保存 · profile ${shortId(payload.profile.profile_hash, 16)} · 下一轮生效`;
    renderWarnings(payload.warnings || []);
  } catch (error) {
    showError(`Profile 保存失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "保存下一轮 Profile";
  }
}

function renderWarnings(warnings) {
  const root = $("profile-warnings");
  root.replaceChildren();
  if (!warnings.length) {
    root.classList.add("hidden");
    return;
  }
  root.classList.remove("hidden");
  for (const warning of warnings) {
    root.append(make("div", "", `⚠ ${warning}`));
  }
}

function bindUI() {
  $("refresh-runs").addEventListener("click", loadRuns);
  $("save-profile").addEventListener("click", saveProfile);
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      $(`panel-${button.dataset.tab}`).classList.add("active");
    });
  });
  document.querySelectorAll(".filter").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.filter = button.dataset.kind;
      renderTimeline();
    });
  });
}

async function init() {
  bindUI();
  await checkServer();
  await Promise.all([loadRuns(), loadExperiments()]);
  window.setInterval(loadRuns, 5000);
  window.setInterval(checkServer, 15000);
}

init().catch((error) => showError(error.message));
