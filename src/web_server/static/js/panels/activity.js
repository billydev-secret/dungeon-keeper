import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { makeBarChart, CHART_BAR, CHART_ACCENT, CHART_TEXT, CHART_GRID, ROLE_COLORS } from "../charts.js";
import { mountTimeSlider } from "../slider.js";
import { renderEmpty, renderError } from "../states.js";
import { filterSelect } from "../filter-select.js";

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

/** Fire `cb` when a shared filterSelect's value changes (it has no change
 *  event of its own — selection closes the popover, focus leaves after). */
function onPickerChange(fs, cb) {
  let last = fs.getValue();
  fs.el.addEventListener("focusout", () => {
    setTimeout(() => {
      const cur = fs.getValue();
      if (cur !== last) { last = cur; cb(); }
    }, 200);
  });
}

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
        <label>Exclude Channels
          <div class="filter-select" data-exclude-search>
            <input class="filter-select-input" data-exclude-input type="text" placeholder="Add channel…" autocomplete="off" />
            <div class="filter-select-list" data-exclude-list></div>
          </div>
        </label>
        <label style="flex-direction:row;align-items:center;gap:6px;">
          <input type="checkbox" data-control="exclude-bots" />
          Exclude Bots
        </label>
      </div>
      <div data-excluded-channels style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-slider-wrap></div>
    </div>
  `;

  const resEl  = container.querySelector('[data-control="resolution"]');
  const modeEl = container.querySelector('[data-control="mode"]');
  const excludeBotsEl = container.querySelector('[data-control="exclude-bots"]');
  const excludeInput = container.querySelector("[data-exclude-input]");
  const excludeList  = container.querySelector("[data-exclude-list]");
  const excludePills = container.querySelector("[data-excluded-channels]");

  resEl.value  = initialParams.resolution || "day";
  modeEl.value = initialParams.mode || "xp";
  excludeBotsEl.checked = initialParams.exclude_bots === undefined
    ? true
    : initialParams.exclude_bots === "1";

  let chart = null;
  let slider = null;
  const sliderWrap = container.querySelector("[data-slider-wrap]");
  const userFS = filterSelect("Type to filter…", [], { label: "Member", emptyLabel: "(all members)" });
  const chanFS = filterSelect("Type to filter…", [], { label: "Channel", emptyLabel: "(all channels)" });
  container.querySelector('[data-slot="user"]').appendChild(userFS.el);
  container.querySelector('[data-slot="channel"]').appendChild(chanFS.el);

  let allChannels = [];
  const excludedChannels = new Set();

  function renderExcludeDropdown(filter) {
    const q = filter.toLowerCase();
    const matches = allChannels
      .filter((ch) => !excludedChannels.has(ch.id) && (!q || ch.name.toLowerCase().includes(q)))
      .slice(0, 40);
    excludeList.innerHTML = matches
      .map((ch) => `<div class="filter-select-item" data-id="${esc(ch.id)}">#${esc(ch.name)}</div>`)
      .join("");
    excludeList.style.display = matches.length ? "block" : "none";
  }

  function renderExcludedPills() {
    excludePills.innerHTML = [...excludedChannels].map((id) => {
      const ch = allChannels.find((c) => c.id === id);
      const name = ch ? ch.name : id;
      return `<button class="role-pill" data-id="${esc(id)}" style="
        display:inline-flex;align-items:center;gap:4px;
        background:var(--bg-alt);border:1px solid var(--rule);border-radius:14px;
        padding:3px 10px;font-size:12px;color:var(--ink);cursor:pointer;
      ">#${esc(name)} <span style="color:var(--ink-dim);font-weight:700;">&times;</span></button>`;
    }).join("");
  }

  excludeInput.addEventListener("focus", () => renderExcludeDropdown(excludeInput.value));
  excludeInput.addEventListener("input", () => renderExcludeDropdown(excludeInput.value));
  excludeInput.addEventListener("blur", () => {
    setTimeout(() => { excludeList.style.display = "none"; }, 150);
  });
  excludeList.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".filter-select-item");
    if (!item) return;
    e.preventDefault();
    excludedChannels.add(item.dataset.id);
    excludeInput.value = "";
    excludeList.style.display = "none";
    renderExcludedPills();
    refresh();
  });
  excludePills.addEventListener("click", (e) => {
    const pill = e.target.closest(".role-pill");
    if (!pill) return;
    excludedChannels.delete(pill.dataset.id);
    renderExcludedPills();
    refresh();
  });

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

      if (initialParams.exclude_channels === undefined) {
        for (const wanted of DEFAULT_EXCLUDED_CHANNEL_NAMES) {
          const match = allChannels.find((ch) => ch.name.toLowerCase() === wanted.toLowerCase());
          if (match) excludedChannels.add(match.id);
        }
      } else if (initialParams.exclude_channels) {
        for (const id of initialParams.exclude_channels.split(",").map((s) => s.trim()).filter(Boolean)) {
          excludedChannels.add(id);
        }
      }
      renderExcludedPills();
    } catch (_) {
      // Meta lookups are optional garnish here — the chart still renders
      // unfiltered if the member/channel lists don't load.
    }
    // Bound after the initial values are in place so restoring a deep link
    // doesn't count as a user change.
    onPickerChange(userFS, refresh);
    onPickerChange(chanFS, refresh);
  }

  async function refresh() {
    const params = {
      resolution: resEl.value,
      mode: modeEl.value,
    };
    if (userFS.getValue()) params.user_id = userFS.getValue();
    if (chanFS.getValue()) params.channel_id = chanFS.getValue();
    if (excludedChannels.size) params.exclude_channel_ids = [...excludedChannels].join(",");
    if (excludeBotsEl.checked) params.exclude_bots = "true";

    const qs = new URLSearchParams();
    qs.set("resolution", resEl.value);
    qs.set("mode", modeEl.value);
    if (userFS.getValue()) qs.set("user_id", userFS.getValue());
    if (chanFS.getValue()) qs.set("channel_id", chanFS.getValue());
    qs.set("exclude_channels", [...excludedChannels].join(","));
    qs.set("exclude_bots", excludeBotsEl.checked ? "1" : "0");
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

  for (const el of [resEl, modeEl, excludeBotsEl]) el.addEventListener("change", refresh);

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
