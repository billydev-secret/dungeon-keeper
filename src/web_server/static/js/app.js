// Dashboard boot + hash-based panel router.
import { api, esc } from "./api.js";
import { toast } from "./ui.js";
import { HELP_GROUPS, HELP_EXTRA_PAGES } from "./panels/help-sections.js?v=24";


// The Help nav is generated from help-sections.js (single source shared with
// the help panel) so nav entries can't drift from the manual's sections.
const _helpNavItem = ({ page, label, order }) => ({ id: page, label, order, module: "./panels/help.js" });
const HELP_NAV_SECTION = {
  id: "help", label: "Help", perms: [], icon: "?",
  items: HELP_GROUPS.filter((g) => !g.heading).flatMap((g) => g.items.map(_helpNavItem)),
  groups: HELP_GROUPS.filter((g) => g.heading).map((g) => ({
    heading: g.heading,
    items: g.items.map(_helpNavItem),
  })),
};

// ── Section definitions ─────────────────────────────────────────────
//
// Optional per-item fields beyond id/label/module:
//   adminOnly  — admins only; rendered as a locked (disabled) entry for
//                moderators, hidden for everyone else.
//   perms      — explicit permission list. Items with an explicit `perms`
//                the user satisfies stay visible even when the section's own
//                gate fails (e.g. moderator-level game configs inside the
//                host-gated Games section).
//   keywords   — extra nav-filter search terms (synonyms, old names).
//   help       — help-page id; renders a "?" link in the panel header row.
//   related    — page ids cross-linked in the panel header row.
//   primaryOnly— hidden on non-primary guilds (bot-global settings).
//   gt         — games-studio game-type query param.

