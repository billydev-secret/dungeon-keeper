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
  mountChannelPicker,
  guardForm,
  renderMetaWarning,
} from "../config-helpers.js";

export function mount(container) {
  container.textContent = "";
  const wrap = document.createElement("div");
  wrap.className = "panel";
  const loading = document.createElement("div");
  loading.className = "empty";
  loading.textContent = "Loading config…";
  wrap.appendChild(loading);
  container.appendChild(wrap);

  (async () => {
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
      "under a line the bot sets on how much the economy grows that day, and " +
      "the winning side splits the pool pro-rata. The house never wins or " +
      "loses — it takes only the takeout, which is burned. This is the " +
      "casino's one deflationary sink.";
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

    const card = (title) => {
      const el = document.createElement("div");
      el.className = "card";
      const lbl = document.createElement("div");
      lbl.className = "section-label";
      lbl.textContent = title;
      el.appendChild(lbl);
      form.appendChild(el);
      return el;
    };

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
      try {
        // A partial save: CasinoConfigUpdate is every-field-optional and the
        // route drops unset keys, so the nine tables and the jackpot are left
        // exactly as the Casino page left them.
        await apiPut("/api/config/casino", {
          ...nums,
          pools_enabled: fd.has("pools_enabled"),
          pools_channel_id: chanPicker.getValue() || "0", // string — snowflake rule
        });
        showStatus(statusEl, true);
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    container.appendChild(panel);
  })();
}
