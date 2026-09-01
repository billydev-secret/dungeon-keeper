import { api } from "../api.js";
import { withLoading } from "../report-helpers.js";
import {
  makeBarChart, makeOverlayChart, renderChartLegend, renderChartTable,
  CHART_BAR, CHART_ACCENT, CHART_TEXT, CHART_GRID, CHART_SURFACE, ROLE_COLORS,
} from "../charts.js";
import { mountTimeSlider } from "../slider.js";
import { renderEmpty, renderError } from "../states.js";
import { filterSelect, multiFilterSelect } from "../filter-select.js";
import { onPickerChange, memberSearch } from "../config-helpers.js";
import { mountPanelPoster } from "../panel-post.js";

const RESOLUTIONS = [
  { value: "hour",        label: "Hourly (24h)" },
  { value: "day",         label: "Daily (30d)" },
  { value: "week",        label: "Weekly (12wk)" },
  { value: "month",       label: "Monthly (12mo)" },
  { value: "hour_of_day", label: "By Hour of Day" },
  { value: "day_of_week", label: "By Day of Week" },
  { value: "day_overlay",  label: "Today vs Recent Days" },
  { value: "week_overlay", label: "This Week vs Recent Weeks" },
];

// The overlay views: the current period drawn against a p25-p75 band over the
// last N. `cap` mirrors activity_graphs.overlay_period_cap() — the server
// clamps regardless, this is only so the reader can see *why* an option is
// unavailable instead of silently getting a shorter window than they picked.
const OVERLAY = {
  day_overlay: {
    unit: "days",
    presets: [7, 14, 28, 90],
    fallback: 28,
    caps: { messages: 90, xp: 90 },
    // The second basis for the same picker: sample every seventh day back
    // instead of every day, so a Tuesday is read against Tuesdays. Weekday
    // seasonality dominates a server's rhythm — a weekday drawn against a band
    // with weekends in it mostly tells you about the weekend. Stepping a week
    // at a time gives it a week overlay's reach, and a week overlay's caps.
    weekday: {
      unit: "same weekdays",
      presets: [4, 8, 12, 26],
      fallback: 8,
      caps: { messages: 26, xp: 12 },
    },
  },
  week_overlay: {
    unit: "weeks",
    presets: [4, 6, 8, 12, 26],
    fallback: 12,
    caps: { messages: 26, xp: 12 },
  },
};

const isOverlay = (res) => Object.prototype.hasOwnProperty.call(OVERLAY, res);

// The compare picker carries two things — how far back, and which days count —
// in one control rather than a window select beside a "same weekday" tick that
// only means anything for one of the two resolutions. Option values are
// `basis:n`; `all` is every period, `weekday` only days sharing today's.
const BASIS_ALL = "all";
const BASIS_WEEKDAY = "weekday";

function parseCompare(value) {
  const [basis, n] = String(value || "").split(":");
  return { basis: basis === BASIS_WEEKDAY ? BASIS_WEEKDAY : BASIS_ALL, n: Number(n) || 0 };
}

const MODES = [
  { value: "messages", label: "Messages" },
  { value: "xp",      label: "XP" },
];

const DEFAULT_EXCLUDED_CHANNEL_NAMES = ["games", "cat-bot"];

