import { api, esc } from "../api.js";
import { makeBarChart } from "../charts.js";
import { mountTimeSlider } from "../slider.js";

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

function filterSelect(placeholder, options) {
  const wrap = document.createElement("div");
  wrap.className = "filter-select";
  const input = document.createElement("input");
  input.type = "text"; input.placeholder = placeholder;
  input.className = "filter-select-input";
  wrap.appendChild(input);
  const list = document.createElement("div");
  list.className = "filter-select-list";
  wrap.appendChild(list);
  let selectedId = "";

  function escHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

  function render(filter) {
    const lc = filter.toLowerCase();
    const matches = lc ? options.filter((o) => o.label.toLowerCase().includes(lc)) : options;
    list.innerHTML = `<div class="filter-select-item" data-id=""><em style="color:var(--text-dim)">(all)</em></div>` +
      (lc ? matches : matches.slice(0, 300)).map((o) => `<div class="filter-select-item" data-id="${escHtml(o.id)}">${escHtml(o.label)}</div>`).join("");
  }

  input.addEventListener("focus", () => { render(input.value); list.style.display = "block"; });
  input.addEventListener("input", () => { selectedId = ""; render(input.value); list.style.display = "block"; });
  list.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".filter-select-item");
    if (!item) return;
    selectedId = item.dataset.id;
    input.value = selectedId ? item.textContent : "";
    list.style.display = "none";
    wrap.dispatchEvent(new Event("change"));
  });
  input.addEventListener("blur", () => { setTimeout(() => { list.style.display = "none"; }, 150); });
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") { list.style.display = "none"; input.blur(); } });

  return { el: wrap, getValue: () => selectedId };
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
        <label>User<span data-slot="user"></span></label>
        <label>Channel<span data-slot="channel"></span></label>
        <label>Exclude Channels
          <div class="filter-select" data-exclude-search>
            <input class="filter-select-input" data-exclude-input type="text" placeholder="Add channel…" autocomplete="off" />
            <div class="filter-select-list" data-exclude-list></div>
          </div>
        </label>
        <label style="flex-direction:row;align-items:center;gap:6px;">
          <input type="checkbox" data-control="exclude-bots" />
          Exclude bots
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
  let userFS = filterSelect("Loading…", []);
  let chanFS = filterSelect("Loading…", []);
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
        background:var(--bg-alt);border:1px solid var(--grid);border-radius:14px;
        padding:3px 10px;font-size:12px;color:var(--text);cursor:pointer;
      ">#${esc(name)} <span style="color:var(--text-dim);font-weight:700;">&times;</span></button>`;
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
      const newChanFS = filterSelect("Type to filter…", channelOpts);
      chanFS.el.replaceWith(newChanFS.el);
      chanFS = newChanFS;
      chanFS.el.addEventListener("change", refresh);

      const memberOpts = members.map((m) => ({
        id: m.id,
        label: m.display_name !== m.name ? `${m.display_name} (${m.name})` : m.name,
        left: !!m.left_server,
      })).sort((a, b) => a.left - b.left || a.label.localeCompare(b.label));
      memberOpts.forEach((o) => { if (o.left) o.label += " (left)"; });
      const newFS = filterSelect("Type to filter…", memberOpts);
      userFS.el.replaceWith(newFS.el);
      userFS = newFS;
      userFS.el.addEventListener("change", refresh);

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
    } catch (_) {}
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

    try {
      const data = await api("/api/reports/activity", params);
      if (chart) { chart.destroy(); chart = null; }
      if (slider) { slider.destroy(); slider = null; }

      const wrap = container.querySelector(".chart-wrap");
      if (!data.labels.length || !data.counts.some((c) => c > 0)) {
        wrap.innerHTML = `<div class="empty">No ${data.mode} activity for this period.</div>`;
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
          member_counts: data.member_counts.slice(lo, hi + 1),
        };
        const title = `${data.y_label} — ${data.window_label} (${data.tz_label})`;
        if (sliced.show_members && sliced.member_counts.length) {
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
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(err.message)}</div>`;
      sliderWrap.innerHTML = "";
    }
  }

  for (const el of [resEl, modeEl, excludeBotsEl]) el.addEventListener("change", refresh);

  (async () => { await loadDropdowns(); await refresh(); })();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } if (slider) { slider.destroy(); slider = null; } } };
}

function _makeActivityChart(canvas, data, title) {
  const ctx = canvas.getContext("2d");
  return new Chart(ctx, {
    type: "bar",
    data: {
      labels: data.labels,
      datasets: [
        {
          label: data.y_label,
          data: data.counts,
          backgroundColor: "#E6B84C",
          borderRadius: 3,
          order: 2,
          yAxisID: "y",
        },
        {
          label: "Unique Members",
          data: data.member_counts,
          type: "line",
          borderColor: "#B36A92",
          backgroundColor: "transparent",
          borderWidth: 2,
          pointRadius: 2,
          tension: 0.3,
          order: 1,
          yAxisID: "y1",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        title: { display: true, text: title, color: "#dbdee1" },
        legend: { labels: { color: "#dbdee1" } },
      },
      scales: {
        x: {
          ticks: { color: "#dbdee1", maxRotation: 45 },
          grid: { color: "#3f4147" },
        },
        y: {
          position: "left",
          title: { display: true, text: data.y_label, color: "#dbdee1" },
          ticks: { color: "#dbdee1" },
          grid: { color: "#3f4147" },
          beginAtZero: true,
        },
        y1: {
          position: "right",
          title: { display: true, text: "Unique Members", color: "#B36A92" },
          ticks: { color: "#B36A92" },
          grid: { drawOnChartArea: false },
          beginAtZero: true,
        },
      },
    },
  });
}