const SECTIONS = [
  {
    id: "home", label: "Dashboard", perms: [], icon: "⌂",
    items: [
      { id: "home", label: "Dashboard", module: "./panels/home.js" },
      { id: "help-quickref", label: "Quick Reference", module: "./panels/help.js" },
    ],
  },
  {
    id: "reports", label: "Reports", perms: ["moderator"], icon: "▤",
    groups: [
      { heading: "Moderation", items: [
        { id: "health-sentiment",       label: "Sentiment & Tone",  module: "./panels/health-sentiment.js" },
        { id: "health-sentiment-feed",  label: "Sentiment Feed",    module: "./panels/health-sentiment-feed.js" },
        { id: "health-message-feed",   label: "Message Feed",       module: "./panels/health-message-feed.js" },
        { id: "health-mod-workload",    label: "Mod Workload",       module: "./panels/health-mod-workload.js" },
        { id: "health-mod-engagement",  label: "Mod Engagement",     module: "./panels/health-mod-engagement.js" },
      ]},
      { heading: "General", items: [
        { id: "health-heatmap",         label: "Activity Heatmap",   module: "./panels/health-heatmap.js" },
        { id: "health-channel-health",  label: "Channel Health",     module: "./panels/health-channel-health.js" },
        { id: "health-composite-score", label: "Health Score",       module: "./panels/health-composite-score.js", keywords: "overview composite" },
        { id: "activity",             label: "Activity",             module: "./panels/activity.js" },
        { id: "role-growth",          label: "Role Growth",          module: "./panels/role-growth.js" },
        { id: "channel-comparison",   label: "Channel Comparison",    module: "./panels/channel-comparison.js" },
      ]},
      { heading: "Messages", items: [
        { id: "message-cadence",      label: "Message Cadence",      module: "./panels/message-cadence.js" },
        { id: "message-rate",         label: "Message Rate",         module: "./panels/message-rate.js" },
        { id: "burst-ranking",        label: "Burst Ranking",         module: "./panels/burst-ranking.js" },
      ]},
      { heading: "Engagement", items: [
        { id: "health-dau-mau",         label: "DAU/MAU",            module: "./panels/health-dau-mau.js", keywords: "daily monthly active users" },
        { id: "health-gini",            label: "Participation Gini", module: "./panels/health-gini.js" },
        { id: "health-churn-risk",      label: "Churn Risk",         module: "./panels/health-churn-risk.js" },
        { id: "retention",            label: "Activity Drops",        module: "./panels/retention.js", keywords: "retention churn drop-off" },
        { id: "voice-activity",       label: "Voice Activity",        module: "./panels/voice-activity.js" },
        { id: "xp-leaderboard",       label: "XP Leaderboard",       module: "./panels/xp-leaderboard.js", keywords: "levels rank experience", related: ["config-xp"], help: "help-community" },
        { id: "quality-score",        label: "Quality Score",        module: "./panels/quality-score.js" },
        { id: "nsfw-gender",          label: "NSFW by Gender",       module: "./panels/nsfw-gender.js" },
      ]},
      { heading: "Social Graph", items: [
        { id: "interaction-graph",    label: "Interactions",          module: "./panels/interaction-graph.js" },
        { id: "interaction-heatmap",  label: "Interaction Heatmap",   module: "./panels/interaction-heatmap.js" },
        { id: "connection-graph",     label: "Connection Graph",      module: "./panels/connection-graph.js", help: "help-network" },
        { id: "reaction-analytics",   label: "Reactions",             module: "./panels/reaction-analytics.js" },
        { id: "one-sided-attention",  label: "One-Sided Attention",   module: "./panels/one-sided-attention.js" },
      ]},
      { heading: "Greeter", items: [
        { id: "health-newcomer-funnel", label: "Newcomer Funnel",    module: "./panels/health-newcomer-funnel.js" },
        { id: "health-cohort-retention",label: "Cohort Retention",   module: "./panels/health-cohort-retention.js" },
        { id: "greeter-response",     label: "Greeter Response",     module: "./panels/greeter-response.js" },
        { id: "intake-report",        label: "Intake Queue",         module: "./panels/intake-report.js" },
        { id: "time-to-level5",       label: "Time to Level 5",      module: "./panels/time-to-level5.js" },
        { id: "xp-level-review",      label: "XP Level Review",      module: "./panels/xp-level-review.js" },
        { id: "invite-effectiveness", label: "Invite Effectiveness", module: "./panels/invite-effectiveness.js" },
        { id: "join-times",           label: "Join Times",           module: "./panels/join-times.js" },
      ]},
      { heading: "Member Lists", items: [
        { id: "list-role",            label: "List Role",            module: "./panels/list-role.js" },
        { id: "inactive-role",        label: "Inactive Role Members", module: "./panels/inactive-role.js", keywords: "inactive role report", related: ["config-prune"] },
        { id: "inactive",             label: "Inactive Members",     module: "./panels/inactive.js", keywords: "inactive members report", related: ["config-inactive"] },
        { id: "oldest-sfw",           label: "Oldest SFW",           module: "./panels/oldest-sfw.js" },
        { id: "birthday-calendar",    label: "Birthday Calendar",    module: "./panels/birthday-calendar.js", keywords: "birthdays report", related: ["config-birthday"] },
      ]},
    ],
  },
  {
    id: "moderation", label: "Moderation", perms: ["moderator"], icon: "⚖",
    items: [
      { id: "mod-todo",       label: "Todo List",      module: "./panels/todo.js", keywords: "tasks" },
      { id: "mod-jails",      label: "Jails",          module: "./panels/mod-jails.js", help: "help-jail" },
      { id: "mod-tickets",    label: "Tickets",        module: "./panels/mod-tickets.js", help: "help-tickets" },
      { id: "mod-warnings",   label: "Warnings",       module: "./panels/mod-warnings.js", help: "help-tickets" },
      { id: "mod-policy-tickets", label: "Policy Tickets", module: "./panels/mod-policy-tickets.js", help: "help-policies", related: ["config-policy-tickets"] },
      { id: "rules-watch",    label: "Rules Watch",    module: "./panels/rules-watch.js", help: "help-rules-watch", related: ["config-rules-watch"] },
      { id: "message-search", label: "Message Search",  module: "./panels/message-search.js", keywords: "messages logs find" },
    ],
    groups: [
      { heading: "Audit Logs", items: [
        { id: "mod-audit",         label: "Audit Log",        module: "./panels/mod-audit.js", adminOnly: true },
        { id: "mod-dm-audit",      label: "DM Audit",         module: "./panels/mod-dm-audit.js", adminOnly: true },
        { id: "quotes-audit",      label: "Quotes Audit",     module: "./panels/quotes-audit.js", adminOnly: true },
        { id: "guess-audit",       label: "Guess Who Audit",  module: "./panels/guess-audit.js", adminOnly: true },
        { id: "mod-whisper-audit", label: "Whisper Audit",    module: "./panels/mod-whisper-audit.js", adminOnly: true },
        { id: "confessions-audit", label: "Confessions Audit", module: "./panels/mod-confessions-audit.js", adminOnly: true },
        { id: "grant-audit",       label: "Grant Audit",      module: "./panels/grant-audit.js", keywords: "role grants audit" },
      ]},
    ],
  },
  {
    id: "config", label: "Config", perms: ["moderator"], icon: "⚙",
    // Most Config pages load at moderator level but every save requires admin,
    // so they're marked adminOnly — moderators see them as locked entries.
    // Exceptions: Wellness config is gated on manage_server, not admin, and
    // Docs / Role Menus / Chat Revive are fully moderator-level features.
    groups: [
      { heading: "Server", items: [
        { id: "config-global",     label: "Global",          module: "./panels/config-global.js", adminOnly: true, help: "help-config" },
        { id: "config-branding",   label: "Branding",        module: "./panels/config-branding.js", adminOnly: true },
        { id: "announcements",     label: "Announcements",     module: "./panels/announcements.js", adminOnly: true, help: "help-announcements" },
      ]},
      { heading: "Roles", items: [
        { id: "config-roles",         label: "Role Grants",      module: "./panels/config-roles.js", adminOnly: true, help: "help-setup" },
        { id: "config-booster-roles", label: "Booster Roles",   module: "./panels/config-booster-roles.js", adminOnly: true },
        { id: "config-auto-role",   label: "Auto-Role",         module: "./panels/config-auto-role.js", adminOnly: true },
        { id: "role-menus",        label: "Role Menus",        module: "./panels/role-menus.js", help: "help-role-menus" },
      ]},
      { heading: "Members", items: [
        { id: "config-welcome",    label: "Welcome & Leave",  module: "./panels/config-welcome.js", adminOnly: true, keywords: "greeting join leave messages" },
        { id: "config-intake",     label: "Intake Cards",     module: "./panels/config-intake.js", adminOnly: true },
        { id: "config-xp",            label: "XP & Leveling",      module: "./panels/config-xp.js", adminOnly: true, keywords: "xp levels leaderboard", related: ["xp-leaderboard"], help: "help-community" },
        { id: "config-bios",       label: "Bios",              module: "./panels/config-bios.js", adminOnly: true },
        { id: "config-birthday",   label: "Birthdays",         module: "./panels/config-birthday.js", adminOnly: true, related: ["birthday-calendar"] },
        { id: "gender-admin",      label: "Gender Tagging",   module: "./panels/gender-admin.js", adminOnly: true },
        { id: "config-wellness",   label: "Wellness",          module: "./panels/wellness-admin.js", perms: ["manage_server"], keywords: "caps limits gambling blackouts", help: "help-wellness" },
        { id: "config-prune",      label: "Auto-Remove Role (Inactive)", module: "./panels/config-prune.js", adminOnly: true, keywords: "prune inactive role removal", related: ["inactive-role"] },
        { id: "config-inactive",   label: "Inactive Sweep",   module: "./panels/config-inactive.js", adminOnly: true, keywords: "inactive purge kick sweep", related: ["inactive"] },
      ]},
      { heading: "Moderation & Safety", items: [
        { id: "config-moderation", label: "Moderation",        module: "./panels/config-moderation.js", adminOnly: true, help: "help-moderation" },
        { id: "config-rules-watch", label: "Rules Watch",       module: "./panels/config-rules-watch.js", adminOnly: true, help: "help-rules-watch", related: ["rules-watch"] },
        { id: "config-greeting-watch", label: "Greeting Watch",  module: "./panels/config-greeting-watch.js", adminOnly: true, help: "help-greeting-watch" },
        { id: "config-policy-tickets", label: "Policy Ticket Settings",  module: "./panels/config-policy-tickets.js", adminOnly: true, help: "help-policies", related: ["mod-policy-tickets"] },
        { id: "config-spoiler",      label: "Spoiler Guard",     module: "./panels/config-spoiler.js", adminOnly: true },
        { id: "config-dms",        label: "DM Permissions",   module: "./panels/config-dms.js", adminOnly: true, help: "help-dms" },
      ]},
      { heading: "Channels & Messages", items: [
        { id: "config-auto-delete", label: "Auto-Delete",      module: "./panels/config-auto-delete.js", adminOnly: true, keywords: "purge retention delete", help: "help-cleanup" },
        { id: "config-bulk-cleanup", label: "Bulk Cleanup",     module: "./panels/config-bulk-cleanup.js", adminOnly: true, keywords: "cleanup purge delete", help: "help-cleanup" },
        { id: "config-needle",     label: "Auto-Thread",       module: "./panels/config-needle.js", adminOnly: true, keywords: "needle thread replies" },
        { id: "config-starboard",  label: "Starboard",         module: "./panels/config-starboard.js", adminOnly: true },
        { id: "chat-revive",       label: "Chat Revive",       module: "./panels/chat-revive.js", keywords: "dead chat prompts", help: "help-chat-revive" },
        { id: "config-quote-border", label: "Quote Tool",     module: "./panels/config-quote-border.js", adminOnly: true, keywords: "quotes border color" },
        { id: "docs",              label: "Docs",              module: "./panels/docs.js", keywords: "channel docs documentation publish" },
      ]},
      { heading: "Voice", items: [
        { id: "config-voice-master", label: "Voice Master",      module: "./panels/config-voice-master.js", adminOnly: true, help: "help-voice" },
        { id: "config-voice-transcription", label: "Voice Transcription", module: "./panels/config-voice-transcription.js", adminOnly: true },
      ]},
      { heading: "AI & Maintenance", items: [
        { id: "config-ai",         label: "AI (Local LLM)",    module: "./panels/config-ai.js", primaryOnly: true, adminOnly: true, keywords: "models prompts llm", help: "help-ai" },
        { id: "config-advisor",    label: "Billy-bot",         module: "./panels/config-advisor.js", adminOnly: true, keywords: "advisor assistant ai ask", help: "help-ask" },
        { id: "admin-backfill",    label: "Backfill Jobs",     module: "./panels/admin-backfill.js", adminOnly: true },
      ]},
    ],
  },
  {
    // Shown to admins OR holders of the economy manager role (econManagerRole,
    // mirroring gameHostRole). Manager-visible items carry NO adminOnly/perms
    // so a manager-role holder who isn't an admin keeps them after
    // item-filtering; Settings is adminOnly (its endpoints require admin).
    id: "economy", label: "Economy", perms: ["admin"], econManagerRole: true, icon: "¤",
    items: [
      { id: "economy-bank-manager", label: "Operations", module: "./panels/economy-bank-manager.js", keywords: "bank manager balance grants refunds", help: "help-economy" },
      { id: "economy-claims", label: "Claims", module: "./panels/economy-claims.js", help: "help-economy" },
      { id: "economy-quests", label: "Quests", module: "./panels/economy-quests.js", help: "help-economy" },
      { id: "economy-income-sources", label: "Income Sources", module: "./panels/economy-income-sources.js", help: "help-economy" },
      { id: "economy-sinks", label: "Sinks", module: "./panels/economy-sinks.js", adminOnly: true, keywords: "shop perks icons", help: "help-economy" },
      { id: "config-casino", label: "Casino", module: "./panels/config-casino.js", adminOnly: true, keywords: "gambling slots blackjack", help: "help-casino" },
      { id: "economy-qotd", label: "QOTD", module: "./panels/economy-qotd.js", adminOnly: true, keywords: "question of the day" },
      { id: "economy-qotd-submissions", label: "Sponsored QOTD", module: "./panels/economy-qotd-submissions.js" },
      { id: "economy-stats", label: "Statistics", module: "./panels/economy-stats.js", help: "help-economy" },
      { id: "economy-config", label: "Settings", module: "./panels/economy-config.js", adminOnly: true, keywords: "economy currency settings", help: "help-economy" },
    ],
  },
  {
    id: "wellness", label: "Wellness", perms: [], roles: ["Wellness Guardian"], icon: "♥",
    items: [
      { id: "wellness-home",      label: "Overview",   module: "./panels/wellness-home.js", help: "help-wellness" },
      { id: "wellness-caps",      label: "Caps",       module: "./panels/wellness-caps.js", help: "help-wellness" },
      { id: "wellness-blackouts", label: "Blackouts",  module: "./panels/wellness-blackouts.js", help: "help-wellness" },
      { id: "wellness-away",      label: "Away",       module: "./panels/wellness-away.js", help: "help-wellness" },
      { id: "wellness-partners",  label: "Partners",   module: "./panels/wellness-partners.js", help: "help-wellness" },
      { id: "wellness-history",   label: "History",    module: "./panels/wellness-history.js", help: "help-wellness" },
    ],
  },
  {
    // Section gate: admins OR configured game-host role holders — every Games
    // endpoint is gated by require_game_host. Items carrying an explicit
    // `perms` list (Guess Who / Whisper configs, moderator-level backends)
    // stay visible to users who satisfy those perms even when the section
    // gate fails, so moderators can still reach them.
    id: "games", label: "Games", perms: ["admin"], gameHostRole: true, icon: "⚄",
    items: [
      { id: "games-logs",         label: "Overview & Logs",   module: "./panels/games-logs.js", help: "help-games" },
      { id: "games-scheduling",   label: "Scheduling",        module: "./panels/games-scheduling.js", help: "help-games" },
      { id: "games-legitlibs",    label: "LegitLibs",         module: "./panels/games-legitlibs.js" },
      { id: "games-config",       label: "Global Config",     module: "./panels/games-config.js", adminOnly: true, help: "help-games" },
      // Single-page games flattened to direct items named after the game
      // (subgroups below are only for games with two or more pages).
      { id: "config-risky-rolls",  label: "Risky Rolls",     module: "./panels/config-risky-rolls.js", adminOnly: true },
      { id: "config-games-pressure", label: "Pressure Cooker", module: "./panels/config-games-pressure.js", adminOnly: true },
      { id: "config-games-quickdraw", label: "Quickdraw", module: "./panels/config-games-quickdraw.js", adminOnly: true },
      { id: "config-games-hotpotato", label: "Hot Potato", module: "./panels/config-games-hotpotato.js", adminOnly: true },
      { id: "config-games-hotpotatogroup", label: "Hot Potato (Group)", module: "./panels/config-games-hotpotatogroup.js", adminOnly: true },
      { id: "config-games-chicken", label: "Chicken", module: "./panels/config-games-chicken.js", adminOnly: true },
      { id: "config-games-musicalchairs", label: "Musical Chairs", module: "./panels/config-games-musicalchairs.js", adminOnly: true },
      { id: "games-ffa", label: "FFA / Truth or Dare", module: "./panels/games-ffa.js" },
      { id: "games-traditional", label: "Traditional Truth or Dare", module: "./panels/games-traditional.js" },
      { id: "config-guess", label: "Guess Who", module: "./panels/config-guess.js", perms: ["moderator"], help: "help-guess" },
      { id: "config-whisper",    label: "Whisper",     module: "./panels/config-whisper.js", perms: ["moderator"], help: "help-whisper" },
      { id: "config-confessions",  label: "Confessions",     module: "./panels/config-confessions.js", adminOnly: true, help: "help-confessions" },
    ],
    groups: [
      { heading: "Would You Rather", items: [
        { id: "games-wyr",        label: "Questions",  module: "./panels/games-wyr.js" },
        { id: "games-wyr-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "wyr" },
      ]},
      { heading: "Never Have I Ever", items: [
        { id: "games-nhie",        label: "Questions",  module: "./panels/games-nhie.js" },
        { id: "games-nhie-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "nhie" },
      ]},
      { heading: "Most Likely To", items: [
        { id: "games-mlt",        label: "Questions",  module: "./panels/games-mlt.js" },
        { id: "games-mlt-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "mlt" },
      ]},
      { heading: "Rushmore", items: [
        { id: "games-rushmore",        label: "Questions",  module: "./panels/games-rushmore.js" },
        { id: "games-rushmore-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "rushmore" },
      ]},
      { heading: "Price", items: [
        { id: "games-price",        label: "Questions",  module: "./panels/games-price.js" },
        { id: "games-price-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "price" },
      ]},
      { heading: "Clapback", items: [
        { id: "games-clapback",        label: "Questions",  module: "./panels/games-clapback.js" },
        { id: "games-clapback-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "clapback" },
      ]},
      { heading: "AMA", items: [
        { id: "games-ama",        label: "Questions",  module: "./panels/games-ama.js" },
        { id: "games-ama-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "ama" },
      ]},
      { heading: "Pen Pals", items: [
        { id: "config-pen-pals",  label: "Config",     module: "./panels/config-pen-pals.js", adminOnly: true, help: "help-pen-pals" },
        { id: "games-pen-pals",   label: "Questions",  module: "./panels/games-pen-pals.js" },
        { id: "games-pen-pals-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "pen_pals" },
      ]},
    ],
  },
  {
    // Standalone feature — pulled out of the Games menu/scheduler. Same
    // game-host/admin gating as Games (endpoints use require_game_host).
    id: "photo-challenge", label: "Photo Challenge", perms: ["admin"], gameHostRole: true, icon: "◉",
    items: [
      { id: "photo-challenge",        label: "Setup & Schedule", module: "./panels/photo-challenge.js", help: "help-photo" },
      { id: "photo-challenge-studio", label: "Prompts & AI",     module: "./panels/games-studio.js", gt: "photo", help: "help-photo" },
    ],
  },
  HELP_NAV_SECTION,
  {
    id: "dev", label: "Dev", perms: ["admin"], icon: "⚒",
    items: [
      { id: "help-owner",    label: "Developer Tools", module: "./panels/help.js" },
      { id: "live-log",      label: "Live Log",        module: "./panels/live-log.js", keywords: "console output tail" },
      { id: "system-stats",  label: "System Stats",    module: "./panels/system-stats.js" },
      { id: "qa-tracker",    label: "QA Tracker",      module: "./panels/qa-tracker.js", keywords: "testing checklist" },
      // Routes the QA Tracker manual section (HELP_EXTRA_PAGES → help-qa). Without
      // a SECTIONS entry the id is absent from ALL_PAGES, so #/help-qa cannot mount
      // and in-manual links to the qa-tracker anchor fall back to the Dashboard.
      { id: "help-qa",       label: "QA Tracker Guide", module: "./panels/help.js" },
    ],
  },
];

// Flatten all page items for lookup
function allPages(section) {
  const items = section.items || [];
  const grouped = section.groups ? section.groups.flatMap((g) => g.items) : [];
  return [...items, ...grouped];
}

// Every page id that exists at all (before permission filtering) — used to
// tell "known but not available to you" apart from "no such page" (W-N4).
const FULL_PAGE_INDEX = new Map(SECTIONS.flatMap(allPages).map((p) => [p.id, p]));
// Routable help pages that live outside the nav (deep links only).
const EXTRA_ROUTES = HELP_EXTRA_PAGES
  .filter(({ page }) => !FULL_PAGE_INDEX.has(page))
  .map(_helpNavItem);
for (const p of EXTRA_ROUTES) FULL_PAGE_INDEX.set(p.id, p);

let userPerms = new Set();
let userRoleIds = new Set();
let userRoleNames = [];
let primaryGuildId = null;
let visibleSections = SECTIONS;
let ALL_PAGES = SECTIONS.flatMap(allPages);
let PAGE_TO_SECTION = {};

function rebuildIndex() {
  const isNonPrimaryGuild = primaryGuildId && window.__dk_user &&
    window.__dk_user.guild_id !== primaryGuildId;

  // Section-level gate. Sections whose gate fails are NOT dropped outright:
  // items inside them carrying an explicit `perms` list the user satisfies
  // remain reachable (W-N2 — e.g. moderator-level Guess Who / Whisper configs
  // inside the host-gated Games section). Empty sections are pruned below.
  const sectionGateOk = (sec) => {
    // Game host role: admins OR configured role holders. NOT moderators —
    // most Games endpoints are gated by require_game_host.
    if (sec.gameHostRole) {
      if (userPerms.has("admin")) return true;
      const hostRoleId = window.__dk_user?.games_editor_role_id;
      return !!(hostRoleId && userRoleIds.has(hostRoleId));
    }
    // Economy manager role: admins OR the configured manager-role holders
    // (every endpoint is gated by require_economy_manager).
    if (sec.econManagerRole) {
      if (userPerms.has("admin")) return true;
      const mgrRoleId = window.__dk_user?.economy_manager_role_id;
      return !!(mgrRoleId && userRoleIds.has(mgrRoleId));
    }
    const permOk = !sec.perms || sec.perms.length === 0 || sec.perms.every((p) => userPerms.has(p));
    if (!permOk) return false;
    if (sec.roles && sec.roles.length > 0) {
      if (userPerms.has("manage_server") || userPerms.has("admin")) return true;
      return sec.roles.some((r) => userRoleNames.includes(r));
    }
    return true;
  };

  const isAdmin = userPerms.has("admin");
  const isModerator = userPerms.has("moderator");

  // Per-item permission gating. Within a gate-passing section:
  //   - adminOnly items show for admins; for moderators they render as
  //     locked (disabled) entries so the page's existence isn't invisible
  //     (W-N5); for everyone else they're hidden.
  //   - `perms` items require every listed perm.
  // Within a gate-failing section, only explicit-`perms` items the user
  // satisfies survive. Returns the item (possibly marked locked) or null.
  const resolveItem = (it, secOk) => {
    if (!secOk) {
      if (it.perms && it.perms.every((p) => userPerms.has(p))) return it;
      return null;
    }
    if (it.adminOnly && !isAdmin) {
      return isModerator ? { ...it, locked: true } : null;
    }
    if (it.perms && !it.perms.every((p) => userPerms.has(p))) return null;
    return it;
  };

  visibleSections = SECTIONS.map((sec) => {
    const secOk = sectionGateOk(sec);
    const newItems = (sec.items || []).map((it) => resolveItem(it, secOk)).filter(Boolean);
    const newGroups = sec.groups
      ? sec.groups
          .map((g) => ({ ...g, items: g.items.map((it) => resolveItem(it, secOk)).filter(Boolean) }))
          .filter((g) => g.items.length > 0)
      : sec.groups;
    return { ...sec, items: newItems, groups: newGroups };
  });

  // Config is per-guild. For a non-primary guild, show every Config page except
  // those marked `primaryOnly` (genuinely-global settings like the AI models,
  // which live under guild_id=0 and apply bot-wide).
  if (isNonPrimaryGuild) {
    const dropPrimaryOnly = (items) => (items || []).filter((it) => !it.primaryOnly);
    visibleSections = visibleSections.map((sec) =>
      sec.id === "config"
        ? {
            ...sec,
            items: dropPrimaryOnly(sec.items),
            groups: sec.groups
              ? sec.groups
                  .map((g) => ({ ...g, items: dropPrimaryOnly(g.items) }))
                  .filter((g) => g.items.length > 0)
              : sec.groups,
          }
        : sec
    );
  }

  // Drop sections left with nothing to show after item filtering, so we never
  // render an empty section header.
  visibleSections = visibleSections.filter((sec) => allPages(sec).length > 0);

  // Locked entries are visible in the nav but not mountable.
  ALL_PAGES = visibleSections.flatMap(allPages).filter((p) => !p.locked);
  PAGE_TO_SECTION = {};
  for (const sec of visibleSections) {
    for (const page of allPages(sec)) {
      PAGE_TO_SECTION[page.id] = sec;
    }
  }
  // Pages this user can actually open — consumed by widget-grid.js so Home
  // tiles only click through to reachable reports (W-N9).
  window.__dkVisiblePages = new Set(ALL_PAGES.map((p) => p.id));
}
rebuildIndex();

// ── DOM refs ────────────────────────────────────────────────────────

const guildSelectEl = document.getElementById("guild-select");
const sidebarEl = document.getElementById("sidebar");
const sidebarItemsEl = document.getElementById("sidebar-items");
const rootEl = document.getElementById("panel-root");
const meEl = document.getElementById("me");
const sidebarToggleEl = document.getElementById("sidebar-toggle");
const sidebarBackdropEl = document.getElementById("sidebar-backdrop");
const navFilterEl = document.querySelector("[data-nav-filter]");
const navFilterClearEl = document.querySelector("[data-nav-filter-clear]");
const skipLinkEl = document.querySelector(".skip-link");

let currentPanel = null;

// ── Unsaved-changes guard ───────────────────────────────────────────
// config-helpers.js publishes window.__dkDirty() → bool and
// window.__dkDirtyReset(); we consult them before any navigation that
// would discard in-progress edits (W-N7 / W-C1).

function confirmLeaveDirty() {
  if (!window.__dkDirty?.()) return true;
  if (!window.confirm("You have unsaved changes — leave anyway?")) return false;
  window.__dkDirtyReset?.();
  return true;
}

// ── Sidebar collapse (desktop) + mobile open/close ─────────────────

const COLLAPSE_KEY = "dk_sidebar_collapsed";

function closeMobileSidebar() {
  sidebarEl.classList.remove("open");
  sidebarBackdropEl.classList.remove("open");
  document.body.classList.remove("sidebar-locked");
}

function openMobileSidebar() {
  sidebarEl.classList.add("open");
  sidebarBackdropEl.classList.add("open");
  document.body.classList.add("sidebar-locked");
  // Keyboard users land on the filter; from there Tab reaches the nav.
  navFilterEl?.focus({ preventScroll: true });
}

// Persisted desktop collapse state (W-N8)
try {
  if (localStorage.getItem(COLLAPSE_KEY) === "1") sidebarEl.classList.add("collapsed");
} catch (_) {}

sidebarToggleEl.addEventListener("click", (e) => {
  e.stopPropagation();
  if (window.innerWidth <= 768) {
    closeMobileSidebar();
  } else {
    const collapsed = sidebarEl.classList.toggle("collapsed");
    try { localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0"); } catch (_) {}
  }
});
sidebarBackdropEl.addEventListener("click", closeMobileSidebar);

// Mobile hamburger button
const mobileMenuBtnEl = document.getElementById("mobile-menu-btn");
if (mobileMenuBtnEl) {
  mobileMenuBtnEl.addEventListener("click", openMobileSidebar);
}

// Escape closes the mobile drawer and returns focus to the hamburger (W-A10)
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && window.innerWidth <= 768 && sidebarEl.classList.contains("open")) {
    closeMobileSidebar();
    mobileMenuBtnEl?.focus();
  }
});

// Skip link: focus the panel without disturbing the hash router (W-A1)
if (skipLinkEl) {
  skipLinkEl.addEventListener("click", (e) => {
    e.preventDefault();
    rootEl.focus();
  });
}

// Swipe-from-left-edge to open sidebar on mobile
(function () {
  let touchStartX = 0;
  let touchStartY = 0;
  let tracking = false;

  document.addEventListener("touchstart", (e) => {
    if (window.innerWidth > 768) return;
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
    tracking = touchStartX < 24; // only track swipes starting near left edge
  }, { passive: true });

  document.addEventListener("touchend", (e) => {
    if (!tracking) return;
    tracking = false;
    const dx = e.changedTouches[0].clientX - touchStartX;
    const dy = Math.abs(e.changedTouches[0].clientY - touchStartY);
    if (dx > 40 && dy < 60) openMobileSidebar();
  }, { passive: true });
})();

// ── Nav filter ──────────────────────────────────────────────────────
// dataset.search on each item = section + subgroup + label + keywords.
// Every whitespace-separated query token must match (AND), so
// "games config" narrows instead of widening.

function applyNavFilter() {
  const q = navFilterEl.value.trim().toLowerCase();
  const tokens = q.split(/\s+/).filter(Boolean);
  if (navFilterClearEl) navFilterClearEl.hidden = !q;
  // While a query is active, matches must show through collapsed groups
  sidebarItemsEl.classList.toggle("filtering", !!q);
  const items = sidebarItemsEl.querySelectorAll(".nav-item");
  items.forEach((it) => {
    const txt = it.dataset.search ||
      it.querySelector(".lbl")?.textContent.toLowerCase() || "";
    const match = tokens.every((t) => txt.includes(t));
    it.classList.toggle("filtered-out", !!q && !match);
  });
  // Hide empty subgroups / groups
  sidebarItemsEl.querySelectorAll(".nav-subgroup").forEach((sg) => {
    let n = sg.nextElementSibling;
    let anyVisible = false;
    while (n && !n.matches(".nav-subgroup, .nav-group")) {
      if (n.matches(".nav-item") && !n.classList.contains("filtered-out")) { anyVisible = true; break; }
      n = n.nextElementSibling;
    }
    sg.classList.toggle("filtered-out", !anyVisible);
  });
  sidebarItemsEl.querySelectorAll(".nav-group").forEach((g) => {
    let n = g.nextElementSibling;
    let anyVisible = false;
    while (n && !n.matches(".nav-group")) {
      if (n.matches(".nav-item") && !n.classList.contains("filtered-out")) { anyVisible = true; break; }
      n = n.nextElementSibling;
    }
    g.classList.toggle("filtered-empty", !anyVisible);
  });
}

if (navFilterEl) {
  navFilterEl.addEventListener("input", applyNavFilter);
  // Enter opens the first visible match
  navFilterEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && navFilterEl.value.trim()) {
      const first = sidebarItemsEl.querySelector(
        ".nav-item:not(.filtered-out):not(.nav-locked)"
      );
      first?.click();
    } else if (e.key === "Escape" && navFilterEl.value) {
      navFilterEl.value = "";
      applyNavFilter();
    }
  });
}
if (navFilterClearEl) {
  navFilterClearEl.addEventListener("click", () => {
    navFilterEl.value = "";
    applyNavFilter();
    navFilterEl.focus();
  });
}

// ── Hash parsing ────────────────────────────────────────────────────
//
// Route convention: `#/<page-id>?key=val&…`. Panel-local state (tabs,
// filters, selections) belongs in the query part:
//   - mount(el, params) receives the parsed query as an object;
//   - to persist state changes while mounted, panels call
//     `history.replaceState(null, "", "#/<own-id>?key=val")` — replaceState
//     does NOT fire hashchange, so the panel is never remounted for its own
//     query updates (activity.js and xp-leaderboard.js are the reference
//     implementations);
//   - a real hashchange (nav click, back button, external link) always
//     remounts, even to the same page id, so deep links re-apply params.

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, "");
  if (!raw) return { id: "home", params: {} };
  const [id, qs] = raw.split("?");
  const params = {};
  if (qs) {
    for (const [k, v] of new URLSearchParams(qs)) params[k] = v;
  }
  return { id, params };
}

// ── Render nav ──────────────────────────────────────────────────────

const NAV_OPEN_KEY = "dk_nav_open";

function loadOpenSections() {
  try {
    const raw = JSON.parse(localStorage.getItem(NAV_OPEN_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw : []);
  } catch (_) {
    return new Set();
  }
}

function saveOpenSections(set) {
  try { localStorage.setItem(NAV_OPEN_KEY, JSON.stringify([...set])); } catch (_) {}
}

// Return a copy of the items, sorted alphabetically by label (case-insensitive).
// An optional numeric `order` overrides alphabetical order (used for the
// onboarding Help items where "Getting Started" must lead despite sorting after
// "Ask Billy-bot"); items without `order` sort after ordered ones, alphabetically.
// Copies rather than mutating so the source SECTIONS order is preserved.
function byLabel(items) {
  return [...(items || [])].sort((a, b) => {
    const ao = a.order ?? Infinity;
    const bo = b.order ?? Infinity;
    if (ao !== bo) return ao - bo;
    return (a.label || "").localeCompare(b.label || "", undefined, { sensitivity: "base" });
  });
}

function makeNavItem(item, activeId, { isSubitem = false, icon = "#" } = {}) {
  const btn = document.createElement("button");
  btn.className = "nav-item" + (isSubitem ? " is-subitem" : "");
  btn.type = "button";
  btn.dataset.pageId = item.id;
  // Tooltip label — the only visible label on the collapsed rail (W-N3).
  btn.title = item.locked ? `${item.label} — Admin only` : item.label;

  const icn = document.createElement("span");
  icn.className = "icn";
  icn.textContent = icon;
  icn.setAttribute("aria-hidden", "true");
  btn.appendChild(icn);

  const lbl = document.createElement("span");
  lbl.className = "lbl";
  lbl.textContent = item.label;
  btn.appendChild(lbl);

  if (item.locked) {
    // Admin-only page shown (but not openable) for moderators (W-N5).
    btn.classList.add("nav-locked");
    btn.disabled = true;
    btn.setAttribute("aria-disabled", "true");
    const lock = document.createElement("span");
    lock.className = "lock";
    lock.textContent = "\u{1F512}";
    lock.setAttribute("aria-hidden", "true");
    btn.appendChild(lock);
    return btn;
  }

  if (item.id === activeId) btn.classList.add("active");

  btn.addEventListener("click", () => {
    const qs = item.gt ? `?gt=${item.gt}` : "";
    window.location.hash = `#/${item.id}${qs}`;
  });
  return btn;
}

function renderNav(activeId) {
  // If focus is inside the nav, restore it to the same page's button after
  // the rebuild instead of dropping it on <body> (W-A1).
  const focusedPageId = document.activeElement?.closest?.(".nav-item")?.dataset?.pageId;

  sidebarItemsEl.innerHTML = "";

  const activeSection = PAGE_TO_SECTION[activeId];
  const openSections = loadOpenSections();

  for (const sec of visibleSections) {
    const group = document.createElement("div");
    group.className = "nav-group";
    group.textContent = sec.label;
    group.setAttribute("role", "button");
    group.tabIndex = 0;
    // Open the active page's section plus any the user opened previously
    // (persisted across navigations, W-N8).
    const startCollapsed =
      !(activeSection && sec.id === activeSection.id) && !openSections.has(sec.id);
    if (startCollapsed) group.classList.add("collapsed");
    group.setAttribute("aria-expanded", String(!startCollapsed));
    const toggleGroup = () => {
      group.classList.toggle("collapsed");
      const hidden = group.classList.contains("collapsed");
      group.setAttribute("aria-expanded", String(!hidden));
      const saved = loadOpenSections();
      if (hidden) saved.delete(sec.id); else saved.add(sec.id);
      saveOpenSections(saved);
      let n = group.nextElementSibling;
      while (n && !n.matches(".nav-group")) {
        n.classList.toggle("group-hidden", hidden);
        n = n.nextElementSibling;
      }
    };
    group.addEventListener("click", toggleGroup);
    group.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleGroup(); }
    });
    sidebarItemsEl.appendChild(group);

    const children = [];
    const icon = sec.icon || "#";

    // Top-level items (rendered before any subgroup), alphabetized by label
    for (const item of byLabel(sec.items)) {
      const el = makeNavItem(item, activeId, { icon });
      el.dataset.search = `${sec.label} ${item.label} ${item.keywords || ""}`.trim().toLowerCase();
      sidebarItemsEl.appendChild(el);
      children.push(el);
    }

    // Subgroups (each with collapsible heading; default expanded)
    if (sec.groups) {
      for (const g of sec.groups) {
        const subLabel = document.createElement("div");
        subLabel.className = "nav-subgroup";
        subLabel.textContent = g.heading;
        subLabel.setAttribute("role", "button");
        subLabel.tabIndex = 0;

        const subgroupActive = g.items.some((item) => item.id === activeId);
        if (!subgroupActive) subLabel.classList.add("collapsed");
        subLabel.setAttribute("aria-expanded", String(subgroupActive));

        const toggleSub = (ev) => {
          if (ev) ev.stopPropagation();
          subLabel.classList.toggle("collapsed");
          const hidden = subLabel.classList.contains("collapsed");
          subLabel.setAttribute("aria-expanded", String(!hidden));
          let n = subLabel.nextElementSibling;
          while (n && !n.matches(".nav-subgroup, .nav-group")) {
            n.classList.toggle("subgroup-hidden", hidden);
            n = n.nextElementSibling;
          }
        };
        subLabel.addEventListener("click", toggleSub);
        subLabel.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleSub(e); }
        });

        sidebarItemsEl.appendChild(subLabel);
        children.push(subLabel);
        for (const item of byLabel(g.items)) {
          const el = makeNavItem(item, activeId, { isSubitem: true, icon });
          el.dataset.search =
            `${sec.label} ${g.heading} ${item.label} ${item.keywords || ""}`.trim().toLowerCase();
          if (!subgroupActive) el.classList.add("subgroup-hidden");
          sidebarItemsEl.appendChild(el);
          children.push(el);
        }
      }
    }

    if (startCollapsed) {
      for (const c of children) c.classList.add("group-hidden");
    }
  }

  // Re-apply any active filter text
  if (navFilterEl && navFilterEl.value) {
    applyNavFilter();
  }

  if (focusedPageId) {
    sidebarItemsEl
      .querySelector(`.nav-item[data-page-id="${CSS.escape(focusedPageId)}"]`)
      ?.focus();
  }
}

