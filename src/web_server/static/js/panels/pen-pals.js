/**
 * Pen Pals — its settings and its question bank, one pane.
 *
 * Formerly two nav entries under a "Pen Pals" subgroup: "Config" (adminOnly)
 * and "Questions" (game-host level). That split cut one feature across two
 * permission levels, so a moderator who curated the conversation starters
 * couldn't set the channel they get posted in. The settings writes are now
 * moderator-level too (see routes/config.py), which is what lets both halves
 * sit on one moderator-reachable page with no in-page locking.
 *
 * Settings lead because they gate everything: while Pen Pals is off, nothing
 * in the bank is ever served, and the settings half says so in its own banner.
 */
import { mountSettings } from "./pen-pals-settings.js";
import { mountPoolActivity } from "./pen-pals-pool-activity.js";
import { mountGamePanel } from "./games-panel-shared.js";

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>🖊️ Pen Pals</h2>
        <div class="subtitle">Private one-day channels that match two members and give them something to talk about</div>
      </header>
      <section data-region="settings"></section>
      <section data-region="pool" style="margin-top:32px;"></section>
      <section data-region="questions" style="margin-top:32px;"></section>
    </div>
  `;

  mountSettings(container.querySelector('[data-region="settings"]'));
  mountPoolActivity(container.querySelector('[data-region="pool"]'));

  const questions = container.querySelector('[data-region="questions"]');
  questions.innerHTML = `<div class="section-label">Questions</div>`;
  const bankSlot = document.createElement("div");
  questions.appendChild(bankSlot);
  mountGamePanel(bankSlot, {
    gameType: "pen_pals",
    gameName: "Pen Pals",
    gameIcon: "🖊️",
    hasBank: true,
    hasStatus: false,
    bare: true,
    bankHint:
      "Conversation starters posted in pen pal channels. Tag adult prompts <strong>nsfw</strong> — they are only served when the Pen Pals settings above include NSFW, and pen pal channels are then created age-restricted.",
  });
}
