/**
 * Policy Tickets — the proposals and the voting deadline that governs them,
 * one pane.
 *
 * Formerly "Policy Tickets" under Moderation (moderator) and "Policy Ticket
 * Settings" under Config → Moderation & Safety (adminOnly), already
 * cross-linked by `related:` chips. The settings half is a single field — the
 * voting deadline — which never justified a page of its own.
 *
 * That one field renders read-only for non-admins (lockUnlessAdmin); the write
 * is refused server-side regardless.
 */
import { mountTickets } from "./mod-policy-tickets.js";
import { mountBallots } from "./policy-ballots.js";
import { mountSettings } from "./policy-tickets-settings.js";

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Policy Tickets</h2>
        <div class="subtitle">Proposed policy changes, how the mod team and the community voted, and how long a vote runs</div>
      </header>
      <section data-region="tickets"></section>
      <section data-region="ballots" style="margin-top:32px;"></section>
      <section data-region="settings" style="margin-top:32px;"></section>
    </div>
  `;

  // mountTickets arms a 45s refresh poll and hands back the handle that clears
  // it. Dropping that handle left one poll per visit running forever — forward
  // it, the way the queue pages do. mountBallots polls on the same cadence and
  // is forwarded for the same reason.
  const tickets = mountTickets(container.querySelector('[data-region="tickets"]'));
  const ballots = mountBallots(container.querySelector('[data-region="ballots"]'));
  mountSettings(container.querySelector('[data-region="settings"]'));

  return {
    unmount() {
      tickets?.unmount?.();
      ballots?.unmount?.();
    },
  };
}
