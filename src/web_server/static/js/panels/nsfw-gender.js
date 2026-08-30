import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { viewerIsAdmin } from "../config-helpers.js";
import { makeStackedBarChart, makeLineChart, renderChartLegend, renderChartTable, seriesColor } from "../charts.js";
import { mountTimeSlider } from "../slider.js";

const RESOLUTIONS = [
  { value: "day",   label: "Daily (30d)" },
  { value: "week",  label: "Weekly (12wk)" },
  { value: "month", label: "Monthly (12mo)" },
];

// Two cuts of the same NSFW-channel activity, so they share a page rather
// than each getting one. Their scopes are close but not identical (the tag
// half also covers spoiler-required channels), and their permissions are not
// the same at all: the gender split reads
// `messages`, which every moderator can already see, while the tag mix is a
// body-part inventory of members' uploads and is admin-only wherever else it
// appears (/api/moderation/nsfw-tags). The option stays visible-but-locked for
// a moderator, matching how the nav renders an adminOnly entry — its existence
// isn't the secret, its contents are.
const BREAKDOWNS = [
  { value: "gender", label: "By gender" },
  { value: "tag",    label: "By tag", adminOnly: true },
];

const MODES = {
  gender: {
    heading:  "NSFW by Gender",
    subtitle: "NSFW channel activity split by gender tag",
    title:    "NSFW by Gender",
    endpoint: "/api/reports/nsfw-gender",
    empty:    "No NSFW posting recorded in this window. Widen the resolution, or clear the Media Only filter.",
    error:    "Couldn’t load NSFW activity by gender — try again.",
  },
  tag: {
    heading:  "NSFW by Tag",
    // The scope has to travel with the chart. An unqualified "tags over time"
    // reads as server-wide, and it never was: the tagger runs in age-gated
    // channels and in spoiler-required ones, and nowhere else.
    subtitle: "What the image tagger labelled, over time. Age-gated and spoiler-required channels only — the tagger never runs anywhere else.",
    title:    "NSFW by Tag",
    endpoint: "/api/reports/nsfw-tag-mix",
    empty:    "No tagged images in this window. The tagger only labels uploads in age-gated and spoiler-required channels — if those are quiet, this stays empty.",
    error:    "Couldn’t load NSFW activity by tag — try again.",
  },
};

