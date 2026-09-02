import { api, esc } from "../api.js";
import { mountReloadable } from "../report-helpers.js";
import { renderSortableTable } from "../table.js";
import { CHART_TEXT, CHART_GRID, seriesColor } from "../charts.js";

// Five separate views, deliberately with no composite and no overall rank. The
// score this replaced blended four signals into one weighted number; a
// nine-week backtest found that not one member appeared on all three of the
// original families, so no set of weights describes these roles — they are
// held by different people. See docs/plans/quality-score-revisit.md.

const LIFT = (v) => `${v.toFixed(2)}x`;
const PCT = (v) => `${(v * 100).toFixed(1)}%`;
const NUM = (v) => (v == null ? "" : String(v));

// html:true — the markup is the point, and the interpolated value is a
// computed number, never a name (see table.js's ESCAPING note).
function liftCell(v) {
  const color = v >= 2 ? "var(--green-text)" : v >= 1.25 ? "var(--yellow-text)" : "var(--ink-dim)";
  return `<span style="color:${color};font-weight:700">${LIFT(v)}</span>`;
}

const MEMBER_COL = {
  key: "user_name",
  label: "Member",
  format: (v, r) => r.user_name || r.user_id,
};
const LIFT_COL = { key: "score", label: "Lift", html: true, format: liftCell };

const VIEWS = {
  popular: {
    label: "Popular Content",
    blurb:
      "Who posts things people respond to. Lift compares the unique people reacting or replying to their posts against that channel's own average, so a strong post in a quiet room counts for more than an ordinary one in a busy room.",
    empty: "Nobody has posted enough in this period to compare against their channels.",
    columns: [
      MEMBER_COL,
      LIFT_COL,
      { key: "volume", label: "Posts", format: NUM },
      { key: "own_rate", label: "Responders / post", format: (v) => v.toFixed(2) },
      { key: "baseline", label: "Room average", format: (v) => v.toFixed(2) },
      { key: "partners", label: "Distinct responders", format: NUM },
    ],
  },
  catalyst: {
    label: "Conversation Catalyst",
    blurb:
      "Who restarts a quiet room. A message after 3+ hours of channel silence counts as a restart when 3+ messages from 2+ other people follow within half an hour. Lift is against how often anyone restarts those same channels.",
    empty: "No restarts to measure in this period.",
    columns: [
      MEMBER_COL,
      LIFT_COL,
      { key: "given", label: "Restarted", format: NUM },
      { key: "volume", label: "Quiet moments entered", format: NUM },
      { key: "own_rate", label: "Their rate", format: PCT },
      { key: "baseline", label: "Room rate", format: PCT },
    ],
  },
  connectors: {
    label: "Connectors",
    blurb:
      "Who spreads attention widely. Ranked by how many distinct people they engage. Reciprocity is what they give divided by what they get back; top partner is the share of their attention going to one person, so a low number means genuine breadth rather than one friendship.",
    empty: "Nobody has engaged enough people in this period.",
    columns: [
      MEMBER_COL,
      { key: "partners", label: "People engaged", format: NUM },
      { key: "given", label: "Given", format: NUM },
      { key: "received", label: "Received", format: NUM },
      { key: "own_rate", label: "Reciprocity", format: (v) => (v ? v.toFixed(2) : "—") },
      { key: "concentration", label: "Top partner", format: PCT },
    ],
  },
  welcomers: {
    label: "Welcomers",
    blurb:
      "Who answers newcomers, measured inside a member's first 14 days of posting. Lift is the share of their own replies aimed at newcomers against the server-wide share — a share rather than a count, because the busiest repliers answer the most of everyone.",
    empty: "Nobody has answered enough newcomers in this period.",
    columns: [
      MEMBER_COL,
      LIFT_COL,
      { key: "partners", label: "Newcomers reached", format: NUM },
      { key: "own_rate", label: "Share of their replies", format: PCT },
      { key: "baseline", label: "Server share", format: PCT },
      { key: "volume", label: "Replies sent", format: NUM },
    ],
  },
  under_attended: {
    label: "Lifts the Under-Attended",
    blurb:
      "Who engages people few others do. Every reply and reaction is weighted by how little attention its target usually receives, so consistently turning toward the overlooked scores above simply being busy.",
    empty: "Not enough engagement in this period to compare.",
    columns: [
      MEMBER_COL,
      LIFT_COL,
      { key: "volume", label: "Replies + reactions", format: NUM },
      { key: "partners", label: "People engaged", format: NUM },
    ],
  },
};

