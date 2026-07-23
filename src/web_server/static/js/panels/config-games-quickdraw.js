import { api } from "../api.js";
import {
  loadConfig, loadChannels, mountChannelMultiPicker, apiPut, showStatus,
  guardForm, renderMetaWarning,
} from "../config-helpers.js";

// Party games only run in the channels allow-listed on Games › Global Config.
async function gameChannelsBanner() {
  try {
    const data = await api("/api/games/config/channels");
    if ((data.channels || []).length) return "";
  } catch (_) {
    return "";
  }
  return `<div class="empty" role="status" style="margin-bottom:12px;">
    No channels are allowed to host party games yet, so this game cannot be played
    anywhere. Add one under <a href="#/games-config">Games › Global Config</a> —
    the settings below start applying as soon as you do.</div>`;
}

const numField = (name, label, value, hint, { min, max, step = "1" }) => `
  <div class="field">
    <label for="gc-${name}">${label}</label>
    <input type="number" name="${name}" id="gc-${name}" required
      min="${min}" max="${max}" step="${step}" value="${value}" style="max-width:140px;" />
    <div class="field-hint">${hint}</div>
  </div>`;

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels, banner] = await Promise.all([
      loadConfig(), loadChannels(), gameChannelsBanner(),
    ]);
    const cfg = config.games_quickdraw;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Quickdraw</h2>
          <div class="subtitle">Two players wait for the signal — the faster reaction wins, and drawing early loses</div>
        </header>
        ${banner}
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">The Draw</div>
            ${numField("min_delay", "Shortest Wait for the Signal (seconds)", cfg.min_delay,
              "The signal never comes sooner than this after the round starts.",
              { min: 0.5, max: 120, step: "0.5" })}
            ${numField("max_delay", "Longest Wait for the Signal (seconds)", cfg.max_delay,
              "The signal always comes by this point. The exact moment is random between the two, so nobody can time it.",
              { min: 1, max: 120, step: "0.5" })}
            ${numField("draw_window", "Time to React (seconds)", cfg.draw_window,
              "How long players have to fire once the signal appears. If neither fires in time the round is thrown out and nobody loses.",
              { min: 1, max: 120, step: "0.5" })}
          </div>

          <div class="card">
            <div class="section-label">Forfeit</div>
            ${numField("sentence_hours", "Nickname Lasts (hours)", cfg.sentence_hours,
              "How long the loser has to wear the nickname they were given before it is removed automatically.",
              { min: 1, max: 8760 })}
            ${numField("max_nick_length", "Longest Nickname (characters)", cfg.max_nick_length,
              "Nicknames longer than this are refused. Discord itself will not accept more than 32 characters.",
              { min: 1, max: 32 })}
            ${numField("max_stakes_length", "Longest Stakes Text (characters)", cfg.max_stakes_length,
              "How much a challenger may write when describing what is at stake.", { min: 1, max: 2000 })}
          </div>

          <div class="card">
            <div class="section-label">Availability</div>
            ${numField("cooldown_hours", "Wait Before a Rematch (hours)", cfg.cooldown_hours,
              "How long the same two people must wait before they can duel each other again. 0 allows endless rematches.",
              { min: 0, max: 8760 })}
            <div class="field">
              <label>Allowed Channels</label>
              <div data-picker="channel_allowlist"></div>
              <div class="field-hint">Restrict this game to these channels. Leave the
                list empty to allow it in every channel that may host party games.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const allowlist = mountChannelMultiPicker(
      form.querySelector('[data-picker="channel_allowlist"]'), channels, cfg.channel_allowlist,
      { label: "Allowed Channels" },
    );

    guardForm(form);

    const NUMS = [
      ["cooldown_hours", "Wait Before a Rematch", 0, 8760, false],
      ["sentence_hours", "Nickname Lasts", 1, 8760, false],
      ["min_delay", "Shortest Wait for the Signal", 0.5, 120, true],
      ["max_delay", "Longest Wait for the Signal", 1, 120, true],
      ["draw_window", "Time to React", 1, 120, true],
      ["max_nick_length", "Longest Nickname", 1, 32, false],
      ["max_stakes_length", "Longest Stakes Text", 1, 2000, false],
    ];

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const payload = { channel_allowlist: allowlist.getValues() };
      for (const [name, label, min, max, isFloat] of NUMS) {
        const n = isFloat ? parseFloat(fd.get(name)) : parseInt(fd.get(name), 10);
        if (!Number.isFinite(n) || n < min || n > max) {
          showStatus(status, false, `${label} must be a number from ${min} to ${max}`);
          form.querySelector(`[name=${name}]`).focus();
          return;
        }
        payload[name] = n;
      }
      if (payload.max_delay < payload.min_delay) {
        showStatus(status, false, "Longest Wait for the Signal cannot be shorter than Shortest Wait for the Signal");
        form.querySelector("[name=max_delay]").focus();
        return;
      }
      try {
        await apiPut("/api/config/games-quickdraw", payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
