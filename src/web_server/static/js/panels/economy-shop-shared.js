/**
 * Helpers shared by the three pages the perk shop was split into:
 *
 *   pricing.js        the priced dials — one form, one Save, admin only
 *   economy-sinks.js  the catalogs an admin curates (icons, palette, items)
 *   shop-approvals.js the two work queues (emoji submissions, orders)
 *
 * They were one 1,339-line page. Splitting them meant this handful of things
 * needed a home that none of the three owns, rather than one page importing
 * from another and quietly becoming its dependency.
 *
 * Distinct from `economy-sources-shared.js`, which mirrors the income-side
 * trigger labels for the Quests and Income Sources pages.
 */

/** Upper bound for any coin-valued input; also the client-side check's ceiling. */
export const DEFAULT_MAX = 100000000;

/**
 * The economy master switch lives on Economy Settings. With it off, nothing on
 * any of the three shop pages has any effect — say so instead of letting an
 * admin price a shop nobody can open (W-C6).
 */
export function economyOffBanner(cfg) {
  if (cfg && cfg.enabled) return "";
  return `<div class="empty" role="status" style="margin-bottom:12px;">
    The economy is currently off, so nothing below takes effect until it is switched
    on under <a href="#/economy-config">Economy Settings</a>.</div>`;
}

/**
 * The three pages cross-reference each other's numbers — a palette row priced 0
 * falls through to the flat Palette Color dial, and the icon catalog prices
 * override the Custom Role Icon dial. Those hints used to say "above" and
 * "further down" because everything was one scroll; now they have to be links.
 */
export function crossLink(pageId, label) {
  return `<a href="#/${pageId}">${label}</a>`;
}
