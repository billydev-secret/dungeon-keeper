/**
 * XP & Leveling — the leaderboard and the settings that produce it, one pane.
 *
 * Formerly two pages: "XP Leaderboard" under Reports → Engagement (moderator)
 * and "XP & Leveling" under Config → Members (adminOnly), cross-linked to each
 * other by `related:` chips. They stayed split because they have different
 * audiences, not because the split helped anyone: reading a level curve and
 * seeing its effect on the level distribution is one question.
 *
 * The permission difference is handled inside the page instead of by hiding
 * half of it. GET /api/config is moderator-gated, so a moderator sees the real
 * settings values; every config write is admin-gated, so the settings section
 * is rendered read-only for them (lockUnlessAdmin). Enforcement is still the
 * server's — this only stops filling in a form whose save could never land.
 */
import { mountLeaderboard } from "./xp-leaderboard.js";
import { mountSettings } from "./xp-settings.js";

export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>XP &amp; Leveling</h2>
        <div class="subtitle">Who is earning XP, and the rules that decide how</div>
      </header>
      <section data-region="leaderboard"></section>
      <section data-region="settings" style="margin-top:32px;"></section>
    </div>
  `;

  const board = mountLeaderboard(
    container.querySelector('[data-region="leaderboard"]'),
    initialParams,
  );
  mountSettings(container.querySelector('[data-region="settings"]'));

  return {
    unmount() {
      board?.unmount?.();
    },
  };
}
