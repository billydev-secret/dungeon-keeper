import {
  loadConfig, loadChannels, mountChannelMultiPicker, apiPut, showStatus,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const cfg = config.games_quickdraw;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Quickdraw</h2>
          <div class="subtitle">Reaction duel settings</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Cooldown (hours)</label>
            <input type="number" name="cooldown_hours" min="0" step="1" value="${cfg.cooldown_hours}" />
            <div class="field-hint">Hours before the same pair can rematch (default 48)</div>
          </div>
          <div class="field">
            <label>Nickname Duration (hours)</label>
            <input type="number" name="sentence_hours" min="1" step="1" value="${cfg.sentence_hours}" />
            <div class="field-hint">How long the loser's imposed nickname lasts (default 24)</div>
          </div>
          <div class="field">
            <label><input type="checkbox" name="allow_early_revert" ${cfg.allow_early_revert ? "checked" : ""} /> Allow early revert</label>
            <div class="field-hint">Lets the loser restore their nickname early with /games quickdraw revert</div>
          </div>
          <div class="field">
            <label>Minimum Delay (seconds)</label>
            <input type="number" name="min_delay" min="0.5" step="0.5" value="${cfg.min_delay}" />
            <div class="field-hint">Minimum seconds before the draw signal (default 3.0)</div>
          </div>
          <div class="field">
            <label>Maximum Delay (seconds)</label>
            <input type="number" name="max_delay" min="1" step="0.5" value="${cfg.max_delay}" />
            <div class="field-hint">Maximum seconds before the draw signal (default 8.0)</div>
          </div>
          <div class="field">
            <label>Draw Window (seconds)</label>
            <input type="number" name="draw_window" min="1" step="0.5" value="${cfg.draw_window}" />
            <div class="field-hint">Seconds to fire after the signal before it voids (default 5.0)</div>
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
        await apiPut("/api/config/games-quickdraw", {
          cooldown_hours: parseInt(fd.get("cooldown_hours"), 10),
          sentence_hours: parseInt(fd.get("sentence_hours"), 10),
          allow_early_revert: form.querySelector('input[name="allow_early_revert"]').checked,
          min_delay: parseFloat(fd.get("min_delay")),
          max_delay: parseFloat(fd.get("max_delay")),
          draw_window: parseFloat(fd.get("draw_window")),
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
