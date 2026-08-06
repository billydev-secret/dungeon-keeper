import { api } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { makeBarChart, CHART_BAR, CHART_ACCENT, CHART_TEXT, CHART_GRID, ROLE_COLORS } from "../charts.js";
import { mountTimeSlider } from "../slider.js";
import { renderEmpty, renderError } from "../states.js";
import { filterSelect, multiFilterSelect } from "../filter-select.js";
import { onPickerChange } from "../config-helpers.js";

const RESOLUTIONS = [
  { value: "hour",        label: "Hourly (24h)" },
  { value: "day",         label: "Daily (30d)" },
  { value: "week",        label: "Weekly (12wk)" },
  { value: "month",       label: "Monthly (12mo)" },
  { value: "hour_of_day", label: "By Hour of Day" },
  { value: "day_of_week", label: "By Day of Week" },
];

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
const SOURCE_COLORS = {
  text:        ROLE_COLORS[0],
  reply:       ROLE_COLORS[2],
  image_react: ROLE_COLORS[4],
  voice:       ROLE_COLORS[1],
  grant:       ROLE_COLORS[3],
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
        <span class="ctrl-field">Member<span data-slot="user"></span></span>
        <span class="ctrl-field">Channel<span data-slot="channel"></span></span>
        <span class="ctrl-field">Exclude Channels<span data-slot="exclude"></span></span>
        <label style="flex-direction:row;align-items:center;gap:6px;">
          <input type="checkbox" data-control="include-bots" />
          Show Bots
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-slider-wrap></div>
    </div>
  `;

  const resEl  = container.querySelector('[data-control="resolution"]');
  const modeEl = container.querySelector('[data-control="mode"]');
  const includeBotsEl = container.querySelector('[data-control="include-bots"]');

  resEl.value  = initialParams.resolution || "day";
  modeEl.value = initialParams.mode || "xp";
  // Bots are excluded everywhere by default; this box opts back in.
  includeBotsEl.checked = initialParams.include_bots === "1";

  let chart = null;
  let slider = null;
  const sliderWrap = container.querySelector("[data-slider-wrap]");
  const userFS = filterSelect("Type to filter…", [], { label: "Member", emptyLabel: "(all members)" });
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

      if (!data.labels.length || !data.counts.some((c) => c > 0)) {
        wrap.innerHTML = renderEmpty(
          `No ${data.mode} activity in this window. Try a wider resolution, clear the member or channel filter, or un-exclude a channel.`
        );
        sliderWrap.innerHTML = "";
        return;
      }

      function renderChart(lo, hi) {
        if (chart) chart.destroy();
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
        if (hasSeries || hasMembers) {
          chart = _makeActivityChart(canvas, sliced, title);
        } else {
          chart = makeBarChart(canvas, { labels: sliced.labels, data: sliced.counts, title, yLabel: data.y_label });
        }
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
    }
  }

  for (const el of [resEl, modeEl, includeBotsEl]) el.addEventListener("change", refresh);

  (async () => { await loadDropdowns(); await refresh(); })();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } if (slider) { slider.destroy(); slider = null; } } };
}

function _makeActivityChart(canvas, data, title) {
  const ctx = canvas.getContext("2d");
  const hasSeries = Array.isArray(data.series) && data.series.length > 0;
  const hasMembers = data.show_members && Array.isArray(data.member_counts) && data.member_counts.length > 0;

  const datasets = [];
  if (hasSeries) {
    for (const s of data.series) {
      datasets.push({
        label: SOURCE_LABELS[s.source] || s.source,
        data: s.counts,
        backgroundColor: SOURCE_COLORS[s.source] || FALLBACK_SOURCE_COLOR,
        borderRadius: 2,
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
  if (hasMembers) {
    datasets.push({
      label: "Unique Members",
      data: data.member_counts,
      type: "line",
      borderColor: CHART_ACCENT,
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 2,
      tension: 0.3,
      order: 1,
      yAxisID: "y1",
    });
  }

  const scales = {
    x: {
      stacked: hasSeries,
      ticks: { color: CHART_TEXT, maxRotation: 45 },
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
  if (hasMembers) {
    scales.y1 = {
      position: "right",
      title: { display: true, text: "Unique Members", color: CHART_ACCENT },
      ticks: { color: CHART_ACCENT },
      grid: { drawOnChartArea: false },
      beginAtZero: true,
    };
  }

  return new Chart(ctx, {
    type: "bar",
    data: { labels: data.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: hasSeries ? { mode: "index", intersect: false } : undefined,
      plugins: {
        title: { display: true, text: title, color: CHART_TEXT },
        legend: { labels: { color: CHART_TEXT } },
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
