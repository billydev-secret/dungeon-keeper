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
    const cfg = config.games_musical_chairs;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Musical Chairs</h2>
          <div class="subtitle">The music stops, everyone grabs a chair, and one player is knocked out each round</div>
        </header>
        ${banner}
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Lobby</div>
            ${numField("min_players", "Fewest Players to Start", cfg.min_players,
              "A lobby will not begin until this many people have joined.", { min: 3, max: 50 })}
            ${numField("max_players", "Most Players Per Lobby", cfg.max_players,
              "Once a lobby is this full, nobody else can join it.", { min: 3, max: 50 })}
          </div>

          <div class="card">
            <div class="section-label">Each Round</div>
            ${numField("min_music", "Shortest Music (seconds)", cfg.min_music,
              "The music never stops sooner than this.", { min: 2, max: 300, step: "0.5" })}
            ${numField("max_music", "Longest Music (seconds)", cfg.max_music,
              "The music always stops by this point. The exact moment is random between the two, so nobody can time it.",
              { min: 3, max: 300, step: "0.5" })}
            ${numField("scramble_window", "Time to Grab a Chair (seconds)", cfg.scramble_window,
              "How long players have to sit down after the music stops. Anyone who has not sat by then is out.",
              { min: 2, max: 300, step: "0.5" })}
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="false_start_elim" ${cfg.false_start_elim ? "checked" : ""} />
                Knock out players who sit too early
              </label>
              <div class="field-hint">When checked, pressing Sit while the music is
                still playing puts that player out on the spot. Unchecked, an early
                press is simply ignored and they can try again.</div>
            </div>
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
              "How much a host may write when describing what is at stake.", { min: 1, max: 2000 })}
          </div>

          <div class="card">
            <div class="section-label">Availability</div>
            ${numField("cooldown_hours", "Wait Between Games (hours)", cfg.cooldown_hours,
              "How long a player must wait after one game before joining another. 0 lets people play back to back.",
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
      ["cooldown_hours", "Wait Between Games", 0, 8760, false],
      ["sentence_hours", "Nickname Lasts", 1, 8760, false],
      ["min_music", "Shortest Music", 2, 300, true],
      ["max_music", "Longest Music", 3, 300, true],
      ["scramble_window", "Time to Grab a Chair", 2, 300, true],
      ["min_players", "Fewest Players to Start", 3, 50, false],
      ["max_players", "Most Players Per Lobby", 3, 50, false],
      ["max_nick_length", "Longest Nickname", 1, 32, false],
      ["max_stakes_length", "Longest Stakes Text", 1, 2000, false],
    ];

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const payload = {
        channel_allowlist: allowlist.getValues(),
        false_start_elim: form.querySelector('input[name="false_start_elim"]').checked,
      };
      for (const [name, label, min, max, isFloat] of NUMS) {
        const n = isFloat ? parseFloat(fd.get(name)) : parseInt(fd.get(name), 10);
        if (!Number.isFinite(n) || n < min || n > max) {
          showStatus(status, false, `${label} must be a number from ${min} to ${max}`);
          form.querySelector(`[name=${name}]`).focus();
          return;
        }
        payload[name] = n;
      }
      if (payload.max_music < payload.min_music) {
        showStatus(status, false, "Longest Music cannot be shorter than Shortest Music");
        form.querySelector("[name=max_music]").focus();
        return;
      }
      if (payload.max_players < payload.min_players) {
        showStatus(status, false, "Most Players Per Lobby cannot be lower than Fewest Players to Start");
        form.querySelector("[name=max_players]").focus();
        return;
      }
      try {
        await apiPut("/api/config/games-musical-chairs", payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
