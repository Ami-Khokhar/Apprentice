(() => {
  const state = { traces: [], events: [], trace: null, event: null, filter: "all", tab: "input" };
  const traceList = document.querySelector("#trace-list");
  const flowList = document.querySelector("#flow-list");
  const timeline = document.querySelector("#timeline");
  const facts = document.querySelector("#event-facts");
  const eventJson = document.querySelector("#event-json");
  const copyButton = document.querySelector("#copy-event");

  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
  const shortId = (value) => String(value || "unknown").slice(0, 8);
  const clock = (value) => new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit" }).format(new Date(Number(value) * 1000));

  async function loadTraces() {
    try {
      const response = await fetch("/api/observer/traces");
      if (!response.ok) throw new Error("Trace access failed");
      state.traces = (await response.json()).traces;
      renderTraces();
      if (state.traces.length) selectTrace(state.traces[0].session_id);
    } catch (_error) {
      traceList.innerHTML = '<div class="loading-state">Local traces could not be read.</div>';
    }
  }

  function renderTraces() {
    const visible = state.traces.filter((item) => state.filter === "all" || item.status === state.filter);
    if (!visible.length) {
      traceList.innerHTML = '<div class="loading-state">No traces match this filter.</div>';
      return;
    }
    traceList.innerHTML = visible.map((item) => `
      <button class="trace-row ${state.trace === item.session_id ? "active" : ""}" type="button" data-session="${escapeHtml(item.session_id)}">
        <span class="trace-level">${item.difficulty_level ? `L${escapeHtml(item.difficulty_level)}` : "—"}</span>
        <span class="trace-session"><strong>${escapeHtml(item.field || item.title || shortId(item.session_id))}</strong><small>${escapeHtml(item.title || shortId(item.session_id))}</small><small class="${item.status === "error" ? "trace-status" : ""}">${escapeHtml(item.status)} · ${item.event_count} events</small></span>
        <time class="trace-time">${escapeHtml(clock(item.updated_at))}</time>
      </button>`).join("");
    traceList.querySelectorAll("[data-session]").forEach((button) => button.addEventListener("click", () => selectTrace(button.dataset.session)));
  }

  async function selectTrace(sessionId) {
    state.trace = sessionId;
    renderTraces();
    const response = await fetch(`/api/observer/traces/${encodeURIComponent(sessionId)}`);
    if (!response.ok) return;
    state.events = (await response.json()).events;
    renderFlow();
    if (state.events.length) selectEvent(state.events[0].id);
  }

  function eventLabel(event) {
    return event.name.replace("practice.", "").replaceAll("-", " ");
  }

  function renderFlow() {
    if (!state.events.length) return;
    flowList.innerHTML = state.events.map((event, index) => {
      const rejected = event.metadata?.result === "rejected" || event.status === "error";
      const reason = event.metadata?.rejection_reason || (event.error?.type ?? "");
      return `${index ? '<span class="flow-connector" aria-hidden="true"></span>' : ""}<button type="button" class="flow-node ${event.kind} ${rejected ? "rejected" : ""}" data-event="${escapeHtml(event.id)}">${escapeHtml(eventLabel(event))}${reason ? `<small>${escapeHtml(reason)}</small>` : ""}</button>`;
    }).join("");
    timeline.innerHTML = state.events.map((event, index) => `<button class="timeline-button" type="button" data-event="${escapeHtml(event.id)}">${escapeHtml(event.kind === "generation" ? `Model ${index + 1}` : eventLabel(event))}</button>`).join("");
    document.querySelectorAll("[data-event]").forEach((button) => button.addEventListener("click", () => selectEvent(button.dataset.event)));
  }

  function selectEvent(eventId) {
    state.event = state.events.find((item) => item.id === eventId) || null;
    document.querySelectorAll("[data-event]").forEach((node) => node.classList.toggle("active", node.dataset.event === eventId));
    renderInspector();
  }

  function evidenceFor(event) {
    const output = event?.output;
    if (!output || typeof output !== "object") return { message: "No evidence was recorded for this event." };
    return output.evidence || output.facilitator_decision?.assessment?.evidence || { message: "No evidence was recorded for this event." };
  }

  function renderInspector() {
    const event = state.event;
    if (!event) return;
    const metadata = event.metadata || {};
    const result = metadata.rejection_reason ? `Rejected · ${metadata.rejection_reason}` : (metadata.result || event.status);
    const values = [metadata.agent_name || event.name, metadata.model || "—", metadata.attempt || "—", `${event.duration_ms}ms`, result];
    facts.querySelectorAll("dd").forEach((node, index) => { node.textContent = values[index]; });
    const selected = state.tab === "input" ? event.input : state.tab === "output" ? (event.output ?? { message: "No output recorded." }) : evidenceFor(event);
    eventJson.textContent = JSON.stringify(selected, null, 2);
    copyButton.disabled = false;
  }

  document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("active", item === button));
    renderTraces();
  }));
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => {
    state.tab = button.dataset.tab;
    document.querySelectorAll("[data-tab]").forEach((item) => item.classList.toggle("active", item === button));
    renderInspector();
  }));
  copyButton.addEventListener("click", async () => {
    if (!state.event) return;
    await navigator.clipboard.writeText(JSON.stringify(state.event, null, 2));
    copyButton.textContent = "Copied";
    window.setTimeout(() => { copyButton.textContent = "Copy event"; }, 1200);
  });
  loadTraces();
})();