// ── Mount panel ─────────────────────────────────────────────────────

// The full hash of the last successfully mounted panel — used to restore the
// URL when the user cancels navigating away from unsaved edits.
let lastGoodHash = null;

function setDocTitle(label) {
  document.title = label ? `${label} — Dungeon Keeper` : "Dungeon Keeper Dashboard";
}

// Small header row above the panel: contextual help "?" (W-H2) and
// related-page cross-links (W-N14). Injected as a sibling before the panel,
// so panels that re-render their own innerHTML don't wipe it.
function renderPanelMeta(page) {
  const bits = [];
  const related = (page.related || [])
    .map((rid) => ALL_PAGES.find((p) => p.id === rid))
    .filter(Boolean);
  for (const rp of related) {
    bits.push(`<a class="panel-meta-link" href="#/${esc(rp.id)}">Related: ${esc(rp.label)} ↗</a>`);
  }
  if (page.help && page.help !== page.id && FULL_PAGE_INDEX.has(page.help)) {
    bits.push(
      `<a class="panel-meta-link panel-meta-help" href="#/${esc(page.help)}"
          title="Open the guide for this page" aria-label="Help for ${esc(page.label)}">?</a>`
    );
  }
  if (!bits.length) return;
  const bar = document.createElement("div");
  bar.className = "panel-meta-bar";
  bar.innerHTML = bits.join("");
  rootEl.prepend(bar);
}

