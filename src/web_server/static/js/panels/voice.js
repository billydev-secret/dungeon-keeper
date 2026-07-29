/**
 * Voice — channel usage and the Voice Control settings behind it, one pane.
 *
 * Formerly "Voice Activity" under Reports → Engagement (moderator) and "Voice
 * Control" under Config → Voice (adminOnly). Peak-hours usage is the evidence
 * for the hub/limit settings underneath it.
 *
 * The settings half renders read-only for non-admins (lockUnlessAdmin); the
 * writes are refused server-side regardless.
 */
import { mountActivity } from "./voice-activity.js";
import { mountSettings } from "./voice-settings.js";

export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Voice</h2>
        <div class="subtitle">How voice channels are used, and how member-owned rooms behave</div>
      </header>
      <section data-region="activity"></section>
      <section data-region="settings" style="margin-top:32px;"></section>
    </div>
  `;

  const activity = mountActivity(container.querySelector('[data-region="activity"]'), initialParams);
  mountSettings(container.querySelector('[data-region="settings"]'));

  return {
    unmount() {
      activity?.unmount?.();
    },
  };
}
