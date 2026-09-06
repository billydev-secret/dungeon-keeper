import { api, esc } from "../api.js";
import { el } from "../audit-helpers.js";
import { withLoading } from "../report-helpers.js";
import {
  makeStackedBarChart,
  makeLineChart,
  renderChartLegend,
  renderChartTable,
  seriesColor,
} from "../charts.js";
import { mountTimeSlider } from "../slider.js";

const RESOLUTIONS = [
  { value: "day", label: "Daily (30d)" },
  { value: "week", label: "Weekly (12wk)" },
  { value: "month", label: "Monthly (12mo)" },
];

function labelText(label) {
  return label.replace(/_/g, " ").toLowerCase();
}

function stat(value, caption) {
  return el(
    "div",
    { style: "min-width:120px;" },
    el("div", { style: "font-size:22px; font-weight:600;" }, String(value)),
    el("div", { className: "field-hint", style: "margin:0;" }, caption),
  );
}

// A horizontal bar sized as a share of the largest row, so the shape of the
// distribution reads without an axis. --accent is injected per guild by the
// branding system; --gold-solid is the palette default, so an unbranded
// dashboard matches the rest of the UI and a palette retune reaches this.
// Same default as miniBarHTML in tiles/tile-helpers.js.
function bar(fraction) {
  return el("div", {
    style:
      "height:8px; border-radius:4px; background:var(--gold-solid);" +
      `width:${Math.max(2, Math.round(fraction * 100))}%;`,
  });
}

// Both distributions on this panel are the same table: labelled columns plus a
// proportion bar scaled to the largest count. They were written out twice and
// had already drifted (one hoisted its thead, the other inlined it), so a
// styling fix to the bar column had to be made in two places.
function distributionTable(rows, { headers, cells, empty }) {
  if (!rows.length) {
    return el("div", { className: "empty" }, empty);
  }
  const most = Math.max(...rows.map((r) => r.count));
  return el(
    "table",
    { className: "data-table" },
    el(
      "thead",
      null,
      el("tr", null, ...headers.map((h) => el("th", null, h)), el("th", null, "")),
    ),
    el(
      "tbody",
      null,
      ...rows.map((r) =>
        el(
          "tr",
          null,
          ...cells(r).map((c) => el("td", null, c)),
          el("td", { style: "width:40%;" }, bar(r.count / most)),
        ),
      ),
    ),
  );
}


