import {
  loadConfig, loadChannels, loadRoles, loadMembers,
  mountChannelPicker, mountRolePicker, mountChannelMultiPicker, mountMemberMultiPicker,
  apiPut, showStatus, guardForm, renderMetaWarning,
} from "../config-helpers.js";

// Numeric fields, in save order. [name, visible label, min, max|null, integer?]
// Used both for client-side validation that names the offending field (W-C5)
// and to keep the payload's number parsing in one place.
const NUM_FIELDS = [
  ["message_word_xp", "XP per Word", 0, null, false],
  ["reply_bonus_xp", "Reply Bonus XP", 0, null, false],
  ["image_reaction_received_xp", "XP per Reaction on an Image", 0, null, false],
  ["reaction_given_xp", "XP for Adding a Reaction", 0, null, false],
  ["duplicate_multiplier", "Repeat-Message Multiplier", 0, 1, false],
  ["pair_streak_threshold", "Back-and-Forth Limit", 1, null, true],
  ["pair_streak_multiplier", "Back-and-Forth Multiplier", 0, 1, false],
  ["voice_award_xp", "XP per Voice Interval", 0, null, false],
  ["voice_interval_seconds", "Voice Interval (seconds)", 10, null, true],
  ["voice_min_humans", "People Needed in Voice", 1, null, true],
  ["manual_grant_xp", "XP per Manual Grant", 0, null, false],
  ["level_curve_factor", "Level Curve Factor", 0.1, null, false],
];

