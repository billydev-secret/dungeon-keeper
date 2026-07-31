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
// distribution reads without an axis.
function bar(fraction) {
  return el("div", {
    style:
      "height:8px; border-radius:4px; background:var(--accent, #888);" +
      `width:${Math.max(2, Math.round(fraction * 100))}%;`,
  });
}

function labelTable(labels) {
  if (!labels.length) {
    return el("div", { className: "empty" }, "Nothing tagged yet.");
  }
  const most = Math.max(...labels.map((l) => l.count));
  const head = el(
    "thead",
    null,
    el(
      "tr",
      null,
      el("th", null, "Tag"),
      el("th", null, "Images"),
      el("th", null, "Avg verdict score"),
      el("th", null, ""),
    ),
  );
  const body = el(
    "tbody",
    null,
    ...labels.map((l) =>
      el(
        "tr",
        null,
        el("td", null, labelText(l.label)),
        el("td", null, String(l.count)),
        el("td", null, l.avg_score.toFixed(2)),
        el("td", { style: "width:40%;" }, bar(l.count / most)),
      ),
    ),
  );
  return el("table", { className: "data-table" }, head, body);
}

function scoreTable(scores) {
  if (!scores.length) {
    return el("div", { className: "empty" }, "Nothing scored yet.");
  }
  const most = Math.max(...scores.map((s) => s.count));
  const rows = scores.map((s) =>
    el(
      "tr",
      null,
      el("td", null, `${s.floor.toFixed(1)} – ${(s.floor + 0.1).toFixed(1)}`),
      el("td", null, String(s.count)),
      el("td", null, String(s.explicit)),
      el("td", { style: "width:40%;" }, bar(s.count / most)),
    ),
  );
  return el(
    "table",
    { className: "data-table" },
    el(
      "thead",
      null,
      el(
        "tr",
        null,
        el("th", null, "Score"),
        el("th", null, "Images"),
        el("th", null, "Judged explicit"),
        el("th", null, ""),
      ),
    ),
    el("tbody", null, ...rows),
  );
}

export function mount(container) {
  container.replaceChildren(
    el("div", { className: "panel" }, el("div", { className: "empty" }, "Loading…")),
  );

  (async () => {
    let data;
    try {
      data = await api("/api/moderation/nsfw-tags", { days: 30 });
    } catch (err) {
      container.replaceChildren(
        el("div", { className: "panel" }, el("div", { className: "error" }, err.message)),
      );
      return;
    }

    if (!data.classified) {
      container.replaceChildren(
        el(
          "div",
          { className: "panel" },
          el("header", null, el("h2", null, "Image Tags")),
          el(
            "div",
            { className: "empty" },
            "Nothing recorded yet. Images are only tagged in age-gated (NSFW-marked) channels.",
          ),
        ),
      );
      return;
    }

    container.replaceChildren(
      el(
        "div",
        { className: "panel" },
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
          labelTable(data.labels),
        ),
        el(
          "div",
          { className: "card" },
          el("div", { className: "section-label" }, "Confidence distribution"),
          scoreTable(data.scores),
          el(
            "div",
            { className: "field-hint" },
            "How the verdict engine scored what it saw. A clean split — most images " +
              "low, a few high — means the thresholds on Image Guard have room; a " +
              "crowded middle means they don't.",
          ),
        ),
      ),
    );
  })();
}
