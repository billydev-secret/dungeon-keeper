// Mod Coverage — is a moderator around when the server is busy?
//
// The hero is the Activity panel's day overlay (today against a band of recent
// same-weekdays) with one line added: the moderators' own messages, on the
// SAME y-axis. That shared axis is the whole point. A second scale would make
// where the mod line sits relative to the server's traffic an artefact of
// autoscaling rather than a fact about coverage, and the fact is the report.
//
// Beneath it, the pattern the hero only hints at: per hour of the local clock,
// on what share of recent days was a moderator talking while the server was.
import { api, esc } from "../api.js";
import { makeOverlayChart, renderChartLegend, renderChartTable } from "../charts.js";
import { mountAsync } from "../config-helpers.js";
import { renderLoading } from "../states.js";

const MOD_LINE_LABEL = "Moderators today";

function sum(arr) {
  return (arr || []).reduce((a, v) => a + (Number.isFinite(v) ? v : 0), 0);
}

/** "3am", "11pm" — the hour labels the tiles speak in, not "03". */
function hourWord(h) {
  const n = h % 12 || 12;
  return `${n}${h < 12 ? "am" : "pm"}`;
}

function hourRange(start, hours) {
  if (hours >= 24) return "all day";
  const end = (start + hours) % 24;
  return `${hourWord(start)}–${hourWord(end)}`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading mod coverage…")}</div>`;
  return mountAsync(container, async () => {
    // Deliberately unguarded: mountAsync must SEE this reject, or its retry
    // button never appears and a failed first fetch is a permanent spinner.
    const d = await api("/api/health/mod-coverage");
    return render(container, d);
  }, { errorMsg: "Couldn’t load moderator coverage." });
}

