// Pools — the casino's daily prediction market.
//
// Lived on the Casino page until 2026-07-28. It moved because it is not a
// tenth table: it ships off, it wants its own channel (a round runs a full
// day), and its takeout is BURNED — where the jackpot cut one card below it
// was skimmed into a pot that re-mints it. Two percent knobs with opposite
// effects on the money supply, one card apart, was the footgun that earned
// this page.
//
// The four keys still live under `casino_*` in the config table and are still
// written by PUT /api/config/casino, whose body model is every-field-optional
// — so this panel sends four fields and nothing else is touched. Sharing that
// route (rather than adding /api/config/pools) also keeps the
// `casino_config_change` dispatch, which the cog listens on to move or tear
// down the market panel without a restart.
import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  field,
  numInput,
  checkbox,
  sectionCard,
  mountChannelPicker,
  guardForm,
  renderMetaWarning,
  mountAsync,
} from "../config-helpers.js";
import { renderLoading } from "../states.js";

export function mount(container) {
  container.textContent = "";
  const wrap = document.createElement("div");
  wrap.className = "panel";
  wrap.innerHTML = renderLoading("Loading config…");
  container.appendChild(wrap);

  return mountAsync(container, async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const c = config.casino || {};

    container.textContent = "";
    const panel = document.createElement("div");
    panel.className = "panel";

    const hdr = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Pools — Daily Prediction Market";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    // Both facts an admin needs before touching the knobs, since neither is
    // visible from the Casino page any more: the round is a day long, and the
    // takeout destroys currency rather than recycling it.
    sub.textContent =
      "One round a day, not an instant-settle table: members bet over or " +
      "under a line the bot sets on something the server did that day, and " +
      "the winning side splits the pool pro-rata. A different metric is " +
      "drawn each day. The house never wins or loses — it takes only the " +
      "takeout, which is burned. This is the casino's one deflationary sink.";
    hdr.append(h2, sub);
    panel.appendChild(hdr);

    const warning = renderMetaWarning();
    if (warning) {
      const w = document.createElement("div");
      w.innerHTML = warning;
      panel.appendChild(w.firstElementChild);
    }

    const form = document.createElement("form");
    form.className = "form form-cards";
    panel.appendChild(form);

    const card = (title) => sectionCard(form, title);

    // ── The master switch, and where the market lives ───────────────────
    const cardWiring = card("Running the Market");
    // Wrapping flex row (not fixed-width) so phones stack cleanly.
    const enabledRow = document.createElement("div");
    enabledRow.style.cssText = "display:flex; flex-wrap:wrap; gap:8px 16px;";
    enabledRow.append(
      checkbox("pools_enabled", c.pools_enabled === true, "Run a Daily Market"),
    );
    cardWiring.appendChild(field(
      "Pools", enabledRow,
      "Unlike the casino tables, Pools ships off. Unchecked, no round opens " +
        "and the market panel is removed.",
    ));

    const chanSlot = document.createElement("div");
    cardWiring.appendChild(field(
      "Pools Channel", chanSlot,
      "Where the market panel sits. The round lasts all day, so it wants " +
        "its own channel rather than sitting above the casino hub. " +
        "\"(use the casino channel)\" puts it with the games.",
    ));
    const chanPicker = mountChannelPicker(
      chanSlot, channels, String(c.pools_channel_id || "0"),
      {
        emptyValue: "0",
        emptyLabel: "(use the casino channel)",
        label: "Pools Channel",
      },
    );

    const cardRound = card("The Daily Round");
    cardRound.appendChild(field(
      "Betting Closes At (hour)",
      numInput("pools_close_hour", c.pools_close_hour ?? 18, 0, "1", 23),
      "Guild-local hour betting shuts, 0–23. The day still settles at " +
        "midnight. Late enough that most of the day is readable, early " +
        "enough that the evening is still unwritten — 18 is the default.",
    ));
    cardRound.appendChild(field(
      "Takeout (% of the pool)",
      numInput("pools_takeout_pct", c.pools_takeout_pct ?? 5, 0, "1", 50),
      "Taken off the pool before winners are paid, and burned — it is not " +
        "paid to the house and does not feed the jackpot. 5% leaves a 95% " +
        "return, the same band as the tables.",
    ));

    // ── The roster the daily draw picks from ────────────────────────────
    // The catalogue comes from the server (pools_metrics.SPECS) rather than
    // being listed here, so adding a metric in Python adds its checkbox.
    const cardMetrics = card("What the Market Bets On");
    const catalog = c.pools_metric_catalog || [];
    // Empty stored value means the whole roster — the same default an
    // untouched guild runs on, so the boxes start all-checked.
    const stored = String(c.pools_metrics || "").trim();
    const chosen = stored
      ? new Set(stored.split(",").map((s) => s.trim()).filter(Boolean))
      : new Set(catalog.map((m) => m.key));
    const metricRow = document.createElement("div");
    metricRow.style.cssText =
      "display:flex; flex-wrap:wrap; gap:8px 16px;";
    for (const m of catalog) {
      const box = checkbox(`metric_${m.key}`, chosen.has(m.key), m.label);
      if (m.cap_note) box.title = m.cap_note;
      metricRow.appendChild(box);
    }
    cardMetrics.appendChild(field(
      "Metrics In Rotation", metricRow,
      "One is drawn at random each day, never the same one two days " +
        "running. A metric sits out automatically until it has seven " +
        "clear days of history, so a newly-ticked box may take a week to " +
        "appear. Unticking every box falls back to the full roster — to " +
        "stop the market entirely, untick \"Run a Daily Market\" above.",
    ));

    const row = document.createElement("div");
    row.style.cssText = "display:flex; gap:8px; align-items:center;";
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save";
    const statusEl = document.createElement("span");
    row.append(saveBtn, statusEl);
    form.appendChild(row);

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const nums = {};
      for (const [name, label, min, max] of [
        ["pools_close_hour", "Betting Closes At", 0, 23],
        ["pools_takeout_pct", "Takeout", 0, 50],
      ]) {
        const raw = String(fd.get(name) ?? "").trim();
        const v = parseInt(raw, 10);
        if (raw === "" || !Number.isFinite(v) || v < min || v > max) {
          showStatus(
            statusEl, false,
            `${label} must be a whole number between ${min} and ${max}.`,
          );
          form.querySelector(`[name="${name}"]`).focus();
          return;
        }
        nums[name] = v;
      }
      // All boxes ticked is stored as "" — the roster is the default, and
      // recording it as an explicit list would silently freeze this page's
      // idea of the roster into config the day a metric is added.
      const picked = catalog.map((m) => m.key)
        .filter((key) => fd.has(`metric_${key}`));
      try {
        // Five fields only — see the header note on partial saves.
        await apiPut("/api/config/casino", {
          ...nums,
          pools_enabled: fd.has("pools_enabled"),
          pools_channel_id: chanPicker.getValue() || "0", // string — snowflake rule
          pools_metrics: picked.length === catalog.length ? "" : picked.join(","),
        });
        showStatus(statusEl, true);
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    container.appendChild(panel);
  }, { errorMsg: "Couldn’t load the reaction pool settings." });
}
