import { esc } from "../api.js";

export function mount(container) {
  container.innerHTML = `
    <div class="panel" style="display:flex;flex-direction:column;">
      <header>
        <h2>Live Log</h2>
        <div class="subtitle">Real-time bot log stream</div>
      </header>
      <div class="controls" style="gap:8px;">
        <label>Filter
          <input type="search" data-filter placeholder="e.g. ERROR, dungeonkeeper.web" style="min-width:220px;" />
        </label>
        <label style="flex-direction:row;align-items:center;gap:6px;">
          <input type="checkbox" data-autoscroll checked /> Auto-Scroll
        </label>
        <button data-clear class="btn btn-sm">Clear</button>
      </div>
      <pre data-log style="
        flex:1;min-height:0;overflow:auto;
        background:var(--bg-rail);border-radius:6px;padding:12px;
        font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all;
        color:var(--ink);margin:0;
      "></pre>
      <div data-status style="font-size:11px;color:var(--ink-dim);margin-top:4px;"></div>
    </div>
  `;

  const logEl = container.querySelector("[data-log]");
  const filterEl = container.querySelector("[data-filter]");
  const autoEl = container.querySelector("[data-autoscroll]");
  const clearBtn = container.querySelector("[data-clear]");
  const statusEl = container.querySelector("[data-status]");

  const MAX_LINES = 2000;
  // Every line received stays in this ring buffer, filtered or not, so changing
  // the filter can re-run it over the whole session instead of only over lines
  // that arrive afterwards (W-D15).
  const buffer = [];
  let evtSource = null;
  let connState = "Connecting…";
  let matchNote = "";

  function renderStatus() {
    statusEl.textContent = [connState, matchNote].filter(Boolean).join(" · ");
  }

  const LEVEL_COLORS = {
    "ERROR": "var(--red)",
    "WARNING": "var(--yellow)",
    "CRITICAL": "var(--red)",
    "DEBUG": "var(--ink-dim)",
  };

  function colorize(text) {
    // Highlight the level keyword
    for (const [level, color] of Object.entries(LEVEL_COLORS)) {
      if (text.includes(` ${level} `) || text.includes(` ${level}  `)) {
        return `<span style="color:${color}">${esc(text)}</span>`;
      }
    }
    return esc(text);
  }

  function matchesFilter(raw) {
    const filter = filterEl.value.trim().toLowerCase();
    return !filter || raw.toLowerCase().includes(filter);
  }

  function scrollIfPinned() {
    if (autoEl.checked) logEl.scrollTop = logEl.scrollHeight;
  }

  function appendLine(raw) {
    buffer.push(raw);
    if (buffer.length > MAX_LINES) buffer.shift();

    if (!matchesFilter(raw)) return;
    if (logEl.childElementCount >= MAX_LINES) {
      const first = logEl.firstChild;
      if (first) logEl.removeChild(first);
    }

    const div = document.createElement("div");
    div.innerHTML = colorize(raw);
    logEl.appendChild(div);
    scrollIfPinned();
  }

  /** Re-run the current filter over every buffered line. */
  function rerenderLog() {
    const shown = buffer.filter(matchesFilter);
    logEl.innerHTML = shown.map((raw) => `<div>${colorize(raw)}</div>`).join("");
    matchNote = shown.length === buffer.length
      ? ""
      : `Showing ${shown.length} of ${buffer.length} buffered lines`;
    renderStatus();
    scrollIfPinned();
  }

  let filterTimer = null;
  filterEl.addEventListener("input", () => {
    if (filterTimer) clearTimeout(filterTimer);
    filterTimer = setTimeout(rerenderLog, 150);
  });

  function connect() {
    connState = "Connecting…";
    renderStatus();
    evtSource = new EventSource("/api/logs/stream");

    evtSource.onopen = () => {
      connState = "Connected — streaming live";
      renderStatus();
    };

    evtSource.onmessage = (e) => {
      appendLine(e.data);
    };

    evtSource.onerror = () => {
      connState = "Disconnected from the log stream — reconnecting…";
      renderStatus();
    };
  }

  clearBtn.addEventListener("click", () => {
    buffer.length = 0;
    logEl.innerHTML = "";
    matchNote = "";
    renderStatus();
  });

  connect();

  return {
    unmount() {
      if (filterTimer) clearTimeout(filterTimer);
      if (evtSource) {
        evtSource.close();
        evtSource = null;
      }
    },
  };
}