// Unknown or inaccessible route: render an in-panel notice instead of
// silently mounting Home (W-N4). "Known but filtered" gets its own copy.
function renderUnavailable(id) {
  const known = FULL_PAGE_INDEX.get(id);
  const msg = known
    ? `<b>${esc(known.label)}</b> exists, but it isn't available to you on this server.`
    : "This page doesn't exist or isn't available to you.";
  rootEl.innerHTML = `
    <div class="panel">
      <div class="panel-missing">
        <h2>Page Not Available</h2>
        <p>${msg}</p>
        <p><a class="btn" href="#/home">Go to the Dashboard</a></p>
      </div>
    </div>`;
  setDocTitle("Page Not Available");
  rootEl.focus();
}

async function mountPanel(evt) {
  // Unsaved-changes guard: cancel keeps the current panel and restores the
  // pre-navigation hash (hashchange can't be prevented, only undone).
  if (currentPanel && !confirmLeaveDirty()) {
    const oldHash = evt?.oldURL ? new URL(evt.oldURL).hash : lastGoodHash;
    if (oldHash && oldHash !== window.location.hash) {
      history.replaceState(null, "", oldHash);
    }
    return;
  }

  closeMobileSidebar();
  const { id, params } = parseHash();
  const page =
    ALL_PAGES.find((p) => p.id === id) || EXTRA_ROUTES.find((p) => p.id === id);

  if (currentPanel && currentPanel.unmount) {
    try { currentPanel.unmount(); } catch (_) {}
  }
  currentPanel = null;

  if (!page) {
    renderNav(id);
    renderUnavailable(id);
    lastGoodHash = window.location.hash || "#/home";
    return;
  }

  renderNav(page.id);
  rootEl.innerHTML = `<div class="panel"><div class="panel-loading">Loading ${esc(page.label)}…</div></div>`;

  try {
    // The ?v=3 literal is rewritten to the current boot id by the server's
    // _CacheBustJS middleware, so each reboot yields a fresh panel URL. Without
    // it, dynamically-imported panels (a variable specifier the import-rewrite
    // regex can't see) would stay immutable-cached forever and never pick up
    // changes to their module graph.
    const mod = await import(`${page.module}?v=3`);
    currentPanel = mod.mount(rootEl, params) || null;
    renderPanelMeta(page);
    setDocTitle(page.label);
    // Move focus to the fresh panel so keyboard/screen-reader users don't
    // have to re-traverse the sidebar after every navigation (W-A1).
    rootEl.focus();
  } catch (err) {
    rootEl.innerHTML = `<div class="panel"><div class="error">Failed to load ${esc(page.label)}: ${esc(err.message)}</div></div>`;
  }
  lastGoodHash = window.location.hash || "#/home";
}

