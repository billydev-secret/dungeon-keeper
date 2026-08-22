// Single source of truth for the dashboard's Help navigation and the help
// panel's page→manual-section mapping.
//
// `page`   — dashboard hash-route id (#/<page>)
// `anchor` — heading id in /static/manual.html (h2 or h3; h3 anchors render
//            just that subsection — see extractSectionContent in help.js)
// `label`  — nav + panel title
//
// app.js builds the sidebar "Help" section from HELP_GROUPS; help.js resolves
// routes via HELP_PAGES. Add new manual sections here (and only here) — a
// page whose anchor is missing from manual.html shows a "not found" error in
// the panel, so drift is visible instead of silent.

// The assistant's name is per-guild branding (Config → Branding). "Billy-bot"
// is only the built-in default, so it must not be hardcoded into a label —
// app.js (nav) and help.js (panel title) both re-label the `brand:"assistant"`
// entry once /api/help/advisor/name resolves, and fall back to this default if
// it doesn't.
export const DEFAULT_ASSISTANT_NAME = "Billy-bot";

/** Nav/panel label for the assistant's help page, for a given guild's name. */
export function assistantHelpLabel(name) {
  return `Ask ${name || DEFAULT_ASSISTANT_NAME} (AI)`;
}

export const HELP_GROUPS = [
  { heading: null, items: [
    // Nav items alphabetize within their group by default; `order` pins these
    // onboarding entries to a deliberate reading order (Getting Started first)
    // since it wouldn't otherwise sort ahead of "Ask …".
    { page: "help-start",    anchor: "getting-started",   label: "Getting Started",       order: 1 },
    { page: "help-overview", anchor: "functional-blocks", label: "Feature Map",           order: 2 },
    // `label` here is the fallback only — see assistantHelpLabel above. It must
    // keep matching the manual's own <h3 id="ask-guide"> text so help.js's
    // dropDuplicateHeading still removes the duplicate title.
    { page: "help-ask",      anchor: "ask-guide",         label: assistantHelpLabel(),    order: 3,
      brand: "assistant", keywords: "ask ai assistant advisor billy billy-bot" },
  ]},
  // Groups run audience-first — members, then moderators, then admins —
  // mirroring the manual's section order.
  //
  // Keep each `label` identical to the manual heading its `anchor` points at:
  // the help panel prints the label as the page title and suppresses the
  // manual's own heading only when the two match (help.js dropDuplicateHeading),
  // so drift here means users see two slightly different titles per page.
  // `page` ids are routed from panel headers — rename labels, never ids.
  { heading: "Games & Social", items: [
    { page: "help-casino",      anchor: "economy-casino",  label: "Casino" },
    { page: "help-pools",       anchor: "pools",           label: "Pools" },
    { page: "help-games",       anchor: "games",           label: "Games Night" },
    { page: "help-guess",       anchor: "guess",           label: "Guess Who" },
    { page: "help-photo",       anchor: "photo-challenge", label: "Photo Challenge" },
    { page: "help-survivor",    anchor: "survivor",        label: "Survivor",
      keywords: "nfl football pickem survival pool gauntlet ghost reckoning" },
    { page: "help-mahjong",     anchor: "mahjong",         label: "Meadow Mahjong",
      keywords: "mahjong tiles charleston joker duel stakes card meadow" },
    { page: "help-whisper",     anchor: "whisper",         label: "Whisper" },
    { page: "help-confessions", anchor: "confessions",     label: "Confessions" },
    { page: "help-pen-pals",    anchor: "pen-pals",        label: "Pen Pals" },
  ]},
  { heading: "Member Tools", items: [
    { page: "help-community", anchor: "community",     label: "Community & XP" },
    { page: "help-economy",   anchor: "economy",       label: "Economy & Perk Shop" },
    { page: "help-bios",      anchor: "bios",          label: "Member Bios" },
    { page: "help-emoji",     anchor: "emoji-stealer", label: "Emoji Stealer" },
    { page: "help-wellness",  anchor: "wellness",      label: "Wellness" },
    { page: "help-dms",       anchor: "dm-perms",      label: "DM Permissions" },
    { page: "help-no-contact", anchor: "no-contact",   label: "No-Contact List" },
    { page: "help-info",      anchor: "info",          label: "Your Info Card" },
    { page: "help-self",      anchor: "self-service",  label: "Member Self-Service" },
    { page: "help-your-data", anchor: "your-data",     label: "Your Data & Privacy" },
    { page: "help-privacy",   anchor: "privacy",       label: "Data Erasure" },
  ]},
  { heading: "Voice & Music", items: [
    { page: "help-voice", anchor: "voice",     label: "Voice Control" },
    { page: "help-music", anchor: "music",     label: "Music" },
    { page: "help-music-playlist", anchor: "music-playlist", label: "Music Playlist",
      keywords: "spotify rolling playlist songs tracks watched channel youtube links review unmatched window" },
  ]},
  { heading: "Moderation", items: [
    { page: "help-moderation",  anchor: "moderation",  label: "Moderation Core" },
    { page: "help-jail",        anchor: "jail",        label: "Jail & Release" },
    { page: "help-tickets",     anchor: "tickets",     label: "Tickets, Policies & Warnings" },
    { page: "help-policies",    anchor: "policies",    label: "Policy Voting" },
    { page: "help-analytics",   anchor: "analytics",   label: "Analytics & Watch List" },
    { page: "help-threads-metrics", anchor: "threads-in-metrics", label: "Threads and Channel Metrics" },
    { page: "help-bots-metrics", anchor: "bots-in-metrics", label: "Bots and Metrics" },
    { page: "help-usage-telemetry", anchor: "usage-telemetry", label: "Command & Panel Usage" },
    { page: "help-ai",          anchor: "ai-tools",    label: "AI Moderation Tools" },
    { page: "help-rules-watch", anchor: "rules-watch", label: "Rules Watch" },
    { page: "help-todo",        anchor: "todo",        label: "Todo List & Boards" },
  ]},
  { heading: "Server Admin", items: [
    { page: "help-setup",          anchor: "setup",             label: "Setup & Permissions" },
    { page: "help-announcements",  anchor: "announcements",     label: "Announcements" },
    { page: "help-role-menus",     anchor: "role-menus",        label: "Role Menus" },
    { page: "help-docs",           anchor: "docs",              label: "Docs" },
    { page: "help-config",         anchor: "config",            label: "Configuration Reference" },
    { page: "help-cleanup",        anchor: "server-ops",        label: "Server Upkeep" },
    { page: "help-chat-revive",    anchor: "chat-revive",       label: "Chat Revive" },
    { page: "help-event-echo",     anchor: "event-echo",        label: "Event Echo" },
    { page: "help-greeting-watch", anchor: "greeting-watch",    label: "Greeting Watch" },
    { page: "help-intake",         anchor: "intake",            label: "Intake Cards" },
    { page: "help-hidden",         anchor: "hidden-channels",   label: "Hidden Channels" },
    { page: "help-network",        anchor: "network-analytics", label: "Network Analytics" },
  ]},
];

// Help pages routed from elsewhere in the nav (Home / Dev sections) — they
// need a route mapping but must not appear twice in the sidebar.
export const HELP_EXTRA_PAGES = [
  { page: "help-quickref", anchor: "quickref",    label: "Quick Reference" },
  { page: "help-owner",    anchor: "owner-tools", label: "Developer / Owner Tools" },
  { page: "help-qa",       anchor: "qa-tracker",  label: "QA Tracker" },
];

export const HELP_PAGES = [
  ...HELP_GROUPS.flatMap((g) => g.items),
  ...HELP_EXTRA_PAGES,
];
