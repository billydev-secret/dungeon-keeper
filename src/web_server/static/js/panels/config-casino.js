import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  buildField,
  mountChannelPicker,
} from "../config-helpers.js";

function numInput(name, value, min, step = "1") {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.min = String(min);
  inp.step = step;
  inp.value = String(value);
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

    container.textContent = "";
    const panel = document.createElement("div");
    panel.className = "panel";

    const hdr = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "The Golden Meadow Casino";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent =
      "House gambling games staking the guild currency. Setting a channel " +
      "opens the casino; the bot keeps its hub panel there.";
    hdr.append(h2, sub);
    panel.appendChild(hdr);

    const form = document.createElement("form");
    form.className = "form";
    panel.appendChild(form);

    // Casino channel — the master switch.
    const chanSlot = document.createElement("span");
    form.appendChild(buildField(
      "Casino Channel",
      chanSlot,
      "Where the hub panel lives and games are played. \"(disabled)\" closes " +
        "the whole casino and removes the panel.",
    ));
    const chanPicker = mountChannelPicker(
      chanSlot, channels, String(c.channel_id || "0"),
      { emptyValue: "0", emptyLabel: "(disabled)" },
    );

    form.appendChild(buildField(
      "Minimum Bet", numInput("min_bet", c.min_bet ?? 5, 1),
      "The smallest stake any table accepts.",
    ));
    form.appendChild(buildField(
      "Maximum Bet", numInput("max_bet", c.max_bet ?? 100, 0),
      "The largest stake per play. 0 removes the ceiling (the daily cap " +
        "still applies).",
    ));
    form.appendChild(buildField(
      "Daily Wager Cap", numInput("daily_wager_cap", c.daily_wager_cap ?? 500, 0),
      "Total a member can stake per guild-local day, across all tables. " +
        "0 = uncapped. This bounds how fast the casino can mint or drain.",
    ));

    // Wrapping flex row (not fixed-width) so phones stack the toggles.
    const tables = document.createElement("div");
    tables.style.cssText = "display:flex; flex-wrap:wrap; gap:8px 16px;";
    tables.append(
      checkbox("coinflip_enabled", c.coinflip_enabled !== false, "Coinflip"),
      checkbox("slots_enabled", c.slots_enabled !== false, "Slots"),
      checkbox("blackjack_enabled", c.blackjack_enabled !== false, "Blackjack"),
      checkbox("roulette_enabled", c.roulette_enabled !== false, "Roulette"),
    );
    form.appendChild(buildField(
      "Open Tables", tables,
      "Unchecked tables refuse bets and drop off the hub panel.",
    ));

    const jackpotRow = document.createElement("div");
    jackpotRow.style.cssText = "display:flex; flex-wrap:wrap; gap:8px 16px;";
    jackpotRow.append(
      checkbox("jackpot_enabled", c.jackpot_enabled !== false, "Progressive jackpot"),
    );
    form.appendChild(buildField(
      "Jackpot", jackpotRow,
      "A cut of every lost bet feeds one pot; triple 7️⃣ on the slots " +
        "wins the larger of the pot or the flat 120×.",
    ));
    form.appendChild(buildField(
      "Jackpot Cut (% of each lost bet)",
      numInput("jackpot_cut_pct", c.jackpot_cut_pct ?? 25, 0),
      "0–100. Bigger cut = faster-growing pot (and a casino that burns less).",
    ));
    form.appendChild(buildField(
      "Jackpot Seed",
      numInput("jackpot_seed", c.jackpot_seed ?? 100, 0),
      "What the pot resets to after it's won — minted when claimed, so keep it modest.",
    ));

    form.appendChild(buildField(
      "Roulette Betting Window (seconds)",
      numInput("roulette_window_seconds", c.roulette_window_seconds ?? 45, 15),
      "How long bets stay open once someone spins up a round (15–600).",
    ));
    form.appendChild(buildField(
      "Blackjack Idle Timeout (seconds)",
      numInput("blackjack_idle_seconds", c.blackjack_idle_seconds ?? 180, 30),
      "An untouched hand stands automatically after this long (30–3600).",
    ));

    const row = document.createElement("div");
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save";
    const statusEl = document.createElement("span");
    row.append(saveBtn, statusEl);
    form.appendChild(row);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const nums = {};
      for (const [name, min] of [
        ["min_bet", 1],
        ["max_bet", 0],
        ["daily_wager_cap", 0],
        ["jackpot_cut_pct", 0],
        ["jackpot_seed", 0],
        ["roulette_window_seconds", 15],
        ["blackjack_idle_seconds", 30],
      ]) {
        const v = parseInt(fd.get(name), 10);
        if (!Number.isFinite(v) || v < min) {
          showStatus(statusEl, false, `${name.replaceAll("_", " ")} must be ≥ ${min}`);
          return;
        }
        nums[name] = v;
      }
      if (nums.max_bet && nums.min_bet > nums.max_bet) {
        showStatus(statusEl, false, "Minimum bet can't exceed the maximum");
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
