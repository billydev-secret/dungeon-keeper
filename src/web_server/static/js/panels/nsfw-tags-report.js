import { api } from "../api.js";
import { el } from "../audit-helpers.js";

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
      "height:8px; border-radius:4px; background:var(--accent, var(--gold-solid));" +
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
    );
  })();

  return {
    unmount() {
      cancelled = true;
    },
  };
}
