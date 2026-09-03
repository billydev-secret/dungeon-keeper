import { api, esc, fmtTs } from "../api.js";
import { jumpLink } from "../audit-helpers.js";
import { renderEmpty, renderError } from "../states.js";
import { mountReloadable, syncHash } from "../report-helpers.js";
import { makeLineChart, renderChartTable, CHART_BAR } from "../charts.js";
import { renderSortableTable } from "../table.js";

// How long after a ping we still count someone as having turned up. Offered as
// a control rather than a setting because nothing is precomputed — the report
// re-counts against retained messages and reactions, so the honest window for
// a game start ("now") and for an announcement ("today") can both be asked for.
const WINDOWS = [
  { value: 5, label: "5 minutes" },
  { value: 15, label: "15 minutes" },
  { value: 30, label: "30 minutes" },
  { value: 60, label: "1 hour" },
  { value: 180, label: "3 hours" },
  { value: 720, label: "12 hours" },
  { value: 1440, label: "24 hours" },
];

const RANGES = [
  { value: 7, label: "Last 7 days" },
  { value: 30, label: "Last 30 days" },
  { value: 90, label: "Last 90 days" },
  { value: 365, label: "Last year" },
];

const SOURCE_LABELS = {
  game_start: "Game start",
  bot: "Dungeon Keeper",
  external: "Another bot",
  member: "Member",
};

// Most role pings in a busy server come from unrelated third-party bots — on
// this server they were 71% of them. Without this filter their noise buries
// the pings anyone is actually asking about.
const SENDERS = [
  { value: "all", label: "Anyone" },
  { value: "self", label: "Dungeon Keeper" },
  { value: "member", label: "Members" },
  { value: "external", label: "Other bots" },
];

function options(list, selected) {
  return list
    .map(
      (o) =>
        `<option value="${esc(String(o.value))}"${String(o.value) === String(selected) ? " selected" : ""}>${esc(o.label)}</option>`,
    )
    .join("");
}