// "10,30,60" style list fields. [name, visible label, expected count]
const LIST_FIELDS = [
  ["cooldown_thresholds_seconds", "Rapid-Fire Time Tiers (seconds)", 3],
  ["cooldown_multipliers", "Rapid-Fire Multipliers", 3],
];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading XP settings…</div></div>`;

  (async () => {
    const [config, channels, roles, members] = await Promise.all([
      loadConfig(), loadChannels(), loadRoles(), loadMembers(),
    ]);
    const xp = config.xp;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>XP &amp; Leveling</h2>
          <div class="subtitle">How members earn XP, who can grant it, and where level-ups are logged</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Roles &amp; Log Channels</div>
            <div class="field">
              <label>Level 5 Role</label>
              <div data-picker="level_5_role_id"></div>
              <div class="field-hint">Given automatically the first time a member reaches level 5. Choose "(none)" to hand out no role.</div>
            </div>
            <div class="field">
              <label>Promotion Review Role</label>
              <div data-picker="promotion_review_grant_role_id"></div>
              <div class="field-hint">Members with this role can approve promotion reviews. Choose "(none)" to leave reviews to admins.</div>
            </div>
            <div class="field">
              <label>Level 5 Log Channel</label>
              <div data-picker="level_5_log_channel_id"></div>
              <div class="field-hint">Posts a note here whenever someone earns the Level 5 role, so your team can welcome them. "(disabled)" posts nothing.</div>
            </div>
            <div class="field">
              <label>Level-Up Log Channel</label>
              <div data-picker="level_up_log_channel_id"></div>
              <div class="field-hint">Posts a note here for every level-up, at every level. "(disabled)" posts nothing.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Permissions &amp; Exclusions</div>
            <div class="field">
              <label>Members Who Can Grant XP</label>
              <div data-picker="xp_grant_allowed_user_ids"></div>
              <div class="field-hint">Only these members can run <code>/xp_give</code> to hand out XP by hand. Type to search, then click a name to add it.</div>
            </div>
            <div class="field">
              <label>Channels That Earn No XP</label>
              <div data-picker="xp_excluded_channel_ids"></div>
              <div class="field-hint">Messages, reactions, and voice time in these channels earn nothing — useful for bot-spam and off-topic rooms. Type to search, then click a channel to add it.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">How XP Is Calculated</div>
            <div class="note" style="margin: 0 0 14px;">
              Every message earns its word count times the XP per word, plus a bonus when it
              replies to another person. Multipliers cut XP down for rapid-fire posting, repeated
              text, and two members trading messages back and forth to farm. Voice XP is paid on a
              timer while a member sits in a voice channel with enough other people. The XP needed
              for a level follows <code>factor × (level − 1)²</code>, so each level costs more than the last.
              Defaults are listed in each hint — the defaults suit most servers.
            </div>
          </div>

          <div class="card">
            <div class="section-label">Message XP</div>
            <div class="field">
              <label for="xp-message_word_xp">XP per Word</label>
              <input type="number" name="message_word_xp" id="xp-message_word_xp" required step="0.01" min="0" value="${xp.message_word_xp}" style="max-width:140px;" />
              <div class="field-hint">Earned for each word a member writes. Raising this makes every level arrive faster. Default 0.08.</div>
            </div>
            <div class="field">
              <label for="xp-reply_bonus_xp">Reply Bonus XP</label>
              <input type="number" name="reply_bonus_xp" id="xp-reply_bonus_xp" required step="0.01" min="0" value="${xp.reply_bonus_xp}" style="max-width:140px;" />
              <div class="field-hint">A flat bonus on top when the message replies to another person. Rewards conversation over monologues. Default 0.33.</div>
            </div>
            <div class="field">
              <label for="xp-image_reaction_received_xp">XP per Reaction on an Image</label>
              <input type="number" name="image_reaction_received_xp" id="xp-image_reaction_received_xp" required step="0.01" min="0" value="${xp.image_reaction_received_xp}" style="max-width:140px;" />
              <div class="field-hint">Paid to the poster each time someone reacts to an image they shared. Default 0.17.</div>
            </div>
            <div class="field">
              <label for="xp-reaction_given_xp">XP for Adding a Reaction</label>
              <input type="number" name="reaction_given_xp" id="xp-reaction_given_xp" required step="0.01" min="0" value="${xp.reaction_given_xp}" style="max-width:140px;" />
              <div class="field-hint">Paid to the person who reacts to someone else's message. Default 0.34.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Rapid-Fire Slowdown</div>
            <div class="field">
              <label for="xp-cooldown_thresholds_seconds">Rapid-Fire Time Tiers (seconds)</label>
              <input type="text" name="cooldown_thresholds_seconds" id="xp-cooldown_thresholds_seconds" required value="${xp.cooldown_thresholds_seconds}" style="max-width:220px;" />
              <div class="field-hint">Three numbers, separated by commas, measured since the member's last message. Posting inside the first tier earns the least. Default 10,30,60.</div>
            </div>
            <div class="field">
              <label for="xp-cooldown_multipliers">Rapid-Fire Multipliers</label>
              <input type="text" name="cooldown_multipliers" id="xp-cooldown_multipliers" required value="${xp.cooldown_multipliers}" style="max-width:220px;" />
              <div class="field-hint">Three numbers, separated by commas, matching the tiers above. 0.35 means a message in that tier earns 35% of its normal XP. Default 0.35,0.6,0.85.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Anti-Farming</div>
            <div class="field">
              <label for="xp-duplicate_multiplier">Repeat-Message Multiplier</label>
              <input type="number" name="duplicate_multiplier" id="xp-duplicate_multiplier" required step="0.01" min="0" max="1" value="${xp.duplicate_multiplier}" style="max-width:140px;" />
              <div class="field-hint">Applied when a member sends the same text again. 0.2 means a repeat earns 20% of normal. Set to 1 to stop penalizing repeats. Default 0.2.</div>
            </div>
            <div class="field">
              <label for="xp-pair_streak_threshold">Back-and-Forth Limit</label>
              <input type="number" name="pair_streak_threshold" id="xp-pair_streak_threshold" required step="1" min="1" value="${xp.pair_streak_threshold}" style="max-width:140px;" />
              <div class="field-hint">How many messages two members may trade before the farming penalty below kicks in. Default 4.</div>
            </div>
            <div class="field">
              <label for="xp-pair_streak_multiplier">Back-and-Forth Multiplier</label>
              <input type="number" name="pair_streak_multiplier" id="xp-pair_streak_multiplier" required step="0.01" min="0" max="1" value="${xp.pair_streak_multiplier}" style="max-width:140px;" />
              <div class="field-hint">What those two members earn once the limit is passed. 0.5 means half XP until someone else joins in. Default 0.5.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Voice XP</div>
            <div class="field">
              <label for="xp-voice_award_xp">XP per Voice Interval</label>
              <input type="number" name="voice_award_xp" id="xp-voice_award_xp" required step="0.01" min="0" value="${xp.voice_award_xp}" style="max-width:140px;" />
              <div class="field-hint">Paid each time the interval below elapses while a member is in voice. Default 1.67.</div>
            </div>
            <div class="field">
              <label for="xp-voice_interval_seconds">Voice Interval (seconds)</label>
              <input type="number" name="voice_interval_seconds" id="xp-voice_interval_seconds" required step="1" min="10" value="${xp.voice_interval_seconds}" style="max-width:140px;" />
              <div class="field-hint">How often voice XP is paid out. Default 60 (once a minute).</div>
            </div>
            <div class="field">
              <label for="xp-voice_min_humans">People Needed in Voice</label>
              <input type="number" name="voice_min_humans" id="xp-voice_min_humans" required step="1" min="1" value="${xp.voice_min_humans}" style="max-width:140px;" />
              <div class="field-hint">Nobody earns voice XP until this many people (bots don't count) are in the channel — stops members idling alone for XP. Default 2.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Levels &amp; Manual Grants</div>
            <div class="field">
              <label for="xp-manual_grant_xp">XP per Manual Grant</label>
              <input type="number" name="manual_grant_xp" id="xp-manual_grant_xp" required step="0.1" min="0" value="${xp.manual_grant_xp}" style="max-width:140px;" />
              <div class="field-hint">How much XP one <code>/xp_give</code> hands over. Default 20.</div>
            </div>
            <div class="field">
              <label for="xp-level_curve_factor">Level Curve Factor</label>
              <input type="number" name="level_curve_factor" id="xp-level_curve_factor" required step="0.1" min="0.1" value="${xp.level_curve_factor}" style="max-width:140px;" />
              <div class="field-hint">How steep the climb is. A higher number means every level takes longer to reach. Default 15.6.</div>
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

    const level5Role = mountRolePicker(form.querySelector('[data-picker="level_5_role_id"]'), roles, xp.level_5_role_id, { label: "Level 5 Role" });
    const promotionReviewGrantRole = mountRolePicker(form.querySelector('[data-picker="promotion_review_grant_role_id"]'), roles, xp.promotion_review_grant_role_id, { label: "Promotion Review Role" });
    const level5Log = mountChannelPicker(form.querySelector('[data-picker="level_5_log_channel_id"]'), channels, xp.level_5_log_channel_id, { label: "Level 5 Log Channel" });
    const levelUpLog = mountChannelPicker(form.querySelector('[data-picker="level_up_log_channel_id"]'), channels, xp.level_up_log_channel_id, { label: "Level-Up Log Channel" });
    const grantUsers = mountMemberMultiPicker(form.querySelector('[data-picker="xp_grant_allowed_user_ids"]'), members, xp.xp_grant_allowed_user_ids, { label: "Members Who Can Grant XP" });
    const excludedChannels = mountChannelMultiPicker(form.querySelector('[data-picker="xp_excluded_channel_ids"]'), channels, xp.xp_excluded_channel_ids, { label: "Channels That Earn No XP" });

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);

      // Validate before posting so a blank box can't become NaN → null → a
      // bare "422: Input should be a valid number" with no field name.
      const nums = {};
      for (const [name, label, min, max, isInt] of NUM_FIELDS) {
        const raw = String(fd.get(name) ?? "").trim();
        const v = isInt ? parseInt(raw, 10) : parseFloat(raw);
        const bounds = max == null ? `${min} or more` : `between ${min} and ${max}`;
        if (raw === "" || !Number.isFinite(v) || v < min || (max != null && v > max)) {
          showStatus(status, false, `${label} must be a number ${bounds}.`);
          form.querySelector(`[name="${name}"]`).focus();
          return;
        }
        nums[name] = v;
      }

      const lists = {};
      for (const [name, label, count] of LIST_FIELDS) {
        const raw = String(fd.get(name) ?? "").trim();
        const parts = raw.split(",").map((s) => s.trim()).filter((s) => s !== "");
        if (parts.length !== count || parts.some((s) => !Number.isFinite(parseFloat(s)))) {
          showStatus(status, false, `${label} needs exactly ${count} numbers separated by commas.`);
          form.querySelector(`[name="${name}"]`).focus();
          return;
        }
        // Posted as the original comma-separated string, unchanged.
        lists[name] = parts.join(",");
      }

      try {
        await apiPut("/api/config/xp", {
          level_5_role_id: level5Role.getValue(),
          promotion_review_grant_role_id: promotionReviewGrantRole.getValue(),
          level_5_log_channel_id: level5Log.getValue(),
          level_up_log_channel_id: levelUpLog.getValue(),
          xp_grant_allowed_user_ids: grantUsers.getValues(),
          xp_excluded_channel_ids: excludedChannels.getValues(),
          // Algorithm coefficients
          ...nums,
          ...lists,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
