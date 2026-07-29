/**
 * Birthdays — the upcoming-birthday calendar and the announcement settings
 * behind it, one pane.
 *
 * Formerly "Birthday Calendar" under Reports → Member Lists (moderator) and
 * "Birthdays" under Config → Members (adminOnly), already cross-linked by a
 * `related:` chip. Seeing whose birthday lands tomorrow and checking which
 * channel it will be announced in is one question.
 *
 * The settings half renders read-only for non-admins (lockUnlessAdmin); the
 * writes themselves are refused server-side regardless.
 */
import { mountCalendar } from "./birthday-calendar.js";
import { mountSettings } from "./birthday-settings.js";

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Birthdays</h2>
        <div class="subtitle">Who's coming up, and how the bot announces them</div>
      </header>
      <section data-region="calendar"></section>
      <section data-region="settings" style="margin-top:32px;"></section>
    </div>
  `;

  mountCalendar(container.querySelector('[data-region="calendar"]'));
  mountSettings(container.querySelector('[data-region="settings"]'));
}