// The tag mix over time. This arrived here in 2026-09 when the NSFW-by-Gender
// panel was removed with the gender store: the chart was the tag half of that
// panel's Breakdown select, and it belongs with the rest of the tag reporting
// rather than disappearing alongside data it was never about. Moving it here
// also settles its permissions — it was the admin-only option on a
// moderator-visible page, and this whole panel is admin-only.
function overTimeCard(onTeardown) {
  const wrap = el("div", { className: "chart-wrap" });
  const caption = el("div", { className: "chart-caption" });
  const legend = el("div");
  const table = el("div");
  const sliderWrap = el("div");

  const resSel = el(
    "select",
    { "data-control": "resolution" },
    ...RESOLUTIONS.map((r) => el("option", { value: r.value }, r.label)),
  );
  resSel.value = "week";
  const dispSel = el(
    "select",
    { "data-control": "display" },
    el("option", { value: "line" }, "Line Chart"),
    el("option", { value: "bar" }, "Stacked Bar"),
  );
  // The unfiltered total spans spoiler-required channels this dropdown cannot
  // name — the tagger runs there too — so the "all" option must not call itself
  // NSFW-only. The named entries below it remain a narrowing filter.
  const chanSel = el(
    "select",
    { "data-control": "channel" },
    el("option", { value: "", "data-all-option": "" }, "All tagged channels"),
  );

  async function loadChannels() {
    try {
      const channels = await api("/api/meta/channels");
      for (const ch of channels.filter((c) => c.nsfw)) {
        chanSel.appendChild(el("option", { value: ch.id }, ch.name));
      }
    } catch {
      const opt = el("option", null, "Channel list failed to load — reload the page");
      opt.disabled = true;
      chanSel.appendChild(opt);
    }
  }

  let chart = null;
  let slider = null;
  // The heading and controls change synchronously while the data arrives from
  // an await, so a slow request landing after a fast one would paint the wrong
  // window under the current caption. These are not rows to be careless about
  // labelling.
  let seq = 0;

  function clearChrome() {
    sliderWrap.replaceChildren();
    caption.textContent = "";
    legend.replaceChildren();
    table.replaceChildren();
  }

  async function refresh() {
    const mine = ++seq;
    try {
      const data = await withLoading(
        wrap,
        api("/api/reports/nsfw-tag-mix", {
          resolution: resSel.value,
          ...(chanSel.value ? { channel_id: chanSel.value } : {}),
        }),
      );
      if (mine !== seq) return; // superseded — the chrome describes a later request
      if (chart) { chart.destroy(); chart = null; }
      if (slider) { slider.destroy(); slider = null; }
      if (!data.series.length) {
        wrap.replaceChildren(
          el(
            "div",
            { className: "empty" },
            "No tagged images in this window. The tagger only labels uploads in " +
              "age-gated and spoiler-required channels — if those are quiet, this " +
              "stays empty.",
          ),
        );
        clearChrome();
        return;
      }

      // The colour comes from the label's position in the tagger's vocabulary
      // (`order`), NOT from its index in this array: this holds only the labels
      // present in the window, so enumerating it would repaint everything after
      // a label a narrower resolution happened to drop.
      const series = data.series.map((sr) => ({
        label: sr.display,
        counts: sr.counts,
        color: seriesColor(sr.order),
      }));
      const title = `Tagged images — ${data.window_label}`;

      function renderChart(lo, hi) {
        if (chart) chart.destroy();
        const canvas = el("canvas");
        wrap.replaceChildren(canvas);
        const sliced = series.map((sr) => ({ ...sr, counts: sr.counts.slice(lo, hi + 1) }));
        const labels = data.labels.slice(lo, hi + 1);
        if (dispSel.value === "line") {
          chart = makeLineChart(canvas, {
            labels,
            series: sliced.map((sr) => ({ ...sr, role: sr.label })),
            title,
          });
        } else {
          chart = makeStackedBarChart(canvas, { labels, series: sliced, title });
        }

        // The caption lives in HTML so it wears the page's type and stays
        // selectable and screen-reader visible; canvas-drawn text was neither.
        caption.textContent = title;

        // "None for one": a lone series is already named by the caption.
        legend.replaceChildren();
        if (sliced.length >= 2) renderChartLegend(legend, chart);

        // A tooltip must never be the only way to read a value — and a tag seen
        // once in a month is an invisible sliver on the stack but an exact
        // number in the table.
        renderChartTable(table, {
          labels,
          datasets: chart.data.datasets.map((d) => ({ label: d.label, data: d.data })),
          indexLabel: { day: "Day", week: "Week", month: "Month" }[resSel.value] || "Period",
        });
      }

      renderChart(0, data.labels.length - 1);
      sliderWrap.replaceChildren();
      slider = mountTimeSlider(sliderWrap, {
        totalPoints: data.labels.length,
        labels: data.labels,
        onChange: renderChart,
      });
    } catch (err) {
      if (mine !== seq) return; // a superseded request's failure is not this view's
      wrap.replaceChildren(
        el(
          "div",
          { className: "error" },
          `Couldn\u2019t load tagging over time — try again. (${esc(err.message)})`,
        ),
      );
      clearChrome();
    }
  }

  for (const control of [resSel, dispSel, chanSel]) {
    control.addEventListener("change", refresh);
  }
  onTeardown(() => {
    if (chart) { chart.destroy(); chart = null; }
    if (slider) { slider.destroy(); slider = null; }
  });
  (async () => { await loadChannels(); await refresh(); })();

  return el(
    "div",
    { className: "card" },
    el("div", { className: "section-label" }, "Tagging over time"),
    el(
      "div",
      { className: "field-hint" },
      "What the tagger labelled, charted over time. Age-gated and " +
        "spoiler-required channels only — the tagger never runs anywhere else.",
    ),
    el(
      "div",
      { className: "controls" },
      el("label", null, "Resolution", resSel),
      el("label", null, "Display", dispSel),
      el("label", null, "Channel", chanSel),
    ),
    caption,
    wrap,
    legend,
    table,
    sliderWrap,
  );
}

