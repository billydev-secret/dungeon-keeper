export function mount(container) {
  container.innerHTML = `
    <div class="panel" style="display:flex;flex-direction:column;">
      <header>
        <h2>Live Log</h2>
        <div class="subtitle">Real-time bot log stream</div>
      </header>
      <div class="controls" style="gap:8px;">
        <label>Filter
          <input type="text" data-filter placeholder="e.g. ERROR, dungeonkeeper.web" style="min-width:220px;" />
        </label>
        <label style="flex-direction:row;align-items:center;gap:6px;">
          <input type="checkbox" data-autoscroll checked /> Auto-scroll
        </label>
        <button data-clear style="background:var(--bg-sidebar);color:var(--text);border:1px solid var(--grid);border-radius:4px;padding:5px 14px;font-size:12px;cursor:pointer;">Clear</button>
      </div>
      <pre data-log style="
        flex:1;min-height:0;overflow:auto;
        background:var(--bg-sidebar);border-radius:6px;padding:12px;
        font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all;
        color:var(--text);margin:0;
      "></pre>
      <div data-status style="font-size:11px;color:var(--text-dim);margin-top:4px;"></div>
    </div>
  `;

  const logEl = container.querySelector("[data-log]");
  const filterEl = container.querySelector("[data-filter]");
  const autoEl = container.querySelector("[data-autoscroll]");
  const clearBtn = container.querySelector("[data-clear]");
  const statusEl = container.querySelector("[data-status]");

  const MAX_LINES = 2000;
  let lineCount = 0;
  let evtSource = null;

  const LEVEL_COLORS = {
    "ERROR": "var(--danger)",
    "WARNING": "var(--warning)",
    "CRITICAL": "var(--danger)",
    "DEBUG": "var(--text-dim)",
  };

  function colorize(text) {
    // Highlight the level keyword
    for (const [level, color] of Object.entries(LEVEL_COLORS)) {
      if (text.includes(` ${level} `) || text.includes(` ${level}  `)) {
        return `<span style="color:${color}">${escHtml(text)}</span>`;
      }
    }
    return escHtml(text);
  }

  function escHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function appendLine(raw) {
    const filter = filterEl.value.trim().toLowerCase();
    if (filter && !raw.toLowerCase().includes(filter)) return;

    lineCount++;
    if (lineCount > MAX_LINES) {
      // Remove oldest lines
      const first = logEl.firstChild;
      if (first) logEl.removeChild(first);
    }

    const div = document.createElement("div");
    div.innerHTML = colorize(raw);
    logEl.appendChild(div);

    if (autoEl.checked) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function connect() {
    statusEl.textContent = "Connecting…";
    evtSource = new EventSource("/api/logs/stream");

    evtSource.onopen = () => {
      statusEl.textContent = "Connected";
    };

    evtSource.onmessage = (e) => {
      appendLine(e.data);
    };

    evtSource.onerror = () => {
      statusEl.textContent = "Disconnected — reconnecting…";
    };
  }

  clearBtn.addEventListener("click", () => {
    logEl.innerHTML = "";
    lineCount = 0;
  });

  connect();

  return {
    unmount() {
      if (evtSource) {
        evtSource.close();
        evtSource = null;
      }
    },
  };
}