// ── Boot ────────────────────────────────────────────────────────────

function applyMeData(me) {
  userPerms = new Set(me.perms);
  userRoleIds = new Set(me.role_ids || []);
  userRoleNames = me.role_names || [];
  primaryGuildId = me.primary_guild_id || me.guild_id;

  window.__dk_user = {
    user_id: me.user_id,
    username: me.username,
    perms: userPerms,
    role_ids: userRoleIds,
    role_names: userRoleNames,
    guild_id: me.guild_id,
    primary_guild_id: primaryGuildId,
    games_editor_role_id: me.games_editor_role_id || null,
    economy_manager_role_id: me.economy_manager_role_id || null,
  };

  // Recompute visible nav (Config pages are filtered per primary/non-primary)
  rebuildIndex();
}

function populateGuildPicker(guilds, activeId) {
  const nameEl = guildSelectEl.querySelector(".guild-picker__name");
  const sigilEl = guildSelectEl.querySelector("[data-guild-sigil]");
  const menuEl = guildSelectEl.querySelector(".guild-picker__menu");
  menuEl.innerHTML = "";
  menuEl.setAttribute("role", "listbox");
  menuEl.setAttribute("aria-label", "Switch server");
  const active = guilds.find((g) => g.id === activeId) || guilds[0];
  if (active) {
    nameEl.textContent = active.name;
    if (sigilEl) {
      if (active.icon) {
        sigilEl.innerHTML = `<img class="guild-sigil-img" src="${esc(active.icon)}" alt="">`;
      } else {
        sigilEl.textContent = active.name.charAt(0).toUpperCase();
      }
    }
  }
  for (const g of guilds) {
    const li = document.createElement("li");
    li.className = "guild-picker__item" + (g.id === activeId ? " active" : "");
    li.textContent = g.name;
    li.dataset.id = g.id;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", g.id === activeId ? "true" : "false");
    li.tabIndex = -1;
    li.addEventListener("click", () => {
      guildSelectEl.classList.remove("open");
      if (g.id !== activeId) switchGuild(g.id);
    });
    menuEl.appendChild(li);
  }
  // Keyboard operation: arrows move focus, Enter/Space select, Escape closes.
  // Bound once — populateGuildPicker reruns per guild switch and stacking a
  // listener each time made Enter fire N times (W-A11).
  if (!menuEl.dataset.kbdBound) {
    menuEl.dataset.kbdBound = "1";
    menuEl.addEventListener("keydown", (e) => {
      const items = Array.from(menuEl.querySelectorAll(".guild-picker__item"));
      if (!items.length) return;
      const idx = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const next = e.key === "ArrowDown" ? idx + 1 : idx - 1;
        items[(next + items.length) % items.length].focus();
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (idx >= 0) items[idx].click();
      } else if (e.key === "Escape") {
        guildSelectEl.classList.remove("open");
        const toggle = guildSelectEl.querySelector(".guild-picker__toggle");
        toggle?.setAttribute("aria-expanded", "false");
        toggle?.focus();
      }
    });
  }
  // Always show the guild bar — it doubles as the sidebar head.
  // If only one guild, suppress the dropdown but keep the bar visible.
  guildSelectEl.style.display = "";
  guildSelectEl.classList.toggle("single-guild", guilds.length <= 1);
}

