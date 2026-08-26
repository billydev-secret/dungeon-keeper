/**
 * The ten section icons for the nav rail.
 *
 * These replace a set of Unicode dingbats — ⌂ ▤ ⚖ ⚙ ¤ ♥ ⚄ ☺ ⚒ ? — that came
 * from nine different Unicode blocks. They were drawn by different people, for
 * different purposes, decades apart: a house, a die, a currency sign and a
 * smiley will never look like a family. They also rendered at different weights
 * on every platform, and they fall outside the latin subsets the dashboard
 * ships, so they came out of a system fallback rather than the page's own type.
 *
 * Every icon here is drawn on one 16x16 grid at a 1.5 stroke with round joins,
 * uses currentColor so the rail's hover/active/gold states work unchanged, and
 * is chosen to say what the section actually holds rather than to be decorative:
 *
 *   home       a 2x2 tile grid   — an overview made of cards, which is what it is
 *   reports    ascending bars    — the section is charts and distributions
 *   moderation a shield          — protection, not punishment; DK's mod surface is
 *                                  no-contact lists and safety gates as much as bans
 *   config     two sliders       — this codebase calls settings "dials"; so does the UI
 *   economy    a stack of coins  — currency, and stacked reads as a balance
 *   wellness   a sprout          — growth rather than a medical cross, and it nods at
 *                                  the Golden Meadow without being twee
 *   games      a die             — the one dingbat that was already right, drawn properly
 *   social     two linked nodes  — the section literally contains a Social Graph
 *   dev        code brackets     — unambiguous, and it matches the technical register
 *   help       a question mark   — conventional, and convention is the point here
 *
 * Keyed by section id rather than edited into app.js's SECTIONS array, so this
 * file never collides with work that adds or regroups nav entries.
 */

const A = 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
  + 'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"';

const ICONS = {
  // Four tiles: an overview assembled from cards.
  home: `<svg ${A}><rect x="2.25" y="2.25" width="5" height="5" rx="1.2"/><rect x="8.75" y="2.25" width="5" height="5" rx="1.2"/><rect x="2.25" y="8.75" width="5" height="5" rx="1.2"/><rect x="8.75" y="8.75" width="5" height="5" rx="1.2"/></svg>`,

  // Bars on a baseline, deliberately uneven — a distribution, not a logo.
  reports: `<svg ${A}><path d="M2.5 13.5h11"/><path d="M5 13.5V9.5"/><path d="M8 13.5V5.5"/><path d="M11 13.5V7.75"/></svg>`,

  // A shield: this section protects people.
  moderation: `<svg ${A}><path d="M8 2.2l5 2v4c0 3-2.1 5.1-5 5.9-2.9-.8-5-2.9-5-5.9v-4l5-2z"/></svg>`,

  // Two rails with knobs at different positions — dials, set differently.
  config: `<svg ${A}><path d="M2.5 5.25h11"/><path d="M2.5 10.75h11"/><circle cx="6" cy="5.25" r="1.75"/><circle cx="10.5" cy="10.75" r="1.75"/></svg>`,

  // Two overlapping coins. A vertical cylinder — the obvious "stack of coins"
  // drawing — is also the universal database icon, and at 15px in a rail with
  // no label beside it that is exactly what it read as.
  economy: `<svg ${A}><circle cx="6.05" cy="9.95" r="4.05"/><circle cx="9.95" cy="6.05" r="4.05"/></svg>`,

  // A sprout: growth, and a nod to the Meadow.
  wellness: `<svg ${A}><path d="M8 13.75V6.5"/><path d="M8 9.25C5.4 9.25 3.75 7.6 3.75 5 6.35 5 8 6.65 8 9.25z"/><path d="M8 7.75c2.6 0 4.25-1.65 4.25-4.25C9.65 3.5 8 5.15 8 7.75z"/></svg>`,

  // The die that was always right, drawn on the grid.
  games: `<svg ${A}><rect x="2.5" y="2.5" width="11" height="11" rx="2.5"/><circle cx="5.6" cy="5.6" r=".95" fill="currentColor" stroke="none"/><circle cx="8" cy="8" r=".95" fill="currentColor" stroke="none"/><circle cx="10.4" cy="10.4" r=".95" fill="currentColor" stroke="none"/></svg>`,

  // Two nodes and the edge between them.
  social: `<svg ${A}><circle cx="4.4" cy="11.6" r="2.15"/><circle cx="11.6" cy="4.4" r="2.15"/><path d="M6.1 9.9l3.8-3.8"/></svg>`,

  // Code brackets.
  dev: `<svg ${A}><path d="M5.6 4.4L2 8l3.6 3.6"/><path d="M10.4 4.4L14 8l-3.6 3.6"/></svg>`,

  // A question mark, because convention is the useful thing here.
  help: `<svg ${A}><circle cx="8" cy="8" r="5.85"/><path d="M6.35 6.4a1.7 1.7 0 113.3.55c0 1.15-1.65 1.35-1.65 2.55"/><circle cx="8" cy="11.55" r=".85" fill="currentColor" stroke="none"/></svg>`,
};

/**
 * SVG markup for a section, or null when there is none — callers fall back to
 * whatever `icon` the section declares, so an unmapped or newly added section
 * degrades to its old glyph instead of rendering blank.
 */
export function sectionIcon(sectionId) {
  return Object.prototype.hasOwnProperty.call(ICONS, sectionId) ? ICONS[sectionId] : null;
}

/** Every id this module draws — used by the test that keeps the set complete. */
export function iconIds() {
  return Object.keys(ICONS);
}

// Parsed once per section, cloned per use. renderNav rebuilds the whole rail on
// every navigation and guild switch, and there are ~176 items, so setting
// innerHTML per item meant ~176 HTML-fragment parses each time — for markup the
// expanded rail then hides. Ten parses and 176 clones instead.
const _nodes = new Map();

/**
 * A detached SVG element for a section, or null when there is none.
 * Always returns a fresh clone, so callers can append it without sharing a node.
 */
export function sectionIconNode(sectionId) {
  const markup = sectionIcon(sectionId);
  if (!markup) return null;
  let tpl = _nodes.get(sectionId);
  if (!tpl) {
    const holder = document.createElement("template");
    holder.innerHTML = markup;
    tpl = holder.content.firstElementChild;
    _nodes.set(sectionId, tpl);
  }
  return tpl.cloneNode(true);
}
