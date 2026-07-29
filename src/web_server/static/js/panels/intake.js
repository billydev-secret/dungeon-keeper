/**
 * Intake — the open-card queue and the settings that shape it, one pane.
 *
 * Formerly "Intake Queue" under Reports → Greeter (moderator) and "Intake
 * Cards" under Config → Members (adminOnly). The queue *is* the output of the
 * step list configured below it, so reading "nobody completes step 4" and
 * editing step 4 belong together.
 *
 * The two settings sections render read-only for non-admins (lockUnlessAdmin);
 * the writes are refused server-side regardless.
 */
import { mountQueue } from "./intake-report.js";
import { mountSettings } from "./intake-settings.js";

export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Intake</h2>
        <div class="subtitle">Open welcome cards, and the procedure they follow</div>
      </header>
      <section data-region="queue"></section>
      <section data-region="settings" style="margin-top:32px;"></section>
    </div>
  `;

  mountQueue(container.querySelector('[data-region="queue"]'), initialParams);
  mountSettings(container.querySelector('[data-region="settings"]'));
}