function renderUserBar(me) {
  const initial = (me.username || "?").charAt(0).toUpperCase();
  const isGuest = me.user_id === "0";
  const status = isGuest ? "offline" : (me.status || "online");
  const statusLabel = isGuest ? "guest" : status;
  const avatarInner = (!isGuest && me.avatar_url)
    ? `<img class="user-avatar-img" src="${esc(me.avatar_url)}" alt="">`
    : esc(initial);
  meEl.innerHTML = `
    <div class="user-avatar status-${esc(status)}">${avatarInner}</div>
    <div class="user-meta">
      <b>${esc(me.username || "")}</b>
      <small>${esc(statusLabel)}</small>
    </div>
    ${!isGuest ? `<a class="logout-link" href="/logout">Logout</a>` : ""}
  `;
}

async function switchGuild(newGuildId) {
  if (!confirmLeaveDirty()) return;
  try {
    const res = await fetch(`/api/guilds/${newGuildId}/select`, {
      method: "POST",
      credentials: "same-origin",
    });
    if (res.status === 401) { window.location = "/login"; return; }
    if (!res.ok) return;
    const me = await res.json();
    applyMeData(me);
    if (me.guilds) populateGuildPicker(me.guilds, me.guild_id);
    // If the open page doesn't exist on the newly selected guild (e.g. a
    // primaryOnly Config page), say so and land on Home instead of bouncing
    // silently (W-N7).
    const { id } = parseHash();
    const stillVisible =
      ALL_PAGES.some((p) => p.id === id) || EXTRA_ROUTES.some((p) => p.id === id);
    if (!stillVisible) {
      const label = FULL_PAGE_INDEX.get(id)?.label || "That page";
      toast(`${label} isn't available on this server`, "info");
      if (window.location.hash !== "#/home") {
        window.location.hash = "#/home"; // hashchange remounts
        return;
      }
    }
    renderNav(id);
    mountPanel();
  } catch (err) {
    console.error("Guild switch failed:", err);
  }
}

