import {
  loadConfig, loadChannels, mountChannelMultiPicker, apiPut, showStatus,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const cfg = config.games_hot_potato_group;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Hot Potato (Group)</h2>
          <div class="subtitle">Lobby pass-the-bomb settings</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Cooldown (hours)</label>
            <input type="number" name="cooldown_hours" min="0" step="1" value="${cfg.cooldown_hours}" />
            <div class="field-hint">Hours before a player can join another game (default 48)</div>
          </div>
          <div class="field">
            <label>Nickname Duration (hours)</label>
            <input type="number" name="sentence_hours" min="1" step="1" value="${cfg.sentence_hours}" />
            <div class="field-hint">How long the loser's imposed nickname lasts (default 24)</div>
          </div>
          <div class="field">
            <label>Minimum Fuse (seconds)</label>
            <input type="number" name="min_fuse" min="5" step="1" value="${cfg.min_fuse}" />
            <div class="field-hint">Minimum fuse length per round (default 20)</div>
          </div>
          <div class="field">
            <label>Maximum Fuse (seconds)</label>
            <input type="number" name="max_fuse" min="10" step="1" value="${cfg.max_fuse}" />
            <div class="field-hint">Maximum fuse length per round (default 60)</div>
          </div>
          <div class="field">
            <label>Minimum Hold (seconds)</label>
            <input type="number" name="min_hold" min="0" step="0.5" value="${cfg.min_hold}" />
            <div class="field-hint">Seconds a holder must wait before passing (default 2)</div>
          </div>
          <div class="field">
            <label>Minimum Players</label>
            <input type="number" name="min_players" min="2" step="1" value="${cfg.min_players}" />
            <div class="field-hint">Minimum players to start (default 2)</div>
          </div>
          <div class="field">
            <label>Maximum Players</label>
            <input type="number" name="max_players" min="2" step="1" value="${cfg.max_players}" />
            <div class="field-hint">Maximum players in a lobby (default 10)</div>
          </div>
          <div class="field">
            <label>Allowed Channels</label>
            <div data-picker="channel_allowlist"></div>
            <div class="field-hint">Leave empty to allow the game everywhere — type to search, click to add</div>
          </div>
          <div class="field">
            <label>Max Nickname Length</label>
            <input type="number" name="max_nick_length" min="1" max="32" step="1" value="${cfg.max_nick_length}" />
            <div class="field-hint">Character cap for imposed nicknames (default 32)</div>
          </div>
          <div class="field">
            <label>Max Stakes Length</label>
            <input type="number" name="max_stakes_length" min="1" max="2000" step="1" value="${cfg.max_stakes_length}" />
            <div class="field-hint">Character cap for the stakes text (default 200)</div>
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const allowlist = mountChannelMultiPicker(
      form.querySelector('[data-picker="channel_allowlist"]'), channels, cfg.channel_allowlist
    );

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/games-hot-potato-group", {
          cooldown_hours: parseInt(fd.get("cooldown_hours"), 10),
          sentence_hours: parseInt(fd.get("sentence_hours"), 10),
          min_fuse: parseFloat(fd.get("min_fuse")),
          max_fuse: parseFloat(fd.get("max_fuse")),
          min_hold: parseFloat(fd.get("min_hold")),
          min_players: parseInt(fd.get("min_players"), 10),
          max_players: parseInt(fd.get("max_players"), 10),
          channel_allowlist: allowlist.getValues(),
          max_nick_length: parseInt(fd.get("max_nick_length"), 10),
          max_stakes_length: parseInt(fd.get("max_stakes_length"), 10),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