export function mount(container, initialParams = {}) {
  let days = Number(initialParams.days) || 30;
  let windowMinutes = Number(initialParams.window) || 30;
  let sentBy = initialParams.sent_by || "all";
  let includeBots = initialParams.include_bots === "true";
  let chart = null;

  container.innerHTML =
    '<div class="panel"><div class="panel-loading">Loading ping response…</div></div>';

  function breakdownColumns(idLabel) {
    return [
      { key: "label", label: idLabel },
      { key: "pings", label: "Pings", cls: "num" },
      { key: "mean_turnout", label: "Avg turnout", cls: "num" },
      { key: "median_turnout", label: "Median", cls: "num" },
      {
        key: "silent_pct",
        label: "Ignored",
        cls: "num",
        format: (v, r) => `${v}% (${r.silent_pings})`,
      },
    ];
  }

  async function load() {
    const params = { days, window_minutes: windowMinutes, sent_by: sentBy };
    if (includeBots) params.include_bots = "true";
    syncHash("ping-response", {
      days,
      window: windowMinutes,
      sent_by: sentBy,
      ...(includeBots ? { include_bots: "true" } : {}),
    });
    // The endpoint answers 200 with zero pings rather than 404-ing, so a
    // rejection here is a real failure and must reach mountReloadable.
    return api("/api/reports/ping-response", params);
  }

  function decorate(data) {
    if (chart) {
      chart.destroy();
      chart = null;
    }
    const panel = container.querySelector(".panel");

    const header = `
      <header>
        <h2>Ping Response</h2>
        <div class="subtitle">How many people turn up after a role ping</div>
      </header>

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          Every <strong>role ping</strong> — <code>@Gamers</code>, <code>@everyone</code>,
          the bot's own game announcements — is recorded when it is sent. Most
          role pings on a busy server come from unrelated third-party bots, so
          use <strong>Sent by</strong> to narrow it to the ones you care about.
          <strong>Turnout</strong> is the number of distinct people who, within the
          response window, either posted in that channel or reacted to the ping
          itself. Someone who does both counts once; someone who posts ten times
          counts once. Whoever sent the ping never counts as their own response.
          <strong>Messages</strong> is the raw volume alongside it, because one
          person saying forty things and forty people saying one thing are
          different nights.
          For a ping the bot sent to launch a game, <strong>Played</strong> is the
          game's actual roster — the honest answer to whether the ping worked.
          Blank means no game was attached, or it left no roster behind.
          Change the <strong>response window</strong> to re-ask the question:
          nothing is precomputed, so a longer window recounts rather than
          estimating.
        </div>
      </details>

      <div class="controls">
        <label>Range
          <select data-days>${options(RANGES, days)}</select>
        </label>
        <label>Response window
          <select data-window>${options(WINDOWS, windowMinutes)}</select>
        </label>
        <label>Sent by
          <select data-sender>${options(SENDERS, sentBy)}</select>
        </label>
        <label class="inline-check">
          <input type="checkbox" data-bots${includeBots ? " checked" : ""}>
          Count bots as turnout
        </label>
      </div>
    `;

    if (!data || !data.total_pings) {
      panel.innerHTML =
        header +
        renderEmpty(
          "No role pings recorded in this window. Tracking starts the next time " +
            "the bot sees a role ping. Past ones can be recovered by a maintenance " +
            "script the bot owner runs on the server.",
        );
      wireControls(panel);
      return;
    }

    const answered = data.total_pings - data.silent_pings;
    const answeredPct = data.total_pings
      ? Math.round((answered / data.total_pings) * 100)
      : 0;

    panel.innerHTML = `
      ${header}
      <div class="card-grid" style="margin-bottom:8px;">
        <div class="stat">
          <div class="stat-label">Pings</div>
          <div class="stat-value">${data.total_pings}</div>
        </div>
        <div class="stat">
          <div class="stat-label">Got a response</div>
          <div class="stat-value">${answeredPct}%</div>
        </div>
        <div class="stat">
          <div class="stat-label">Median turnout</div>
          <div class="stat-value">${data.median_turnout}</div>
        </div>
        <div class="stat stat-warning">
          <div class="stat-label">Ignored entirely</div>
          <div class="stat-value">${data.silent_pings}</div>
        </div>
      </div>

      <div class="chart-caption" data-caption></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-chart-table></div>

      <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-top:16px;">
        <div>
          <div class="section-label">By role</div>
          <div data-by-role></div>
        </div>
        <div>
          <div class="section-label">By channel</div>
          <div data-by-channel></div>
        </div>
      </div>

      <div class="section-label" style="margin-top:16px;">Recent pings</div>
      <div data-entries style="max-height:420px;overflow-y:auto;"></div>
    `;

    const labels = data.series.map((p) => p.day);
    const values = data.series.map((p) => p.mean_turnout);

    panel.querySelector("[data-caption]").textContent =
      `Average turnout per ping, by day — ${data.window_label}, ` +
      `counted over ${data.window_minutes} minutes after each ping.`;

    // One series, one axis. Ping volume is a second measure on a different
    // scale, so it belongs in the numbers table below rather than on a second
    // y-axis — and a single series names itself in the caption, so no legend.
    chart = makeLineChart(panel.querySelector("[data-chart]"), {
      labels,
      series: [{ label: "Avg turnout", counts: values, color: CHART_BAR }],
    });

    renderChartTable(panel.querySelector("[data-chart-table]"), {
      labels,
      datasets: [
        { label: "Avg turnout", data: values },
        { label: "Pings", data: data.series.map((p) => p.pings) },
      ],
      indexLabel: "Day",
    });

    renderSortableTable(panel.querySelector("[data-by-role]"), {
      columns: breakdownColumns("Role"),
      data: data.by_role || [],
      defaultSort: "pings",
      emptyMsg: "No role pings in this window.",
    });

    renderSortableTable(panel.querySelector("[data-by-channel]"), {
      columns: breakdownColumns("Channel"),
      data: data.by_channel || [],
      defaultSort: "pings",
      emptyMsg: "No pings in this window.",
    });

    renderSortableTable(panel.querySelector("[data-entries]"), {
      columns: [
        { key: "ts", label: "When", format: (v) => fmtTs(v) },
        {
          key: "channel_name",
          label: "Channel",
          format: (v, r) => (v ? `#${v}` : r.channel_id),
        },
        {
          key: "role_labels",
          label: "Pinged",
          format: (v) => (v && v.length ? v.join(", ") : "—"),
        },
        {
          key: "author_name",
          label: "Sent by",
          format: (v, r) => v || r.author_id,
        },
        {
          key: "source",
          label: "Kind",
          format: (v) => SOURCE_LABELS[v] || v,
        },
        { key: "turnout", label: "Turned up", cls: "num" },
        { key: "messages", label: "Messages", cls: "num" },
        {
          key: "players",
          label: "Played",
          cls: "num",
          // Blank, not 0: "no game attached" and "a game nobody joined" are
          // different facts and must not render the same.
          format: (v) => (v == null ? "—" : String(v)),
        },
        {
          key: "message_id",
          label: "",
          html: true,
          // This column opts into markup, so it escapes its own interpolations
          // — both ids are server-side integers rendered as strings, and both
          // go through esc() rather than trusting that.
          format: (v, r) =>
            `<a href="${esc(jumpLink(r.channel_id, v))}" target="_blank" rel="noopener noreferrer">Open</a>`,
        },
      ],
      data: data.entries || [],
      defaultSort: "ts",
      emptyMsg: "No pings in this window.",
      maxRows: 200,
    });

    wireControls(panel);
  }

  function wireControls(panel) {
    const daysEl = panel.querySelector("[data-days]");
    const windowEl = panel.querySelector("[data-window]");
    const senderEl = panel.querySelector("[data-sender]");
    const botsEl = panel.querySelector("[data-bots]");
    if (daysEl) {
      daysEl.addEventListener("change", () => {
        days = Number(daysEl.value);
        reload();
      });
    }
    if (windowEl) {
      windowEl.addEventListener("change", () => {
        windowMinutes = Number(windowEl.value);
        reload();
      });
    }
    if (senderEl) {
      senderEl.addEventListener("change", () => {
        sentBy = senderEl.value;
        reload();
      });
    }
    if (botsEl) {
      botsEl.addEventListener("change", () => {
        includeBots = botsEl.checked;
        reload();
      });
    }
  }

  // Every pass is guarded, not just the first — see mountReloadable.
  const reload = mountReloadable(container, {
    load,
    decorate,
    describe: "the ping response report",
    renderError,
  });

  // Chart.js keeps its own handle on the canvas and its resize listeners, so a
  // panel that navigates away without destroying it leaks one per visit.
  return { unmount() { if (chart) chart.destroy(); } };
}
