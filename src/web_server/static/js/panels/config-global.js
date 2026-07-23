import { api, esc } from "../api.js";
import {
  loadConfig,
  loadChannels,
  loadRoles,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  mountRoleMultiPicker,
} from "../config-helpers.js";

// A Discord user id is a snowflake: 17–20 digits, no other characters.
const SNOWFLAKE_RE = /^\d{17,20}$/;

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels, roles, supportResp] = await Promise.all([
      loadConfig(), loadChannels(), loadRoles(),
      api("/api/config/support-access").catch(() => ({ enabled: false })),
    ]);
    const g = config.global;

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Global Settings</h2>
          <div class="subtitle">Server-wide basics every other Dungeon Keeper feature builds on</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Time &amp; Reporting</div>
            <div class="field">
              <label for="cg-tz">Time Zone Offset (hours from UTC)</label>
              <input type="number" step="0.5" min="-12" max="14" required
                name="tz_offset_hours" id="cg-tz" value="${esc(g.tz_offset_hours)}"
                style="max-width:140px;" />
              <div class="field-hint">Sets the day boundary Dungeon Keeper uses for
                daily quests, streaks, digests, and every "today" figure in reports.
                Enter hours from UTC — for example -5 for US Eastern time, 1 for
                Central European time. Half-hour zones are allowed (5.5 for India).</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Staff Channel</div>
            <div class="field">
              <label>Moderator Channel</label>
              <span data-picker="mod_channel_id"></span>
              <div class="field-hint">The fallback channel for staff-facing notices
                that have no channel of their own. "(disabled)" means those notices
                are not posted anywhere.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Exemptions</div>
            <div class="field">
              <label>Bypass Roles</label>
              <span data-picker="bypass_role_ids"></span>
              <div class="field-hint">Members holding any of these roles skip the
                spoiler guard and the other automatic content restrictions. Give this
                to trusted staff only — it removes protections rather than adding
                powers.</div>
            </div>
            <div class="field">
              <label for="cg-bots">Recorded Bot User IDs</label>
              <input type="text" name="recorded_bot_user_ids" id="cg-bots"
                value="${esc((g.recorded_bot_user_ids || []).join(", "))}"
                placeholder="e.g. 123456789012345678, 234567890123456789" />
              <div class="field-hint">Bot accounts whose messages Dungeon Keeper
                stores anyway — for example a dice roller whose results you want kept
                in transcripts. Enter Discord user IDs separated by commas (right-click
                the bot with Developer Mode on and choose "Copy User ID"). These bots
                still earn no XP and never trigger wellness or moderation checks.
                Leave empty to store no bot messages.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Server File Paths</div>
            <div class="field">
              <label for="cg-swatch">Booster Swatch Directory</label>
              <input type="text" name="booster_swatch_dir" id="cg-swatch"
                value="${esc(g.booster_swatch_dir || "")}"
                placeholder="e.g. /srv/dungeon-keeper/swatches" />
              <div class="field-hint">A folder path on the machine running Dungeon
                Keeper (not on your computer, and not a link) holding the booster
                color swatch images. Leave empty to use the built-in swatches. A path
                that does not exist means boosters see no swatch preview.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <section class="form" style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid var(--border,#333)">
          <h3 style="margin:0 0 0.25rem">Support Access</h3>
          <div class="field-hint" style="margin-bottom:1rem">Lets the Dungeon Keeper
            developer open this server's dashboard to troubleshoot a problem or help
            with setup. While it is on, they can see everything a server admin can see
            here. Turn it off again as soon as you are done — it takes effect
            immediately.</div>
          <div style="display:flex;align-items:center;gap:12px;">
            <label style="margin:0;cursor:pointer;display:flex;align-items:center;gap:8px;">
              <input type="checkbox" data-support-toggle ${supportResp.enabled ? "checked" : ""} />
              Allow the Dungeon Keeper developer to access this dashboard
            </label>
            <span data-support-status></span>
          </div>
        </section>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    const modChannelPicker = mountChannelPicker(
      form.querySelector('[data-picker="mod_channel_id"]'),
      channels, String(g.mod_channel_id || "0"), { label: "Moderator Channel" },
    );
    const bypassRolesPicker = mountRoleMultiPicker(
      form.querySelector('[data-picker="bypass_role_ids"]'),
      roles, g.bypass_role_ids, { label: "Bypass Roles" },
    );

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);

      const tz = parseFloat(fd.get("tz_offset_hours"));
      if (!Number.isFinite(tz) || tz < -12 || tz > 14) {
        showStatus(status, false, "Time Zone Offset must be a number from -12 to 14");
        form.querySelector("[name=tz_offset_hours]").focus();
        return;
      }

      const botIds = String(fd.get("recorded_bot_user_ids") || "")
        .split(",").map((s) => s.trim()).filter(Boolean);
      const badId = botIds.find((id) => !SNOWFLAKE_RE.test(id));
      if (badId) {
        showStatus(status, false,
          `Recorded Bot User IDs: "${badId}" is not a Discord user ID (17–20 digits)`);
        form.querySelector("[name=recorded_bot_user_ids]").focus();
        return;
      }

      try {
        await apiPut("/api/config/global", {
          tz_offset_hours: tz,
          // Snowflakes stay strings — parseInt rounds 19-digit ids.
          mod_channel_id: modChannelPicker.getValue() || "0",
          bypass_role_ids: bypassRolesPicker.getValues(),
          recorded_bot_user_ids: botIds,
          booster_swatch_dir: fd.get("booster_swatch_dir"),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    const supportToggle = container.querySelector("[data-support-toggle]");
    const supportStatus = container.querySelector("[data-support-status]");
    supportToggle.addEventListener("change", async () => {
      try {
        await apiPut("/api/config/support-access", { enabled: supportToggle.checked });
        showStatus(supportStatus, true, supportToggle.checked ? "Access granted" : "Access revoked");
      } catch (err) {
        supportToggle.checked = !supportToggle.checked;
        showStatus(supportStatus, false, err.message);
      }
    });
  })();
}
