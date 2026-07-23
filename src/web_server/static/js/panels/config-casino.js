import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  buildField,
  mountChannelPicker,
  guardForm,
  renderMetaWarning,
} from "../config-helpers.js";

let _fieldSeq = 0;

// buildField renders a bare <label>; pair it with its control by id so screen
// readers announce the label and a label tap focuses the input (W-A7).
function field(labelText, control, hint) {
  const div = buildField(labelText, control, hint);
  if (control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) {
    const id = control.id || `cc-field-${++_fieldSeq}`;
    control.id = id;
    div.querySelector("label").htmlFor = id;
  }
  return div;
}

function numInput(name, value, min, step = "1", max = null) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.required = true;
  inp.min = String(min);
  if (max != null) inp.max = String(max);
  inp.step = step;
  inp.value = String(value);
  inp.style.maxWidth = "160px";
  return inp;
}

function checkbox(name, checked, labelText) {
  const label = document.createElement("label");
  label.style.cssText = "display:flex; gap:6px; align-items:center;";
  const inp = document.createElement("input");
  inp.type = "checkbox";
  inp.name = name;
  inp.checked = !!checked;
  label.append(inp, document.createTextNode(" " + labelText));
  return label;
}

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
    // Name is edited on the Branding panel; this panel just wears it.
    const br = config.branding || {};
    const casinoName =
      (br.casino_name || "").trim() || br.default_casino_name || "Golden Meadow";

    container.textContent = "";
    const panel = document.createElement("div");
    panel.className = "panel";

    const hdr = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = `The ${casinoName} Casino`;
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent =
      "House gambling games played for your server currency. Picking a channel " +
      "opens the casino; the bot keeps its hub panel there. Rename it on the " +
      "Branding panel.";
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

    // ── Where the casino lives — this channel is the master switch. ──────
    const cardWiring = card("Casino Channel");
    const chanSlot = document.createElement("span");
    cardWiring.appendChild(field(
      "Casino Channel",
      chanSlot,
      "Where the hub panel sits and every game is played. Choosing " +
        "\"(disabled)\" closes the casino and removes the panel — members can " +
        "no longer place any bet.",
    ));
    const chanPicker = mountChannelPicker(
      chanSlot, channels, String(c.channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(disabled)", label: "Casino Channel" },
    );

    const cardStakes = card("Betting Limits");
    cardStakes.appendChild(field(
      "Minimum Bet", numInput("min_bet", c.min_bet ?? 5, 1),
      "The smallest stake any table accepts. Bets below this are refused.",
    ));
    cardStakes.appendChild(field(
      "Maximum Bet", numInput("max_bet", c.max_bet ?? 100, 0),
      "The largest stake allowed on a single play. Enter 0 for no per-bet " +
        "ceiling — the daily cap below still applies.",
    ));
    cardStakes.appendChild(field(
      "Daily Wager Cap", numInput("daily_wager_cap", c.daily_wager_cap ?? 500, 0),
      "The most one member can stake in a single server day, counting every " +
        "table together. Enter 0 for no cap. This is your main lever on how " +
        "fast the casino creates or destroys currency.",
    ));

    // Wrapping flex row (not fixed-width) so phones stack the toggles.
    const cardTables = card("Games");
    const tables = document.createElement("div");
    tables.style.cssText = "display:flex; flex-wrap:wrap; gap:8px 16px;";
    tables.append(
      checkbox("coinflip_enabled", c.coinflip_enabled !== false, "Coinflip"),
      checkbox("slots_enabled", c.slots_enabled !== false, "Slots"),
      checkbox("blackjack_enabled", c.blackjack_enabled !== false, "Blackjack"),
      checkbox("roulette_enabled", c.roulette_enabled !== false, "Roulette"),
    );
    cardTables.appendChild(field(
      "Open Tables", tables,
      "Unchecked games refuse all bets and disappear from the hub panel.",
    ));

    const cardJackpot = card("Progressive Jackpot");
    const jackpotRow = document.createElement("div");
    jackpotRow.style.cssText = "display:flex; flex-wrap:wrap; gap:8px 16px;";
    jackpotRow.append(
      checkbox("jackpot_enabled", c.jackpot_enabled !== false, "Run a Progressive Jackpot"),
    );
    cardJackpot.appendChild(field(
      "Jackpot", jackpotRow,
      "When checked, a share of every losing bet builds one shared pot. " +
        "Three 7️⃣ symbols on the slots wins whichever is bigger: the pot, or " +
        "the standard 120× payout. Unchecked, the slots pay the flat 120×.",
    ));
    cardJackpot.appendChild(field(
      "Share of Each Losing Bet (percent)",
      numInput("jackpot_cut_pct", c.jackpot_cut_pct ?? 25, 0, "1", 100),
      "Between 0 and 100. A bigger share grows the pot faster and lets the " +
        "casino destroy less currency overall.",
    ));
    cardJackpot.appendChild(field(
      "Starting Pot After a Win",
      numInput("jackpot_seed", c.jackpot_seed ?? 100, 0),
      "What the pot resets to once someone wins it. This amount is created " +
        "out of nothing each time, so keep it modest.",
    ));

    const cardTiming = card("Table Timing");
    cardTiming.appendChild(field(
      "Roulette Betting Window (seconds)",
      numInput("roulette_window_seconds", c.roulette_window_seconds ?? 45, 15, "1", 600),
      "How long bets stay open after someone starts a round. Between 15 and 600 seconds.",
    ));
    cardTiming.appendChild(field(
      "Blackjack Idle Timeout (seconds)",
      numInput("blackjack_idle_seconds", c.blackjack_idle_seconds ?? 180, 30, "1", 3600),
      "A hand nobody touches for this long stands automatically so the table " +
        "frees up. Between 30 and 3600 seconds.",
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
        ["min_bet", "Minimum Bet", 1, null],
        ["max_bet", "Maximum Bet", 0, null],
        ["daily_wager_cap", "Daily Wager Cap", 0, null],
        ["jackpot_cut_pct", "Share of Each Losing Bet", 0, 100],
        ["jackpot_seed", "Starting Pot After a Win", 0, null],
        ["roulette_window_seconds", "Roulette Betting Window", 15, 600],
        ["blackjack_idle_seconds", "Blackjack Idle Timeout", 30, 3600],
      ]) {
        const raw = String(fd.get(name) ?? "").trim();
        const v = parseInt(raw, 10);
        const bounds = max == null ? `${min} or more` : `between ${min} and ${max}`;
        if (raw === "" || !Number.isFinite(v) || v < min || (max != null && v > max)) {
          showStatus(statusEl, false, `${label} must be a whole number ${bounds}.`);
          form.querySelector(`[name="${name}"]`).focus();
          return;
        }
        nums[name] = v;
      }
      if (nums.max_bet && nums.min_bet > nums.max_bet) {
        showStatus(statusEl, false, "Minimum Bet cannot be larger than Maximum Bet.");
        form.querySelector('[name="min_bet"]').focus();
        return;
      }
      try {
        await apiPut("/api/config/casino", {
          channel_id: chanPicker.getValue() || "0", // string — snowflake rule
          ...nums,
          coinflip_enabled: fd.has("coinflip_enabled"),
          slots_enabled: fd.has("slots_enabled"),
          blackjack_enabled: fd.has("blackjack_enabled"),
          roulette_enabled: fd.has("roulette_enabled"),
          jackpot_enabled: fd.has("jackpot_enabled"),
        });
        showStatus(statusEl, true);
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    });

    container.appendChild(panel);
  })();
}
