import { loadConfig, loadChannels, loadRoles, channelSelect, channelSelectMulti, roleSelect, apiPut, showStatus } from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    const xp = config.xp;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>XP Logging</h2>
          <div class="subtitle">XP role grants, log channels, and excluded channels</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Level 5 Role</label>
            <select name="level_5_role_id">${roleSelect(roles, xp.level_5_role_id)}</select>
          </div>
          <div class="field">
            <label>Level 5 Log Channel</label>
            <select name="level_5_log_channel_id">${channelSelect(channels, xp.level_5_log_channel_id)}</select>
          </div>
          <div class="field">
            <label>Level-Up Log Channel</label>
            <select name="level_up_log_channel_id">${channelSelect(channels, xp.level_up_log_channel_id)}</select>
          </div>
          <div class="field">
            <label>XP Grant Allowed User IDs</label>
            <input type="text" name="xp_grant_allowed_user_ids" value="${xp.xp_grant_allowed_user_ids.join(", ")}" />
            <div class="field-hint">Comma-separated user IDs allowed to use /xp grant</div>
          </div>
          <div class="field">
            <label>XP Excluded Channels</label>
            <select name="xp_excluded_channel_ids" multiple size="6">${channelSelectMulti(channels, xp.xp_excluded_channel_ids)}</select>
            <div class="field-hint">Channels where XP is not earned (Ctrl/Cmd-click to select multiple)</div>
          </div>

          <details class="form-section" open>
            <summary class="form-section-summary">Algorithm Tuning</summary>
            <div class="note" style="margin: 8px 0 14px;">
              <strong>How XP works:</strong>
              Each message earns <code>word_count &times; word_xp</code> base XP, plus a reply bonus if replying to another human.
              Multipliers reduce XP for rapid-fire messages (cooldown), duplicate content, and back-and-forth farming between two users.
              Voice XP is awarded per interval while in a voice channel with enough humans.
              Level thresholds follow <code>factor &times; (level &minus; 1)&sup2;</code>.
            </div>

            <div class="section-label">Text XP</div>
            <div class="field">
              <label>XP per Word</label>
              <input type="number" name="message_word_xp" step="0.01" min="0" value="${xp.message_word_xp}" />
              <div class="field-hint">XP per word in a message (default 0.08)</div>
            </div>
            <div class="field">
              <label>Reply Bonus XP</label>
              <input type="number" name="reply_bonus_xp" step="0.01" min="0" value="${xp.reply_bonus_xp}" />
              <div class="field-hint">Flat bonus for replying to another human (default 0.33)</div>
            </div>
            <div class="field">
              <label>Image Reaction XP</label>
              <input type="number" name="image_reaction_received_xp" step="0.01" min="0" value="${xp.image_reaction_received_xp}" />
              <div class="field-hint">XP per reaction received on image posts (default 0.17)</div>
            </div>

            <div class="section-label">Cooldown</div>
            <div class="field">
              <label>Cooldown Thresholds (seconds)</label>
              <input type="text" name="cooldown_thresholds_seconds" value="${xp.cooldown_thresholds_seconds}" />
              <div class="field-hint">3 comma-separated thresholds in seconds (default 10,30,60)</div>
            </div>
            <div class="field">
              <label>Cooldown Multipliers</label>
              <input type="text" name="cooldown_multipliers" value="${xp.cooldown_multipliers}" />
              <div class="field-hint">XP multiplier at each cooldown tier (default 0.35,0.6,0.85)</div>
            </div>

            <div class="section-label">Anti-Farm</div>
            <div class="field">
              <label>Duplicate Multiplier</label>
              <input type="number" name="duplicate_multiplier" step="0.01" min="0" max="1" value="${xp.duplicate_multiplier}" />
              <div class="field-hint">Multiplier for repeated/duplicate messages (default 0.2)</div>
            </div>
            <div class="field">
              <label>Pair Streak Threshold</label>
              <input type="number" name="pair_streak_threshold" step="1" min="1" value="${xp.pair_streak_threshold}" />
              <div class="field-hint">Back-and-forth messages before the farming penalty starts (default 4)</div>
            </div>
            <div class="field">
              <label>Pair Streak Multiplier</label>
              <input type="number" name="pair_streak_multiplier" step="0.01" min="0" max="1" value="${xp.pair_streak_multiplier}" />
              <div class="field-hint">XP multiplier during pair farming penalty (default 0.5)</div>
            </div>

            <div class="section-label">Voice XP</div>
            <div class="field">
              <label>Voice XP per Interval</label>
              <input type="number" name="voice_award_xp" step="0.01" min="0" value="${xp.voice_award_xp}" />
              <div class="field-hint">XP awarded each voice interval (default 1.67)</div>
            </div>
            <div class="field">
              <label>Voice Interval (seconds)</label>
              <input type="number" name="voice_interval_seconds" step="1" min="10" value="${xp.voice_interval_seconds}" />
              <div class="field-hint">How often voice XP is awarded in seconds (default 60)</div>
            </div>
            <div class="field">
              <label>Min Humans in Voice</label>
              <input type="number" name="voice_min_humans" step="1" min="1" value="${xp.voice_min_humans}" />
              <div class="field-hint">Minimum humans in voice channel for XP to count (default 2)</div>
            </div>

            <div class="section-label">Leveling</div>
            <div class="field">
              <label>Manual Grant XP</label>
              <input type="number" name="manual_grant_xp" step="0.1" min="0" value="${xp.manual_grant_xp}" />
              <div class="field-hint">XP given per /xp grant command (default 20)</div>
            </div>
            <div class="field">
              <label>Level Curve Factor</label>
              <input type="number" name="level_curve_factor" step="0.1" min="0.1" value="${xp.level_curve_factor}" />
              <div class="field-hint">Steepness of the level curve — higher means slower leveling (default 15.6)</div>
            </div>
          </details>

          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/xp", {
          level_5_role_id: fd.get("level_5_role_id"),
          level_5_log_channel_id: fd.get("level_5_log_channel_id"),
          level_up_log_channel_id: fd.get("level_up_log_channel_id"),
          xp_grant_allowed_user_ids: fd.get("xp_grant_allowed_user_ids").split(",").map((s) => s.trim()).filter(Boolean),
          xp_excluded_channel_ids: Array.from(form.querySelector('select[name="xp_excluded_channel_ids"]').selectedOptions).map((o) => o.value),
          // Algorithm coefficients
          message_word_xp: parseFloat(fd.get("message_word_xp")),
          reply_bonus_xp: parseFloat(fd.get("reply_bonus_xp")),
          image_reaction_received_xp: parseFloat(fd.get("image_reaction_received_xp")),
          cooldown_thresholds_seconds: fd.get("cooldown_thresholds_seconds"),
          cooldown_multipliers: fd.get("cooldown_multipliers"),
          duplicate_multiplier: parseFloat(fd.get("duplicate_multiplier")),
          pair_streak_threshold: parseInt(fd.get("pair_streak_threshold")),
          pair_streak_multiplier: parseFloat(fd.get("pair_streak_multiplier")),
          voice_award_xp: parseFloat(fd.get("voice_award_xp")),
          voice_interval_seconds: parseInt(fd.get("voice_interval_seconds")),
          voice_min_humans: parseInt(fd.get("voice_min_humans")),
          manual_grant_xp: parseFloat(fd.get("manual_grant_xp")),
          level_curve_factor: parseFloat(fd.get("level_curve_factor")),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
