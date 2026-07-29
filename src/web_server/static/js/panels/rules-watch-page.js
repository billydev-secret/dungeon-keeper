/**
 * Rules Watch — the alert queue and the settings that feed it, one pane.
 *
 * Formerly "Rules Watch" under Moderation (moderator) and a same-named config
 * page under Config → Moderation & Safety (adminOnly), already cross-linked by
 * `related:` chips — two nav entries with the identical label, which is the
 * clearest possible sign the split was noise.
 *
 * The settings half renders read-only for non-admins (lockUnlessAdmin); the
 * writes are refused server-side regardless.
 */
import { mountQueue } from "./rules-watch.js";
import { mountSettings } from "./rules-watch-settings.js";

export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Rules Watch</h2>
        <div class="subtitle">What the AI flagged, and what it is told to watch for</div>
      </header>
      <section data-region="queue"></section>
      <section data-region="settings" style="margin-top:32px;"></section>
    </div>
  `;

  const queue = mountQueue(container.querySelector('[data-region="queue"]'), initialParams);
  mountSettings(container.querySelector('[data-region="settings"]'));

  return {
    unmount() {
      queue?.unmount?.();
    },
  };
}
