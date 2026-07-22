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

export const HELP_GROUPS = [
  { heading: null, items: [
    // Nav items are alphabetized within their group; these labels are chosen
    // so Getting Started sorts first.
    { page: "help-start",    anchor: "getting-started",   label: "Getting Started" },
    { page: "help-ask",      anchor: "ask-guide",         label: "Ask Billy-bot (AI)" },
    { page: "help-overview", anchor: "functional-blocks", label: "Overview (Feature Map)" },
  ]},
  // Groups run audience-first — members, then moderators, then admins —
  // mirroring the manual's section order. (Items alphabetize within a group
  // in the sidebar, so only group membership/order matters here.)
  { heading: "Games & Social", items: [
    { page: "help-casino",      anchor: "economy-casino",  label: "Casino" },
    { page: "help-games",       anchor: "games",           label: "Games Night" },
    { page: "help-guess",       anchor: "guess",           label: "Guess Who" },
    { page: "help-photo",       anchor: "photo-challenge", label: "Photo Challenge" },
    { page: "help-whisper",     anchor: "whisper",         label: "Whisper" },
    { page: "help-confessions", anchor: "confessions",     label: "Confessions" },
    { page: "help-pen-pals",    anchor: "pen-pals",        label: "Pen Pals" },
  ]},
  { heading: "Member Tools", items: [
    { page: "help-community", anchor: "community",     label: "Community & XP" },
    { page: "help-economy",   anchor: "economy",       label: "Economy & Perk Shop" },
    { page: "help-bios",      anchor: "bios",          label: "Bios" },
    { page: "help-emoji",     anchor: "emoji-stealer", label: "Emoji Stealer" },
    { page: "help-wellness",  anchor: "wellness",      label: "Wellness" },
    { page: "help-dms",       anchor: "dm-perms",      label: "DM Permissions" },
    { page: "help-self",      anchor: "self-service",  label: "Member Self-Service" },
    { page: "help-privacy",   anchor: "privacy",       label: "Data Erasure" },
  ]},
  { heading: "Voice & Music", items: [
    { page: "help-voice", anchor: "voice",     label: "Voice Channels" },
    { page: "help-music", anchor: "music",     label: "Music" },
    { page: "help-247",   anchor: "music-247", label: "24/7 Mode" },
  ]},
  { heading: "Moderation", items: [
    { page: "help-moderation",  anchor: "moderation",  label: "Moderation Core" },
    { page: "help-jail",        anchor: "jail",        label: "Jail & Release" },
    { page: "help-tickets",     anchor: "tickets",     label: "Tickets, Policies & Warnings" },
    { page: "help-policies",    anchor: "policies",    label: "Policy Voting" },
    { page: "help-analytics",   anchor: "analytics",   label: "Analytics & Watch" },
    { page: "help-ai",          anchor: "ai-tools",    label: "AI Moderation" },
    { page: "help-rules-watch", anchor: "rules-watch", label: "Rules Watch" },
  ]},
  { heading: "Server Admin", items: [
    { page: "help-setup",          anchor: "setup",             label: "Setup & Permissions" },
    { page: "help-announcements",  anchor: "announcements",     label: "Announcements" },
    { page: "help-role-menus",     anchor: "role-menus",        label: "Role Menus" },
    { page: "help-config",         anchor: "config",            label: "Configuration" },
    { page: "help-cleanup",        anchor: "server-ops",        label: "Server Upkeep" },
    { page: "help-chat-revive",    anchor: "chat-revive",       label: "Chat Revive" },
    { page: "help-greeting-watch", anchor: "greeting-watch",    label: "Greeting Watch" },
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