export function mount(container) {
  // The panel shell is built once, synchronously, and only `body` is replaced
  // when data lands. app.js prepends its help/related bar to `container` AFTER
  // mount() returns, so a panel that replaceChildren()s the container later
  // deletes that bar — and with it this page's "Related: Image Guard" link.
  const body = el("div", null, el("div", { className: "empty" }, "Loading…"));
  const panel = el("div", { className: "panel" }, body);
  container.replaceChildren(panel);

  // Navigating away mid-fetch would otherwise let this resolve and paint the
  // tags report over whichever panel replaced it.
  let cancelled = false;
  const teardowns = [];
  const onTeardown = (fn) => teardowns.push(fn);

  (async () => {
    let data;
    try {
      data = await api("/api/moderation/nsfw-tags", { days: 30 });
    } catch (err) {
      if (cancelled) return;
      body.replaceChildren(el("div", { className: "error" }, err.message));
      return;
    }
    if (cancelled) return;

    if (!data.classified) {
      body.replaceChildren(
        el("header", null, el("h2", null, "Image Tags")),
        el(
          "div",
          { className: "empty" },
          "Nothing recorded yet. Images are only tagged in age-gated (NSFW-marked) channels.",
        ),
      );
      return;
    }

    body.replaceChildren(
      el(
        "header",
        null,
        el("h2", null, "Image Tags"),
        el(
          "div",
          { className: "subtitle" },
          `What was detected in age-gated channels over the last ${data.days} days`,
        ),
      ),
      el(
        "div",
        { className: "card" },
        el(
          "div",
          {
            style:
              "display:flex; flex-wrap:wrap; gap:24px 32px; margin-bottom:8px;",
          },
          stat(data.classified, "images checked"),
          stat(data.explicit, "judged explicit"),
          stat(data.tagged, "carried a tag"),
          stat(`${data.avg_inference_ms}ms`, "average per image"),
        ),
        el(
          "div",
          { className: "field-hint" },
          "Only age-gated channels are tagged and recorded — checks elsewhere leave " +
            "no trace here. Removals in any channel are on the Blocked Images report.",
        ),
      ),
      el(
        "div",
        { className: "card" },
        el("div", { className: "section-label" }, "Where the two models disagree"),
        el(
          "div",
          { style: "display:flex; flex-wrap:wrap; gap:24px 32px;" },
          stat(data.explicit_untagged, "explicit, nothing tagged"),
          stat(data.tagged_not_explicit, "tagged, judged not explicit"),
        ),
        el(
          "div",
          { className: "field-hint" },
          "The verdict comes from a whole-image model; the tags come from a " +
            "body-part detector. The first number is content the tagger cannot " +
            "see — the blind spot that caused the switch. The second is where the " +
            "tagger found exposed nudity the verdict let through, and is worth a " +
            "look if it grows.",
        ),
      ),
      el(
        "div",
        { className: "card" },
        el("div", { className: "section-label" }, "Most common tags"),
        distributionTable(data.labels, {
          headers: ["Tag", "Images", "Avg verdict score"],
          cells: (l) => [labelText(l.label), String(l.count), l.avg_score.toFixed(2)],
          empty: "Nothing tagged yet.",
        }),
      ),
      el(
        "div",
        { className: "card" },
        el("div", { className: "section-label" }, "Confidence distribution"),
        distributionTable(data.scores, {
          headers: ["Score", "Images", "Judged explicit"],
          cells: (s) => [
            `${s.floor.toFixed(1)} – ${(s.floor + 0.1).toFixed(1)}`,
            String(s.count),
            String(s.explicit),
          ],
          empty: "Nothing scored yet.",
        }),
        el(
          "div",
          { className: "field-hint" },
          "How the verdict engine scored what it saw. A clean split — most images " +
            "low, a few high — means the thresholds on Image Guard have room; a " +
            "crowded middle means they don't.",
        ),
      ),
      overTimeCard(onTeardown),
    );
  })();

  return {
    unmount() {
      cancelled = true;
      for (const fn of teardowns) fn();
    },
  };
}