const VIEW_KEYS = Object.keys(VIEWS);

// `initialParams` is the parsed window.location.hash (see app.js parseHash), so
// every value in it arrives from an attacker-controlled link. `view` is checked
// against the known keys rather than trusted, and `days` goes through parseInt.
export function mount(container, initialParams) {
  const startView = VIEW_KEYS.includes(initialParams.view) ? initialParams.view : "popular";
  const startDays = parseInt(initialParams.days) || 90;

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Contributors</h2>
        <div class="subtitle">Who keeps the conversation going, posts what people respond to, and turns toward the people who show up</div>
      </header>
      <div class="controls">
        <label>Period
          <select data-control="days">
            <option value="30">Last 30 days</option>
            <option value="60">Last 60 days</option>
            <option value="90">Last 90 days</option>
            <option value="180">Last 180 days</option>
          </select>
        </label>
      </div>
      <div class="tab-row" data-views style="display:flex; flex-wrap:wrap; gap:8px; margin:12px 0;"></div>
      <p data-blurb class="subtitle" style="max-width:70ch; margin:0 0 12px;"></p>
      <div data-meta class="subtitle" style="margin-bottom:12px;"></div>
      <div class="chart-wrap" data-chart style="margin-bottom:16px;"><canvas></canvas></div>
      <div data-table></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const viewsEl = container.querySelector("[data-views]");
  const blurbEl = container.querySelector("[data-blurb]");
  const metaEl = container.querySelector("[data-meta]");
  const chartWrap = container.querySelector("[data-chart]");
  const tableWrap = container.querySelector("[data-table]");

  daysEl.value = String(startDays);
  let view = startView;
  let chart = null;
  let payload = null;

  for (const key of VIEW_KEYS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn";
    btn.dataset.view = key;
    btn.textContent = VIEWS[key].label;
    btn.addEventListener("click", () => {
      view = key;
      render();
    });
    viewsEl.appendChild(btn);
  }

  function syncUrl() {
    const qs = new URLSearchParams({ days: daysEl.value, view });
    history.replaceState(null, "", `#/quality-score?${qs}`);
  }

  function destroyChart() {
    if (chart) {
      chart.destroy();
      chart = null;
    }
  }

  function render() {
    if (!payload) return;
    syncUrl();
    const spec = VIEWS[view];
    const rows = payload[view] || [];

    for (const btn of viewsEl.querySelectorAll("button")) {
      btn.classList.toggle("active", btn.dataset.view === view);
    }
    blurbEl.textContent = spec.blurb;
    metaEl.textContent = `${rows.length} of ${payload.members_considered} active members qualify over ${payload.window_days} days.`;

    destroyChart();
    const top = rows.slice(0, 12);
    if (top.length) {
      chartWrap.innerHTML = "<canvas></canvas>";
      chartWrap.style.minHeight = `${Math.max(200, top.length * 28 + 60)}px`;
      const isCount = view === "connectors";
      chart = new Chart(chartWrap.querySelector("canvas"), {
        type: "bar",
        data: {
          labels: top.map((e) => e.user_name || e.user_id),
          datasets: [
            {
              label: isCount ? "People engaged" : "Lift vs baseline",
              data: top.map((e) => (isCount ? e.partners : Number(e.score.toFixed(2)))),
              backgroundColor: seriesColor(0),
            },
          ],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false }, title: { display: false } },
          scales: {
            x: {
              grid: { color: CHART_GRID },
              ticks: { color: CHART_TEXT },
              beginAtZero: true,
              title: {
                display: true,
                text: isCount ? "Distinct people engaged" : "Lift (1.0 = the room's own rate)",
                color: CHART_TEXT,
              },
            },
            y: { grid: { color: CHART_GRID }, ticks: { color: CHART_TEXT } },
          },
        },
      });
    } else {
      chartWrap.innerHTML = "";
      chartWrap.style.minHeight = "0";
    }

    renderSortableTable(tableWrap, {
      columns: spec.columns,
      data: rows,
      defaultSort: view === "connectors" ? "partners" : "score",
      emptyMsg: spec.empty,
      maxRows: 300,
    });
  }

  const reload = mountReloadable(container, {
    load: () => api("/api/reports/quality-score", { days: parseInt(daysEl.value) || 90 }),
    decorate: (data) => {
      payload = data;
      render();
    },
    describe: "contributors",
    renderError: (msg) => `<div class="error">${esc(msg)}</div>`,
  });

  daysEl.addEventListener("change", () => reload());

  return {
    unmount() {
      destroyChart();
    },
  };
}