async function boot() {
  try {
    const me = await api("/api/me");
    if (!me) return; // redirecting to login

    applyMeData(me);

    // Guild picker
    if (me.guilds && me.guilds.length > 0) {
      populateGuildPicker(me.guilds, me.guild_id);
      const toggle = guildSelectEl.querySelector(".guild-picker__toggle");
      toggle.setAttribute("aria-haspopup", "listbox");
      toggle.setAttribute("aria-expanded", "false");
      const setOpen = (open) => {
        guildSelectEl.classList.toggle("open", open);
        toggle.setAttribute("aria-expanded", String(open));
        if (open) {
          // Move focus into the list so arrow keys work immediately.
          const first =
            guildSelectEl.querySelector(".guild-picker__item.active") ||
            guildSelectEl.querySelector(".guild-picker__item");
          first?.focus();
        }
      };
      toggle.addEventListener("click", (e) => {
        // Only open the dropdown if there's more than one guild
        if (me.guilds.length <= 1) return;
        e.stopPropagation();
        setOpen(!guildSelectEl.classList.contains("open"));
      });
      toggle.addEventListener("keydown", (e) => {
        if (me.guilds.length <= 1) return;
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setOpen(true);
        }
      });
      document.addEventListener("click", (e) => {
        if (!guildSelectEl.contains(e.target)) {
          guildSelectEl.classList.remove("open");
          toggle.setAttribute("aria-expanded", "false");
        }
      });
    }

    renderUserBar(me);
  } catch (err) {
    meEl.innerHTML = `<div class="user-meta"><small style="color:var(--red-text)">auth error: ${esc(err.message)}</small></div>`;
  }
  window.addEventListener("hashchange", mountPanel);
  window.addEventListener("beforeunload", (e) => {
    if (window.__dkDirty?.()) e.preventDefault();
  });
  mountPanel();
}

boot();