const SOURCE_LABELS = {
  text:        "Messages",
  reply:       "Reply bonus",
  image_react: "Image reaction",
  voice:       "Voice",
  grant:       "Manual grant",
};
// XP-source series colors, drawn from the shared categorical palette so the
// panel reads as part of the same chart system as every other report.
// Sequential slots rather than the old 0,2,4,1,3, so that *by declaration* the
// palette's one weak pair (teal/orchid, ΔE 6.2 under deuteranopia) is two
// slots apart.
//
// That is a best effort and not a guarantee: the API returns the series ordered
// by magnitude, so which two segments actually share an edge depends on the
// data, and teal/orchid do end up adjacent on a typical week. What makes that
// legal is the 2px surface gap on every segment — the secondary encoding the
// 6-8 band requires — plus the legend and table. The gap is load-bearing, not
// decorative; do not remove it on the grounds that the colours look distinct.
const SOURCE_COLORS = {
  text:        ROLE_COLORS[0],
  reply:       ROLE_COLORS[1],
  image_react: ROLE_COLORS[2],
  voice:       ROLE_COLORS[3],
  grant:       ROLE_COLORS[4],
};
const FALLBACK_SOURCE_COLOR = ROLE_COLORS[5];

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Activity</h2>
        <div class="subtitle">Message or XP volume over time</div>
      </header>
      <div class="controls" style="align-items:flex-start;">
        <label>Resolution
          <select data-control="resolution">
            ${RESOLUTIONS.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}
          </select>
        </label>
        <label>Mode
          <select data-control="mode">
            ${MODES.map((m) => `<option value="${m.value}">${m.label}</option>`).join("")}
          </select>
        </label>
        <label data-field="compare" hidden>Compare to
          <select data-control="compare"></select>
        </label>
        <span class="ctrl-field">Member<span data-slot="user"></span></span>
        <span class="ctrl-field">Channel<span data-slot="channel"></span></span>
        <span class="ctrl-field">Exclude Channels<span data-slot="exclude"></span></span>
        <label style="flex-direction:row;align-items:center;gap:6px;">
          <input type="checkbox" data-control="include-bots" />
          Show Bots
        </label>
      </div>
      <div class="chart-caption" data-caption></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-legend></div>
      <div data-members hidden>
        <div class="section-label">Unique members</div>
        <div class="chart-wrap chart-wrap--short"><canvas data-members-chart></canvas></div>
      </div>
      <div data-chart-table></div>
      <div data-slider-wrap></div>
      <div class="card" style="margin-top:20px">
        <div data-poster="mod-stats"></div>
      </div>
    </div>
  `;

  const resEl  = container.querySelector('[data-control="resolution"]');
  const modeEl = container.querySelector('[data-control="mode"]');
  const includeBotsEl = container.querySelector('[data-control="include-bots"]');
  const compareEl = container.querySelector('[data-control="compare"]');
  const compareField = container.querySelector('[data-field="compare"]');

  // The same two overlays this page draws, as a self-refreshing panel in a
  // Discord channel. It sits here rather than on a config page because this is
  // where an admin is already looking at the charts it posts — and because its
  // only setting is the channel the control itself asks for.
  // Not awaited: the poster loads its own spec and channel list, and the report
  // above must not wait on that before drawing.
  mountPanelPoster(container.querySelector('[data-poster="mod-stats"]'), "mod-stats", {
    heading: "Post this to a mod channel",
    buttonLabel: "Post Panel",
  });

  // Rebuild the window picker for the current period, greying out the windows
  // this mode cannot reach. XP is capped by raw retention because the overlay
  // cannot read hour-of-day out of the daily rollup; messages read the whole
  // archive, so only the longest XP window is ever unavailable.
  function syncCompareControl() {
    const cfg = OVERLAY[resEl.value];
    compareField.hidden = !cfg;
    if (!cfg) return;

    const bases = [
      { key: BASIS_ALL, group: "Every day", spec: cfg },
      ...(cfg.weekday ? [{ key: BASIS_WEEKDAY, group: "Same weekday", spec: cfg.weekday }] : []),
    ];
    // Group headings only earn their place when there is a choice to make;
    // week_overlay has one basis, so its options stay a flat list.
    const grouped = bases.length > 1;
    const previous = parseCompare(compareEl.value);

    compareEl.replaceChildren();
    for (const { key, group, spec } of bases) {
      const cap = spec.caps[modeEl.value] ?? Infinity;
      const host = grouped ? document.createElement("optgroup") : compareEl;
      if (grouped) host.label = group;
      for (const n of spec.presets) {
        const opt = document.createElement("option");
        opt.value = `${key}:${n}`;
        opt.textContent = `Last ${n} ${spec.unit}`;
        if (n > cap) {
          opt.disabled = true;
          opt.textContent += " — messages only";
        }
        host.appendChild(opt);
      }
      if (grouped) compareEl.appendChild(host);
    }

    // Keep the reader's basis across a mode change, only pulling the window in
    // when this mode cannot reach that far. The value has to be one of the
    // options just built and not a disabled one: assigning a select a value it
    // has no option for silently blanks it, and a blank picker sitting over a
    // chart drawn to the server's default is the worst of both.
    const chosen = bases.find((b) => b.key === previous.basis) || bases[0];
    const cap = chosen.spec.caps[modeEl.value] ?? Infinity;
    const usable = chosen.spec.presets.filter((n) => n <= cap);
    const wanted = usable.includes(previous.n)
      ? previous.n
      : usable.includes(chosen.spec.fallback)
        ? chosen.spec.fallback
        : usable[usable.length - 1] ?? chosen.spec.presets[0];
    compareEl.value = `${chosen.key}:${wanted}`;
  }

  resEl.value  = initialParams.resolution || "day";
  modeEl.value = initialParams.mode || "xp";
  // Bots are excluded everywhere by default; this box opts back in.
  includeBotsEl.checked = initialParams.include_bots === "1";

  let chart = null;
  let membersChart = null;
  let slider = null;
  const sliderWrap = container.querySelector("[data-slider-wrap]");
  const captionEl  = container.querySelector("[data-caption]");
  const legendEl   = container.querySelector("[data-legend]");
  const membersWrap = container.querySelector("[data-members]");
  const tableEl    = container.querySelector("[data-chart-table]");
  // `search` reaches past the bounded page setOptions() is seeded with below,
  // so filtering the chart by a member who left long ago still works.
  const userFS = filterSelect("Type to filter…", [],
    { label: "Member", emptyLabel: "(all members)", search: memberSearch() });
  const chanFS = filterSelect("Type to filter…", [], { label: "Channel", emptyLabel: "(all channels)" });
  // Exclude Channels was a hand-rolled combobox: mousedown-to-pick plus a
  // blur timeout, no keyboard handling and no ARIA, so it was unusable without
  // a mouse — while the two filters beside it already used the shared widget.
  // multiFilterSelect is that widget's multi-value form: it brings the chips,
  // arrow-key navigation, Enter-to-pick, Escape, and the combobox roles, and it
  // announces a genuine value change as a bubbling `dk:change`.
  const excludeFS = multiFilterSelect("Add channel…", [], { label: "Exclude channels" });
  container.querySelector('[data-slot="user"]').appendChild(userFS.el);
  container.querySelector('[data-slot="channel"]').appendChild(chanFS.el);
  container.querySelector('[data-slot="exclude"]').appendChild(excludeFS.el);

  let allChannels = [];
  const excludedIds = () => excludeFS.getValues();

  async function loadDropdowns() {
    try {
      const [channels, members] = await Promise.all([
        api("/api/meta/channels"),
        api("/api/meta/members"),
      ]);

      allChannels = channels.map((ch) => ({ id: String(ch.id), name: ch.name }));

      const channelOpts = allChannels.map((ch) => ({
        id: ch.id,
        label: `#${ch.name}`,
      }));
      chanFS.setOptions(channelOpts);
      if (initialParams.channel_id) chanFS.setValue(initialParams.channel_id);

      const memberOpts = members.map((m) => ({
        id: m.id,
        label: m.display_name !== m.name ? `${m.display_name} (${m.name})` : m.name,
        left: !!m.left_server,
      })).sort((a, b) => a.left - b.left || a.label.localeCompare(b.label));
      memberOpts.forEach((o) => { if (o.left) o.label += " (left)"; });
      userFS.setOptions(memberOpts);
      if (initialParams.user_id) userFS.setValue(initialParams.user_id);

      excludeFS.setOptions(channelOpts);
      const initialExcluded = [];
      if (initialParams.exclude_channels === undefined) {
        for (const wanted of DEFAULT_EXCLUDED_CHANNEL_NAMES) {
          const match = allChannels.find((ch) => ch.name.toLowerCase() === wanted.toLowerCase());
          if (match) initialExcluded.push(match.id);
        }
      } else if (initialParams.exclude_channels) {
        for (const id of initialParams.exclude_channels.split(",").map((s) => s.trim()).filter(Boolean)) {
          initialExcluded.push(id);
        }
      }
      excludeFS.setValues(initialExcluded);
    } catch (_) {
      // Meta lookups are optional garnish here — the chart still renders
      // unfiltered if the member/channel lists don't load.
    }
    // Bound after the initial values are in place so restoring a deep link
    // doesn't count as a user change.
    onPickerChange(userFS, refresh);
    onPickerChange(chanFS, refresh);
    // The multi-picker fires its own change event (setValues above does not),
    // so it needs no focusout dance.
    excludeFS.el.addEventListener("dk:change", refresh);
  }

  async function refresh() {
    const params = {
      resolution: resEl.value,
      mode: modeEl.value,
    };
    // Deliberately not deep-linked: the window is a per-look question, not
    // part of the view's identity, and it defaults fresh every time.
    if (isOverlay(resEl.value)) {
      const { basis, n } = parseCompare(compareEl.value);
      params.compare_periods = n;
      if (basis === BASIS_WEEKDAY) params.same_weekday = "true";
    }
    if (userFS.getValue()) params.user_id = userFS.getValue();
    if (chanFS.getValue()) params.channel_id = chanFS.getValue();
    if (excludedIds().length) params.exclude_channel_ids = excludedIds().join(",");
    if (includeBotsEl.checked) params.include_bots = "true";

    const qs = new URLSearchParams();
    qs.set("resolution", resEl.value);
    qs.set("mode", modeEl.value);
    if (userFS.getValue()) qs.set("user_id", userFS.getValue());
    if (chanFS.getValue()) qs.set("channel_id", chanFS.getValue());
    qs.set("exclude_channels", excludedIds().join(","));
    qs.set("include_bots", includeBotsEl.checked ? "1" : "0");
    history.replaceState(null, "", `#/activity?${qs}`);

    const wrap = container.querySelector(".chart-wrap");
    try {
      const data = await withLoading(wrap, api("/api/reports/activity", params));
      if (chart) { chart.destroy(); chart = null; }
      if (slider) { slider.destroy(); slider = null; }

      // An overlay is worth drawing even when the current period is still
      // empty — at 00:30 on a Sunday the band IS the answer. It is only empty
      // when neither the period nor its history has anything in it.
      const overlay = isOverlay(data.resolution);
      const hasCurrent = data.counts.some((c) => c > 0);
      const hasBand = overlay && (data.band_mid || []).some((c) => c > 0);
      if (!data.labels.length || !(hasCurrent || hasBand)) {
        wrap.innerHTML = renderEmpty(
          `No ${data.mode} activity in this window. Try a wider resolution, clear the member or channel filter, or un-exclude a channel.`
        );
        sliderWrap.innerHTML = "";
        captionEl.textContent = "";
        legendEl.replaceChildren();
        tableEl.replaceChildren();
        membersWrap.hidden = true;
        return;
      }

      if (overlay) {
        renderOverlay(data, wrap);
        return;
      }

      function renderChart(lo, hi) {
        if (chart) chart.destroy();
        if (membersChart) { membersChart.destroy(); membersChart = null; }
        wrap.innerHTML = '<canvas data-chart></canvas>';
        const canvas = container.querySelector("[data-chart]");
        const sliced = {
          ...data,
          labels: data.labels.slice(lo, hi + 1),
          counts: data.counts.slice(lo, hi + 1),
          member_counts: (data.member_counts || []).slice(lo, hi + 1),
          series: (data.series || []).map((s) => ({
            source: s.source,
            counts: s.counts.slice(lo, hi + 1),
          })),
        };
        const title = `${data.y_label} — ${data.window_label} (${data.tz_label})`;
        const hasSeries = sliced.series.length > 0;
        const hasMembers = sliced.show_members && sliced.member_counts.length > 0;

        // The caption lives in HTML so it wears the page's type rather than
        // whatever the canvas was handed, and can be selected and read aloud.
        captionEl.textContent = title;

        chart = hasSeries
          ? _makeActivityChart(canvas, { ...sliced, hide_x_labels: hasMembers })
          : makeBarChart(canvas, { labels: sliced.labels, data: sliced.counts, title: "", yLabel: data.y_label });

        // A legend only earns its place with two or more series; with one, the
        // caption already names it.
        legendEl.replaceChildren();
        if (hasSeries) renderChartLegend(legendEl, chart);

        // Members get their own chart under the same x-axis instead of a second
        // y-scale over the bars.
        membersWrap.hidden = !hasMembers;
        if (hasMembers) {
          const mCanvas = container.querySelector("[data-members-chart]");
          membersChart = _makeMembersChart(mCanvas, sliced.labels, sliced.member_counts);
        }

        // Tooltips enhance; they must never be the only way to read a value —
        // and three of the palette's slots sit under 3:1 on this surface, which
        // obliges a table view.
        renderChartTable(tableEl, {
          labels: sliced.labels,
          datasets: [
            ...chart.data.datasets.map((d) => ({ label: d.label || data.y_label, data: d.data })),
            ...(hasMembers ? [{ label: "Unique members", data: sliced.member_counts }] : []),
          ],
          indexLabel: data.x_label || "Period",
        });
      }

      renderChart(0, data.labels.length - 1);
      sliderWrap.innerHTML = "";
      slider = mountTimeSlider(sliderWrap, {
        totalPoints: data.labels.length,
        labels: data.labels,
        onChange: renderChart,
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = renderError(
        `Couldn't load activity — ${err.message}. Change a control to try again.`
      );
      sliderWrap.innerHTML = "";
      captionEl.textContent = "";
      legendEl.replaceChildren();
      tableEl.replaceChildren();
      membersWrap.hidden = true;
    }
  }

  // The band view. No slider (the x-axis is a position inside a period, not a
  // timeline), no members sub-chart, no XP source breakdown — see
  // docs/plans/weekly-activity-comparison.md for why each is dropped.
  function renderOverlay(data, wrap) {
    if (membersChart) { membersChart.destroy(); membersChart = null; }
    membersWrap.hidden = true;
    sliderWrap.innerHTML = "";

    wrap.innerHTML = '<canvas data-chart></canvas>';
    const canvas = container.querySelector("[data-chart]");

    const isWeek = data.resolution === "week_overlay";
    const subject = isWeek ? "This week" : "Today";
    // The server names the band, because only it knows the guild-local weekday
    // a same-weekday comparison is drawn against ("Typical Tuesday").
    const typical = data.band_label || (isWeek ? "Typical week" : "Typical day");

    const hasBand = (data.band_mid || []).length > 0;
    const lived = data.counts.filter((c) => c !== null && c !== undefined).length;
    const sum = (arr) => arr.reduce((a, v) => a + (Number.isFinite(v) ? v : 0), 0);
    const currentTotal = sum(data.counts);
    // Like for like: a typical period measured to the SAME point, not its full
    // total. Comparing a Wednesday against a whole week is the misread this
    // chart exists to prevent, so the number beside the median is truncated
    // to the hours actually lived through.
    const typicalToDate = hasBand ? sum(data.band_mid.slice(0, lived)) : 0;

    captionEl.textContent = `${data.y_label} — ${data.window_label} (${data.tz_label})`;

    // A week is 168 hourly points on an axis that can label one tick a day, and
    // a single week is one realisation of it — drawn raw it reads as hash. The
    // server hands down a centred rolling mean of the current line and the
    // panel plots that instead. Only that line: the band is a per-hour
    // percentile over N weeks and so is smoothed across weeks already, and
    // blurring it as well would soften the envelope this week is read against.
    //
    // The cost, deliberately taken: averaging one side and not the other pulls
    // this week's spikes down toward a band that kept its own, so a single
    // roaring hour sits lower against the p75 than it truly was. That is why
    // the numbers stay raw — the totals beside the legend and every cell of the
    // table are built from `data.counts`, so the exact hour is one click away.
    const smoothWindow = (data.counts_smooth || []).length ? data.smooth_window || 1 : 1;
    const plotted = smoothWindow > 1 ? data.counts_smooth : data.counts;

    chart = makeOverlayChart(canvas, { ...data, counts: plotted }, {
      subject, typical, isWeek, currentTotal, typicalToDate,
      currentNote: smoothWindow > 1 ? `${smoothWindow}-hour average` : "",
    });

    legendEl.replaceChildren();
    renderChartLegend(legendEl, chart);

    renderChartTable(tableEl, {
      labels: data.labels,
      datasets: [
        // Raw, and said so when the line above is not: the table is where an
        // exact hour is read.
        {
          label: smoothWindow > 1 ? `${subject} so far (hourly)` : `${subject} so far`,
          data: data.counts,
        },
        ...(hasBand ? [
          { label: `${typical} (median)`, data: data.band_mid },
          { label: `${typical} (p25)`, data: data.band_low },
          { label: `${typical} (p75)`, data: data.band_high },
        ] : []),
      ],
      indexLabel: data.x_label || "Hour",
    });
  }

  for (const el of [resEl, modeEl, includeBotsEl]) {
    el.addEventListener("change", () => { syncCompareControl(); refresh(); });
  }
  compareEl.addEventListener("change", refresh);

  syncCompareControl();
  (async () => { await loadDropdowns(); await refresh(); })();

  return {
    unmount() {
      if (chart) { chart.destroy(); chart = null; }
      if (membersChart) { membersChart.destroy(); membersChart = null; }
      if (slider) { slider.destroy(); slider = null; }
    },
  };
}

/**
 * The XP/message bars. Deliberately ONE y-axis.
 *
 * "Unique Members" used to ride a second, right-hand scale over these bars.
 * Two independent scales means where the line sits relative to the bars is an
 * artefact of autoscaling, so the chart implied relationships — "members
 * tracked XP", "they diverged" — the data never claimed. It is its own chart
 * below now, sharing this one's x-axis and labels.
 */
function _makeActivityChart(canvas, data) {
  const ctx = canvas.getContext("2d");
  const hasSeries = Array.isArray(data.series) && data.series.length > 0;

  const datasets = [];
  if (hasSeries) {
    for (const s of data.series) {
      datasets.push({
        label: SOURCE_LABELS[s.source] || s.source,
        data: s.counts,
        backgroundColor: SOURCE_COLORS[s.source] || FALLBACK_SOURCE_COLOR,
        borderRadius: 2,
        // A 2px gap in the surface colour between stacked segments. The
        // palette's worst all-pairs CVD separation sits in the 6-8 band, which
        // is permitted ONLY with secondary encoding — this gap is that
        // encoding, so it is required, not decorative.
        borderColor: CHART_SURFACE,
        borderWidth: { top: 2 },
        borderSkipped: false,
        order: 2,
        yAxisID: "y",
        stack: "xp",
      });
    }
  } else {
    datasets.push({
      label: data.y_label,
      data: data.counts,
      backgroundColor: CHART_BAR,
      borderRadius: 3,
      order: 2,
      yAxisID: "y",
    });
  }
  const scales = {
    x: {
      stacked: hasSeries,
      // When the members chart is drawn underneath it repeats this exact axis,
      // so the labels are hidden here rather than printed twice — the two
      // charts share one x-axis, which is the whole point of splitting them.
      ticks: { color: CHART_TEXT, maxRotation: 45, display: !data.hide_x_labels },
      grid: { color: CHART_GRID },
    },
    y: {
      position: "left",
      stacked: hasSeries,
      title: { display: true, text: data.y_label, color: CHART_TEXT },
      ticks: { color: CHART_TEXT },
      grid: { color: CHART_GRID },
      beginAtZero: true,
    },
  };
  return new Chart(ctx, {
    type: "bar",
    data: { labels: data.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: hasSeries ? { mode: "index", intersect: false } : undefined,
      plugins: {
        // Both are drawn in HTML instead: canvas text cannot use the page's
        // type, is unselectable, and is invisible to a screen reader. The
        // caption is a sibling element; the legend comes from
        // renderChartLegend, which also puts each series' total beside it.
        title: { display: false },
        legend: { display: false },
        tooltip: hasSeries ? {
          callbacks: {
            footer: (items) => {
              const total = items.reduce((sum, it) => {
                const v = it.dataset.yAxisID === "y" ? it.parsed.y : 0;
                return sum + (Number.isFinite(v) ? v : 0);
              }, 0);
              return `Total XP: ${total.toFixed(1)}`;
            },
          },
        } : undefined,
      },
      scales,
    },
  });
}

/**
 * Unique members over the same window, as its own chart.
 *
 * Markers are 4px radius — an 8px mark, the smallest the guidance allows — with
 * a 12px hit radius so the hover target clears ~24px. They were 2px before,
 * which is a 4px dot you have to land on dead centre.
 */
function _makeMembersChart(canvas, labels, counts) {
  return new Chart(canvas.getContext("2d"), {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Unique members",
        data: counts,
        borderColor: CHART_ACCENT,
        backgroundColor: "transparent",
        borderWidth: 2,
        pointRadius: 4,
        pointHoverRadius: 6,
        pointHitRadius: 12,
        tension: 0.3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        // One series: the section label above the chart names it, so a legend
        // box would just repeat itself.
        title: { display: false },
        legend: { display: false },
      },
      scales: {
        x: { ticks: { color: CHART_TEXT, maxRotation: 45 }, grid: { color: CHART_GRID } },
        y: { ticks: { color: CHART_TEXT, precision: 0 }, grid: { color: CHART_GRID }, beginAtZero: true },
      },
    },
  });
}