export function mount(container, initialParams) {
  const isAdmin = viewerIsAdmin();

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2 data-heading>NSFW by Gender</h2>
        <div class="subtitle" data-subtitle></div>
      </header>
      <div class="controls">
        <label>Breakdown
          <select data-control="breakdown">
            ${BREAKDOWNS.map((b) => {
              const locked = b.adminOnly && !isAdmin;
              return `<option value="${b.value}"${locked ? " disabled" : ""}>${
                esc(locked ? `${b.label} (admins only)` : b.label)
              }</option>`;
            }).join("")}
          </select>
        </label>
        <label>Resolution
          <select data-control="resolution">
            ${RESOLUTIONS.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}
          </select>
        </label>
        <label>Display
          <select data-control="display">
            <option value="bar">Stacked Bar</option>
            <option value="line">Line Chart</option>
          </select>
        </label>
        <label>Channel
          <select data-control="channel"><option value="" data-all-option>All NSFW</option></select>
        </label>
        <label style="flex-direction:row; align-items:center; gap:6px;">
          <input type="checkbox" data-control="media_only" />
          Media Only
        </label>
      </div>
      <div class="chart-caption" data-caption></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-legend></div>
      <div data-chart-table></div>
      <div data-slider-wrap></div>
    </div>
  `;

  const breakEl   = container.querySelector('[data-control="breakdown"]');
  const resEl     = container.querySelector('[data-control="resolution"]');
  const dispEl    = container.querySelector('[data-control="display"]');
  const chanEl    = container.querySelector('[data-control="channel"]');
  const mediaEl   = container.querySelector('[data-control="media_only"]');
  const mediaLabel = mediaEl.closest("label");
  const allChannelsOpt = container.querySelector("[data-all-option]");
  const headingEl  = container.querySelector("[data-heading]");
  const subtitleEl = container.querySelector("[data-subtitle]");

  // A deep link to the tag view from a moderator's bookmark must not select an
  // option they cannot use — the request would 403 and the panel would render
  // an error where the gender chart used to be.
  const wanted = initialParams.breakdown === "tag" ? "tag" : "gender";
  breakEl.value = wanted === "tag" && !isAdmin ? "gender" : wanted;
  resEl.value  = initialParams.resolution || "week";
  dispEl.value = initialParams.display || "line";
  mediaEl.checked = initialParams.media_only !== undefined ? initialParams.media_only === "1" : true;

  let chart = null;
  let slider = null;
  // The heading, subtitle and Media Only state change synchronously while the
  // chart arrives from an await. Without a token, a slow tag request landing
  // after a cache-fast gender request paints body-part series under the "split
  // by gender tag" heading — a mislabelled chart, and these are not rows to be
  // careless about labelling.
  let reqSeq = 0;
  const sliderWrap = container.querySelector("[data-slider-wrap]");
  const captionEl  = container.querySelector("[data-caption]");
  const legendEl   = container.querySelector("[data-legend]");
  const tableEl    = container.querySelector("[data-chart-table]");

  async function loadChannels() {
    try {
      const channels = await api("/api/meta/channels");
      for (const ch of channels.filter((c) => c.nsfw)) {
        const opt = document.createElement("option");
        opt.value = ch.id;
        opt.textContent = ch.name;
        chanEl.appendChild(opt);
      }
      if (initialParams.channel_id) chanEl.value = initialParams.channel_id;
    } catch (err) {
      const opt = document.createElement("option");
      opt.disabled = true;
      opt.textContent = "Channel list failed to load — reload the page";
      chanEl.appendChild(opt);
    }
  }

  function clearChrome() {
    sliderWrap.innerHTML = "";
    captionEl.textContent = "";
    legendEl.replaceChildren();
    tableEl.replaceChildren();
  }

  async function refresh() {
    const seq = ++reqSeq;
    const breakdown = breakEl.value;
    const mode = MODES[breakdown];
    const isTag = breakdown === "tag";

    headingEl.textContent = mode.heading;
    subtitleEl.textContent = mode.subtitle;

    // Every row in nsfw_classifications IS an image, so the filter is already
    // implied. Leaving it live would imply a distinction that does not exist.
    mediaEl.disabled = isTag;
    mediaLabel.title = isTag ? "Every tagged row is an image, so this is always on." : "";

    // The dropdown lists Discord-NSFW channels, which is the whole scope of the
    // gender half but only part of the tag half's — the tagger also runs in
    // spoiler-required channels that Discord need not age-gate. Rather than
    // claim the unfiltered total is NSFW-only, say what it actually is; the
    // named channels below it remain a narrowing filter either way.
    allChannelsOpt.textContent = isTag ? "All tagged channels" : "All NSFW";

    const params = { resolution: resEl.value };
    if (!isTag) params.media_only = mediaEl.checked;
    if (chanEl.value) params.channel_id = chanEl.value;

    const qs = new URLSearchParams({
      breakdown,
      resolution: resEl.value,
      display: dispEl.value,
    });
    // Written even in tag mode, where it is not sent to the API: dropping it
    // would silently re-default the box to ticked on a reload, and the gender
    // numbers would come back different with no visible cause.
    qs.set("media_only", mediaEl.checked ? "1" : "0");
    if (chanEl.value) qs.set("channel_id", chanEl.value);
    history.replaceState(null, "", `#/nsfw-gender?${qs}`);

    const wrap = container.querySelector(".chart-wrap");
    try {
      const data = await withLoading(wrap, api(mode.endpoint, params));
      if (seq !== reqSeq) return;  // superseded — the chrome describes a later request
      if (chart) { chart.destroy(); chart = null; }
      if (slider) { slider.destroy(); slider = null; }
      if (!data.series.length) {
        wrap.innerHTML = `<div class="empty">${esc(mode.empty)}</div>`;
        clearChrome();
        return;
      }
      // One series shape for both endpoints. The tag colour comes from the
      // label's position in the tagger's vocabulary (`order`), NOT from its
      // index here: this array holds only the labels present in the window, so
      // enumerating it would repaint everything after a label that a narrower
      // resolution or a channel filter happened to drop.
      const series = isTag
        ? data.series.map((s) => ({
            label: s.display,
            counts: s.counts,
            color: seriesColor(s.order),
          }))
        : data.series;
      const title = `${mode.title} — ${data.window_label}`;
      function renderChart(lo, hi) {
        if (chart) chart.destroy();
        wrap.innerHTML = '<canvas data-chart></canvas>';
        const canvas = container.querySelector("[data-chart]");
        const slicedSeries = series.map((s) => ({ ...s, counts: s.counts.slice(lo, hi + 1) }));
        const slicedLabels = data.labels.slice(lo, hi + 1);
        if (dispEl.value === "line") {
          chart = makeLineChart(canvas, { labels: slicedLabels, series: slicedSeries.map((s) => ({ ...s, role: s.gender || s.label })), title });
        } else {
          chart = makeStackedBarChart(canvas, { labels: slicedLabels, series: slicedSeries, title });
        }

        // The caption lives in HTML so it wears the page's type and stays
        // selectable/screen-reader visible; canvas-drawn text was neither.
        captionEl.textContent = title;

        // "None for one": a lone series is already named by the caption, so a
        // legend would just repeat it. Both display modes are multi-dataset
        // charts (one dataset per gender or per tag), so renderChartLegend —
        // not the doughnut/pie form — is the right shape here.
        legendEl.replaceChildren();
        if (slicedSeries.length >= 2) renderChartLegend(legendEl, chart);

        // A tooltip must never be the only way to read a value. It also does
        // the real work for the rare tags: a label seen once in a month is an
        // invisible sliver on the stack but an exact number in the table.
        renderChartTable(tableEl, {
          labels: slicedLabels,
          datasets: chart.data.datasets.map((d) => ({ label: d.label, data: d.data })),
          indexLabel: { day: "Day", week: "Week", month: "Month" }[resEl.value] || "Period",
        });
      }
      renderChart(0, data.labels.length - 1);
      sliderWrap.innerHTML = "";
      slider = mountTimeSlider(sliderWrap, { totalPoints: data.labels.length, labels: data.labels, onChange: renderChart });
    } catch (err) {
      if (seq !== reqSeq) return;  // a superseded request's failure is not this view's
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(mode.error)} (${esc(err.message)})</div>`;
      clearChrome();
    }
  }

  for (const el of [breakEl, resEl, dispEl, chanEl, mediaEl]) el.addEventListener("change", refresh);

  (async () => { await loadChannels(); await refresh(); })();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } if (slider) { slider.destroy(); slider = null; } } };
}
