import { esc } from "../api.js";
import { mountGamePanel } from "./games-panel-shared.js";
import {
  loadConfig, loadChannels, mountChannelMultiPicker, apiPut, showStatus,
  guardForm, renderMetaWarning,
  mountAsync,
} from "../config-helpers.js";

const numField = (name, label, value, hint, { min, max, step = "1" }) => `
  <div class="field">
    <label for="gc-${name}">${label}</label>
    <input type="number" name="${name}" id="gc-${name}" required
      min="${min}" max="${max}" step="${step}" value="${value}" style="max-width:140px;" />
    <div class="field-hint">${hint}</div>
  </div>`;

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  return mountAsync(container, async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    // `|| {}`: a config payload missing this section (a fresh guild, a partial
    // response) used to throw on the first cfg.<field> read, and the panel hung
    // on "Loading configuration…" forever. Undefined fields render blank instead.
    const cfg = config.games_hot_potato || {};

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Hot Potato</h2>
          <div class="subtitle">Two players pass a ticking bomb back and forth — whoever is holding it when it goes off takes the forfeit</div>
        </header>
        <div data-region="status"></div>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Bomb Timer</div>
            ${numField("min_timer", "Shortest Fuse (seconds)", cfg.min_timer,
              "The bomb never goes off sooner than this after a round starts.",
              { min: 5, max: 600, step: "0.5" })}
            ${numField("max_timer", "Longest Fuse (seconds)", cfg.max_timer,
              "The bomb always goes off by this point. The actual moment is picked at random between the two, so nobody can count it out.",
              { min: 10, max: 600, step: "0.5" })}
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
            <div class="field">
              <label for="gc-nick_denylist">Extra Banned Words</label>
              <input type="text" name="nick_denylist" id="gc-nick_denylist"
                value="${esc((cfg.nick_denylist || []).join(", "))}"
                style="width:100%;max-width:420px;box-sizing:border-box;" />
              <div class="field-hint">Comma-separated. A nickname or stakes text
                that uses one of these words is refused, on top of the slurs the bot
                always blocks. Capitals are ignored, and each entry has to appear as a
                whole word &mdash; &ldquo;ass&rdquo; won&rsquo;t block
                &ldquo;class&rdquo;.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Availability</div>
            ${numField("cooldown_hours", "Wait Before a Rematch (hours)", cfg.cooldown_hours,
              "How long the same two people must wait before they can play each other again. 0 allows endless rematches.",
              { min: 0, max: 8760 })}
            ${numField("challenge_limit_per_hour", "Challenges Per Person Per Hour", cfg.challenge_limit_per_hour,
              "How many challenges one person may start in an hour. Set to 0 for no limit. This is a spam brake, not a pacing rule &mdash; a busy games night can easily run through a low number.",
              { min: 0, max: 999 })}
            <div class="field">
              <label>Allowed Channels</label>
              <div data-picker="channel_allowlist"></div>
              <div class="field-hint">Restrict this game to these channels. Leave the
                list empty to allow it anywhere. This list is the only channel rule
                for this game &mdash; the allowed-channel list on Games &rsaquo; Global
                Config governs the question-bank games, not this one.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    mountGamePanel(container.querySelector('[data-region="status"]'), {
      gameType: "hot_potato",
      gameName: "Hot Potato",
      bare: true,
      statusHint: "When off, nobody can start a new Hot Potato game. Games already running finish normally.",
    });

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const allowlist = mountChannelMultiPicker(
      form.querySelector('[data-picker="channel_allowlist"]'), channels, cfg.channel_allowlist,
      { label: "Allowed Channels" },
    );

    guardForm(form);

    const NUMS = [
      ["cooldown_hours", "Wait Before a Rematch", 0, 8760, false],
      ["challenge_limit_per_hour", "Challenges Per Person Per Hour", 0, 999, false],
      ["sentence_hours", "Nickname Lasts", 1, 8760, false],
      ["min_timer", "Shortest Fuse", 5, 600, true],
      ["max_timer", "Longest Fuse", 10, 600, true],
      ["max_nick_length", "Longest Nickname", 1, 32, false],
      ["max_stakes_length", "Longest Stakes Text", 1, 2000, false],
    ];

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const payload = { channel_allowlist: allowlist.getValues() };
      payload.nick_denylist = String(fd.get("nick_denylist") || "")
        .split(",").map(s => s.trim()).filter(Boolean);
      for (const [name, label, min, max, isFloat] of NUMS) {
        const n = isFloat ? parseFloat(fd.get(name)) : parseInt(fd.get(name), 10);
        if (!Number.isFinite(n) || n < min || n > max) {
          showStatus(status, false, `${label} must be a number from ${min} to ${max}`);
          form.querySelector(`[name=${name}]`).focus();
          return;
        }
        payload[name] = n;
      }
      if (payload.max_timer < payload.min_timer) {
        showStatus(status, false, "Longest Fuse cannot be shorter than Shortest Fuse");
        form.querySelector("[name=max_timer]").focus();
        return;
      }
      try {
        await apiPut("/api/config/games-hot-potato", payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the Hot Potato settings." });
}