function render(container, d) {
  const hasBand = (d.band_mid || []).length > 0;
  const hasMods = d.mod_count > 0;
  const lived = (d.server_current || []).filter((c) => c !== null && c !== undefined).length;
  const serverTotal = sum(d.server_current);
  const modTotal = sum(d.mod_current);
  const typicalToDate = hasBand ? sum((d.band_mid || []).slice(0, lived)) : 0;

  const gap = d.longest_gap;
  const worst = d.busiest_uncovered;
  const threshold = d.covered_threshold_pct;

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Mod Coverage</h2>
        <div class="subtitle">Is a moderator around when the server is busy?</div>
      </header>

      ${d.degraded ? `<div class="note">Discord hasn’t finished sending the member
        list yet, so the moderator side of this report may be incomplete.
        Reload in a minute.</div>` : ""}

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          <strong>Moderator</strong> — anyone who can delete another member’s
          message. That is a wider group than the Mod Workload or Moderator
          Community Engagement reports count, which is why they don’t match:
          this one asks who is <em>present</em>, not who takes action.
          <strong>The chart</strong> — today’s messages per hour, drawn against
          what a typical ${esc(d.weekday || "day")} looks like, with the
          moderators’ own messages over the top. Bots are excluded throughout.
          <strong>Covered</strong> — an hour where a moderator was talking on
          more than ${threshold}% of the last ${d.gap_days} days. Below that,
          a member arriving in that hour can’t count on finding anyone.
          Hours nobody posted in at all are neither covered nor a gap — there
          was nothing there to miss.
          <strong>By moderator</strong> — the same measure, split per person:
          how many of the window’s days they showed up at all, and how much
          of the busy hours above they personally covered. It’s presence, not
          a ranking — read it as “was this person around”, not “who did the
          most”.
        </div>
      </details>

      <div class="chart-caption" data-caption></div>
      <div class="chart-wrap" style="min-height:320px"><canvas data-chart></canvas></div>
      <div data-legend></div>
      <div data-chart-table></div>

      <h3 style="margin-top:22px;">Coverage gaps</h3>
      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Busy hours covered</div>
          <div class="home-card-big">${d.busy_hours_covered} / ${d.busy_hours}</div>
          <div class="home-card-sub">The server’s busiest quarter of the clock</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Coverage at peak</div>
          <div class="home-card-big">${d.peak_coverage_pct}%</div>
          <div class="home-card-sub">Share of days with a mod talking in those hours</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Longest gap</div>
          <div class="home-card-big">${gap ? `${gap.hours}h` : "None"}</div>
          <div class="home-card-sub">${gap
            ? esc(hourRange(gap.start_hour, gap.hours))
            : `Every active hour clears ${threshold}%`}</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Busiest uncovered hour</div>
          <div class="home-card-big">${worst ? esc(hourWord(worst.hour)) : "None"}</div>
          <div class="home-card-sub">${worst
            ? `${worst.server_messages} messages over ${d.gap_days} days · mod present ${worst.coverage_pct}% of them`
            : "No busy hour falls short"}</div>
        </div>
      </div>

      ${hasMods ? "" : `<div class="note" style="margin-top:12px;">No members
        currently hold permission to delete messages, so there is no moderator
        side to draw. The server’s own activity is shown alone.</div>`}

      ${hasMods ? '<div data-mods-table style="margin-top:22px;"></div>' : ""}

      <div data-hours-table style="margin-top:18px;"></div>
    </div>
  `;

  const captionEl = container.querySelector("[data-caption]");
  const legendEl = container.querySelector("[data-legend]");
  const tableEl = container.querySelector("[data-chart-table]");

  const typical = d.band_label || "Typical day";
  const windowLabel = hasBand
    ? `Today vs Last ${d.periods_sampled} ${esc(d.weekday || "day")}s`
    : `Today (no past ${esc(d.weekday || "day")}s to compare against yet)`;
  // The hour in progress is a real count of a few minutes, so both lines are
  // marked there rather than left to read as a crash. Named in words too: the
  // mark is a convention, and this is where a reader learns it.
  const partialNote = Number.isInteger(d.partial_from)
    ? " · open end = the hour in progress"
    : "";
  captionEl.textContent = `Messages — ${windowLabel} (${d.tz_label})${partialNote}`;

  // The overlay data shape the shared chart builder expects. `counts` is the
  // server line; the moderators ride in as an extra series so this panel adds
  // no second copy of the band-drawing code.
  const chartData = {
    labels: d.labels,
    counts: d.server_current,
    band_low: d.band_low,
    band_mid: d.band_mid,
    band_high: d.band_high,
    y_label: "Messages",
    partial_from: d.partial_from,
  };
  const chart = makeOverlayChart(container.querySelector("[data-chart]"), chartData, {
    subject: "Server today",
    typical,
    isWeek: false,
    currentTotal: serverTotal,
    typicalToDate,
    extraSeries: hasMods
      ? [{ label: MOD_LINE_LABEL, data: d.mod_current, total: modTotal }]
      : [],
  });

  legendEl.replaceChildren();
  renderChartLegend(legendEl, chart);

  // Tooltips enhance; they are never the only way to read a value.
  renderChartTable(tableEl, {
    labels: d.labels,
    datasets: [
      { label: "Server today", data: d.server_current },
      ...(hasMods ? [{ label: MOD_LINE_LABEL, data: d.mod_current }] : []),
      ...(hasBand ? [
        { label: `${typical} (median)`, data: d.band_mid },
        { label: `${typical} (p25)`, data: d.band_low },
        { label: `${typical} (p75)`, data: d.band_high },
      ] : []),
    ],
    indexLabel: "Hour of day",
  });

  if (hasMods) {
    renderModsTable(container.querySelector("[data-mods-table]"), d);
  }

  renderHoursTable(container.querySelector("[data-hours-table]"), d);

  return {
    unmount() {
      if (chart) chart.destroy();
    },
  };
}

function renderHoursTable(host, d) {
  const rows = (d.hours || []).map((r) => {
    const state = !r.days_observed
      ? '<span class="home-dim">quiet</span>'
      : r.gap
        ? '<strong>gap</strong>'
        : "covered";
    return `
      <tr>
        <td>${esc(hourWord(r.hour))}${r.busy ? ' <span class="home-dim">busy</span>' : ""}</td>
        <td>${r.server_messages}</td>
        <td>${r.days_with_mod} / ${r.days_observed}</td>
        <td>${r.coverage_pct}%</td>
        <td>${state}</td>
      </tr>`;
  }).join("");

  host.innerHTML = `
    <details class="panel-about">
      <summary>Hour by hour (last ${d.gap_days} days)</summary>
      <div class="data-table-scroll">
        <table class="data-table">
          <thead><tr>
            <th>Hour (${esc(d.tz_label)})</th>
            <th>Messages</th>
            <th>Days with a mod</th>
            <th>Coverage</th>
            <th></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </details>`;
}

// Deliberately not sorted here — the API already orders by name (see
// health.py), and a client-side re-sort by any of these numbers would turn
// "was this person around" back into "who did the most" one refresh later.
function renderModsTable(host, d) {
  const rows = (d.mods || []).map((m) => `
    <tr>
      <td>${esc(m.user_name || `User ${m.user_id}`)}</td>
      <td>${m.days_active} / ${d.gap_days}</td>
      <td>${m.busy_hours_covered} / ${d.busy_hours}</td>
      <td>${m.peak_coverage_pct}%</td>
    </tr>`).join("");

  host.innerHTML = `
    <h3>By moderator</h3>
    <div class="note">Each moderator’s own presence — not a comparison
      between them. “Active days” is how many of the last ${d.gap_days} days
      they posted at all; “busy hours covered” is how much of the server’s
      busiest quarter of the clock they personally covered.</div>
    <div class="data-table-scroll">
      <table class="data-table">
        <thead><tr>
          <th>Moderator</th>
          <th>Active days</th>
          <th>Busy hours covered</th>
          <th>Coverage at peak</th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="home-dim">No data</td></tr>'}</tbody>
      </table>
    </div>`;
}
