import {
  loadConfig, loadChannels, loadRoles,
  mountChannelPicker, mountRolePicker,
  apiPut, showStatus, guardForm, renderMetaWarning, lockUnlessAdmin,
  mountAsync,
} from "../config-helpers.js";
import { mountRoleDialStates } from "../role-dial-state.js";

/**
 * Promotion Reviews — where review cards post, who gets pinged, and what
 * pressing Grant hands out (Moderation → Role Management, id
 * promotion-reviews, adminOnly).
 *
 * These three dials used to ride the XP & Leveling form. Two of the three
 * things that post a review card have nothing to do with XP — a pruned member
 * coming back, and a sleeper waking up — and saving any XP dial rewrote all of
 * them as one payload, which is how the ping role reached prod as 0. This page
 * PUTs only its own three fields; /api/config/xp skips anything it isn't sent.
 * lockUnlessAdmin stays as defense in depth; writes are refused server-side
 * regardless.
 */
export function mount(outer) {
  outer.innerHTML = `
    <div class="panel">
      <header>
        <h2>Promotion Reviews</h2>
        <div class="subtitle">Review cards for members who may be ready for more access</div>
      </header>
      <section data-region="settings"></section>
    </div>
  `;
  return mountSettings(outer.querySelector('[data-region="settings"]'));
}

export function mountSettings(container) {
  container.innerHTML = `<div class="empty">Loading promotion review settings…</div>`;

  return mountAsync(container, async () => {
    const [config, channels, roles] = await Promise.all([
      loadConfig(), loadChannels(), loadRoles(),
    ]);
    const xp = config.xp;

    container.innerHTML = `
      <div>
        <div class="section-label">Settings</div>
        <div class="field-hint">
          A review card posts when a member reaches level 5, when a member who
          was pruned for inactivity comes back, or when a quiet member starts
          talking again. Each card carries a <strong>Grant access</strong>
          button — pressing it hands over the role below and writes a line to
          Grant Audit.
        </div>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Where Cards Post</div>
            <div class="field">
              <label>Promotion Reviews Channel</label>
              <div data-picker="level_5_log_channel_id"></div>
              <div class="field-hint">Every review card posts here. "(disabled)" posts none of them, and nobody is told a member is up for review.</div>
              <div class="field-hint">This is also where level-up notices for level 5 itself land, so pick a channel your role managers read.</div>
            </div>
            <div class="field">
              <label>Ping Role</label>
              <div data-picker="promotion_review_ping_role_id"></div>
              <div class="field-hint">Pinged when a card posts, so your role managers know someone is waiting. Choose "(none)" and the cards ping your moderator roles instead — set a role here if you'd rather they didn't.</div>
              <div data-role-state="promotion_review_ping_role_id"></div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">What Grant Hands Out</div>
            <div class="field">
              <label>Grant Role</label>
              <div data-picker="promotion_review_grant_role_id"></div>
              <div class="field-hint">Given <em>to</em> the member when someone presses Grant access — typically your NSFW-access role. Also what the level 5 card's <strong>Spicy access</strong> line reports on.</div>
              <div class="field-hint">Choose "(none)" and Grant has nothing to give — the Spicy access line then falls back to a role named <code>nsfw</code>, or drops off the card if there isn't one. Pressing Grant itself is open to admins, mods, and anyone with Manage Roles.</div>
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

    const reviewsChannel = mountChannelPicker(form.querySelector('[data-picker="level_5_log_channel_id"]'), channels, xp.level_5_log_channel_id, { label: "Promotion Reviews Channel" });
    const pingRole = mountRolePicker(form.querySelector('[data-picker="promotion_review_ping_role_id"]'), roles, xp.promotion_review_ping_role_id, { label: "Ping Role" });
    const grantRole = mountRolePicker(form.querySelector('[data-picker="promotion_review_grant_role_id"]'), roles, xp.promotion_review_grant_role_id, { label: "Grant Role" });

    // Lock *after* the pickers mount — each builds its own inputs, and locking
    // first would leave those live. A moderator sees the real saved values
    // (GET /api/config is moderator-gated) but cannot submit; the write itself
    // is still refused server-side by require_perms({"admin"}).
    if (lockUnlessAdmin(container)) return;

    guardForm(form);
    mountRoleDialStates(container);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        // Only these three keys. /api/config/xp leaves every field it isn't
        // sent alone, so saving here cannot disturb an XP dial — and saving on
        // XP & Leveling can no longer flatten these.
        await apiPut("/api/config/xp", {
          level_5_log_channel_id: reviewsChannel.getValue(),
          promotion_review_ping_role_id: pingRole.getValue(),
          promotion_review_grant_role_id: grantRole.getValue(),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }, { errorMsg: "Couldn’t load the promotion review settings." });
}
