// Dashboard boot + hash-based panel router.
import { api, esc } from "./api.js";
import { HELP_GROUPS } from "./panels/help-sections.js?v=24";


// The Help nav is generated from help-sections.js (single source shared with
// the help panel) so nav entries can't drift from the manual's sections.
const _helpNavItem = ({ page, label }) => ({ id: page, label, module: "./panels/help.js" });
const HELP_NAV_SECTION = {
  id: "help", label: "Help", perms: [],
  items: HELP_GROUPS.filter((g) => !g.heading).flatMap((g) => g.items.map(_helpNavItem)),
  groups: HELP_GROUPS.filter((g) => g.heading).map((g) => ({
    heading: g.heading,
    items: g.items.map(_helpNavItem),
  })),
};

// ── Section definitions ─────────────────────────────────────────────

const SECTIONS = [
  {
    id: "home", label: "Dashboard", perms: [],
    items: [
      { id: "home", label: "Dashboard", module: "./panels/home.js" },
      { id: "help-quickref", label: "Quick Reference", module: "./panels/help.js" },
    ],
  },
  {
    id: "reports", label: "Reports", perms: ["moderator"],
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
        { id: "health-composite-score", label: "Health Score",       module: "./panels/health-composite-score.js" },
        { id: "activity",             label: "Activity",             module: "./panels/activity.js" },
        { id: "role-growth",          label: "Role Growth",          module: "./panels/role-growth.js" },
        { id: "channel-comparison",   label: "Channel Comparison",    module: "./panels/channel-comparison.js" },
      ]},
      { heading: "Messages", items: [
        { id: "message-cadence",      label: "Message Cadence",      module: "./panels/message-cadence.js" },
        { id: "message-rate",         label: "Message Rate",         module: "./panels/message-rate.js" },
        { id: "burst-ranking",        label: "Burst Ranking",         module: "./panels/burst-ranking.js" },
      ]},
      { heading: "People", items: [
        { id: "health-dau-mau",         label: "DAU/MAU",            module: "./panels/health-dau-mau.js" },
        { id: "health-gini",            label: "Participation Gini", module: "./panels/health-gini.js" },
        { id: "health-churn-risk",      label: "Churn Risk",         module: "./panels/health-churn-risk.js" },
        { id: "retention",            label: "Activity Drops",        module: "./panels/retention.js" },
        { id: "interaction-graph",    label: "Interactions",          module: "./panels/interaction-graph.js" },
        { id: "interaction-heatmap",  label: "Interaction Heatmap",   module: "./panels/interaction-heatmap.js" },
        { id: "connection-graph",     label: "Connection Graph",      module: "./panels/connection-graph.js" },
        { id: "voice-activity",       label: "Voice Activity",        module: "./panels/voice-activity.js" },
        { id: "xp-leaderboard",       label: "XP Leaderboard",       module: "./panels/xp-leaderboard.js" },
        { id: "reaction-analytics",   label: "Reactions",             module: "./panels/reaction-analytics.js" },
        { id: "nsfw-gender",          label: "NSFW by Gender",       module: "./panels/nsfw-gender.js" },
        { id: "quality-score",        label: "Quality Score",        module: "./panels/quality-score.js" },
      ]},
      { heading: "Greeter", items: [
        { id: "health-newcomer-funnel", label: "Newcomer Funnel",    module: "./panels/health-newcomer-funnel.js" },
        { id: "health-cohort-retention",label: "Cohort Retention",   module: "./panels/health-cohort-retention.js" },
        { id: "greeter-response",     label: "Greeter Response",     module: "./panels/greeter-response.js" },
        { id: "time-to-level5",       label: "Time to Level 5",      module: "./panels/time-to-level5.js" },
        { id: "xp-level-review",      label: "XP Level Review",      module: "./panels/xp-level-review.js" },
        { id: "invite-effectiveness", label: "Invite Effectiveness", module: "./panels/invite-effectiveness.js" },
        { id: "join-times",           label: "Join Times",           module: "./panels/join-times.js" },
      ]},
      { heading: "Member Lists", items: [
        { id: "list-role",            label: "List Role",            module: "./panels/list-role.js" },
        { id: "inactive-role",        label: "Inactive Role",        module: "./panels/inactive-role.js" },
        { id: "inactive",             label: "Inactive Members",     module: "./panels/inactive.js" },
        { id: "oldest-sfw",           label: "Oldest SFW",           module: "./panels/oldest-sfw.js" },
      ]},
    ],
  },
  {
    id: "moderation", label: "Moderation", perms: ["moderator"],
    items: [
      { id: "mod-todo",       label: "Todo List",      module: "./panels/todo.js" },
      { id: "mod-jails",      label: "Jails",          module: "./panels/mod-jails.js" },
      { id: "mod-tickets",    label: "Tickets",        module: "./panels/mod-tickets.js" },
      { id: "mod-warnings",   label: "Warnings",       module: "./panels/mod-warnings.js" },
      { id: "mod-policy-tickets", label: "Policy Tickets", module: "./panels/mod-policy-tickets.js" },
      { id: "rules-watch",    label: "Rules Watch",    module: "./panels/rules-watch.js" },
      { id: "message-search", label: "Message Search",  module: "./panels/message-search.js" },
    ],
    groups: [
      { heading: "Audit Logs", items: [
        { id: "mod-audit",         label: "Audit Log",        module: "./panels/mod-audit.js", adminOnly: true },
        { id: "mod-dm-audit",      label: "DM Audit",         module: "./panels/mod-dm-audit.js", adminOnly: true },
        { id: "quotes-audit",      label: "Quotes Audit",     module: "./panels/quotes-audit.js", adminOnly: true },
        { id: "guess-audit",       label: "Guess Who Audit",  module: "./panels/guess-audit.js", adminOnly: true },
        { id: "mod-whisper-audit", label: "Whisper Audit",    module: "./panels/mod-whisper-audit.js", adminOnly: true },
        { id: "confessions-audit", label: "Confessions Audit", module: "./panels/mod-confessions-audit.js", adminOnly: true },
      ]},
    ],
  },
  {
    id: "config", label: "Config", perms: ["moderator"],
    // Most Config pages load at moderator level but every save requires admin,
    // so they're marked adminOnly to hide them from moderators who can't use
    // them. Exceptions: Birthday Calendar is genuinely moderator-level (read
    // only), Wellness config is gated on manage_server, not admin, and
    // Docs / Role Menus / Chat Revive are fully moderator-level features.
    items: [
      { id: "config-global",     label: "Global",          module: "./panels/config-global.js", adminOnly: true },
      { id: "config-branding",   label: "Branding",        module: "./panels/config-branding.js", adminOnly: true },
      { id: "config-quote-border", label: "Quote Tool",     module: "./panels/config-quote-border.js", adminOnly: true },
      { id: "config-welcome",    label: "Welcome & Leave",  module: "./panels/config-welcome.js", adminOnly: true },
      { id: "config-roles",         label: "Role Grants",      module: "./panels/config-roles.js", adminOnly: true },
      { id: "config-booster-roles", label: "Booster Roles",   module: "./panels/config-booster-roles.js", adminOnly: true },
      { id: "config-xp",            label: "XP Logging",      module: "./panels/config-xp.js", adminOnly: true },
      { id: "config-moderation", label: "Moderation",        module: "./panels/config-moderation.js", adminOnly: true },
      { id: "config-rules-watch", label: "Rules Watch",       module: "./panels/config-rules-watch.js", adminOnly: true },
      { id: "config-greeting-watch", label: "Greeting Watch",  module: "./panels/config-greeting-watch.js", adminOnly: true },
      { id: "config-policy-tickets", label: "Policy Ticket Settings",  module: "./panels/config-policy-tickets.js", adminOnly: true },
      { id: "config-prune",      label: "Inactivity Prune", module: "./panels/config-prune.js", adminOnly: true },
      { id: "config-inactive",   label: "Inactive Sweep",   module: "./panels/config-inactive.js", adminOnly: true },
      { id: "config-spoiler",      label: "Spoiler Guard",     module: "./panels/config-spoiler.js", adminOnly: true },
      { id: "config-auto-role",   label: "Auto-Role",         module: "./panels/config-auto-role.js", adminOnly: true },
      { id: "role-menus",        label: "Role Menus",        module: "./panels/role-menus.js" },
      { id: "config-auto-delete", label: "Auto-Delete",      module: "./panels/config-auto-delete.js", adminOnly: true },
      { id: "config-bulk-cleanup", label: "Bulk Cleanup",     module: "./panels/config-bulk-cleanup.js", adminOnly: true },
      { id: "config-needle",     label: "Auto-Thread",       module: "./panels/config-needle.js", adminOnly: true },
      { id: "config-starboard",  label: "Starboard",         module: "./panels/config-starboard.js", adminOnly: true },
      { id: "chat-revive",       label: "Chat Revive",       module: "./panels/chat-revive.js" },
      { id: "docs",              label: "Docs",              module: "./panels/docs.js" },
      { id: "announcements",     label: "Announcements",     module: "./panels/announcements.js", adminOnly: true },
      { id: "config-voice-master", label: "Voice Master",      module: "./panels/config-voice-master.js", adminOnly: true },
      { id: "config-birthday",   label: "Birthdays",         module: "./panels/config-birthday.js", adminOnly: true },
      { id: "birthday-calendar", label: "Birthday Calendar",  module: "./panels/birthday-calendar.js" },
      { id: "config-bios",       label: "Bios",              module: "./panels/config-bios.js", adminOnly: true },
      { id: "config-voice-transcription", label: "Voice Transcription", module: "./panels/config-voice-transcription.js", adminOnly: true },
      { id: "config-dms",        label: "DM Permissions",   module: "./panels/config-dms.js", adminOnly: true },
      { id: "config-ai",         label: "AI (Local LLM)",    module: "./panels/config-ai.js", primaryOnly: true, adminOnly: true },
      { id: "config-wellness",   label: "Wellness",          module: "./panels/wellness-admin.js", perms: ["manage_server"] },
      { id: "gender-admin",      label: "Gender Tagging",   module: "./panels/gender-admin.js", adminOnly: true },
      { id: "admin-backfill",    label: "Backfill Jobs",     module: "./panels/admin-backfill.js", adminOnly: true },
    ],
  },
  {
    // Shown to admins OR holders of the economy manager role (econManagerRole,
    // mirroring gameHostRole). Manager-visible items carry NO adminOnly/perms
    // so a manager-role holder who isn't an admin keeps them after
    // item-filtering; Settings is adminOnly (its endpoints require admin).
    id: "economy", label: "Economy", perms: ["admin"], econManagerRole: true,
    items: [
      { id: "economy-bank-manager", label: "Operations", module: "./panels/economy-bank-manager.js" },
      { id: "economy-claims", label: "Claims", module: "./panels/economy-claims.js" },
      { id: "economy-quests", label: "Quests", module: "./panels/economy-quests.js" },
      { id: "economy-income-sources", label: "Income Sources", module: "./panels/economy-income-sources.js" },
      { id: "economy-sinks", label: "Sinks", module: "./panels/economy-sinks.js", adminOnly: true },
      { id: "economy-qotd", label: "QOTD", module: "./panels/economy-qotd.js", adminOnly: true },
      { id: "economy-qotd-submissions", label: "Sponsored QOTD", module: "./panels/economy-qotd-submissions.js" },
      { id: "economy-stats", label: "Statistics", module: "./panels/economy-stats.js" },
      { id: "economy-config", label: "Settings", module: "./panels/economy-config.js", adminOnly: true },
    ],
  },
  {
    id: "wellness", label: "Wellness", perms: [], roles: ["Wellness Guardian"],
    items: [
      { id: "wellness-home",      label: "Overview",   module: "./panels/wellness-home.js" },
      { id: "wellness-caps",      label: "Caps",       module: "./panels/wellness-caps.js" },
      { id: "wellness-blackouts", label: "Blackouts",  module: "./panels/wellness-blackouts.js" },
      { id: "wellness-away",      label: "Away",       module: "./panels/wellness-away.js" },
      { id: "wellness-partners",  label: "Partners",   module: "./panels/wellness-partners.js" },
      { id: "wellness-history",   label: "History",    module: "./panels/wellness-history.js" },
    ],
  },
  {
    id: "games", label: "Games", perms: ["admin"], gameHostRole: true,
    items: [
      { id: "games-logs",         label: "Overview & Logs",   module: "./panels/games-logs.js" },
      { id: "games-scheduling",   label: "Scheduling",        module: "./panels/games-scheduling.js" },
      { id: "games-legitlibs",    label: "LegitLibs",         module: "./panels/games-legitlibs.js" },
      { id: "games-config",       label: "Config",            module: "./panels/games-config.js", adminOnly: true },
    ],
    groups: [
      { heading: "Risky Roller", items: [
        { id: "config-risky-rolls",  label: "Config",    module: "./panels/config-risky-rolls.js", adminOnly: true },
      ]},
      { heading: "Pressure Cooker", items: [
        { id: "config-games-pressure", label: "Config", module: "./panels/config-games-pressure.js", adminOnly: true },
      ]},
      { heading: "Quickdraw", items: [
        { id: "config-games-quickdraw", label: "Config", module: "./panels/config-games-quickdraw.js", adminOnly: true },
      ]},
      { heading: "Hot Potato", items: [
        { id: "config-games-hotpotato", label: "Config", module: "./panels/config-games-hotpotato.js", adminOnly: true },
      ]},
      { heading: "Hot Potato (Group)", items: [
        { id: "config-games-hotpotatogroup", label: "Config", module: "./panels/config-games-hotpotatogroup.js", adminOnly: true },
      ]},
      { heading: "Chicken", items: [
        { id: "config-games-chicken", label: "Config", module: "./panels/config-games-chicken.js", adminOnly: true },
      ]},
      { heading: "Musical Chairs", items: [
        { id: "config-games-musicalchairs", label: "Config", module: "./panels/config-games-musicalchairs.js", adminOnly: true },
      ]},
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
      { heading: "FFA / Truth or Dare", items: [
        { id: "games-ffa", label: "Questions", module: "./panels/games-ffa.js" },
      ]},
      { heading: "Traditional Truth or Dare", items: [
        { id: "games-traditional", label: "Questions", module: "./panels/games-traditional.js" },
      ]},
      { heading: "Guess Who", items: [
        { id: "config-guess", label: "Config",     module: "./panels/config-guess.js", perms: ["moderator"] },
      ]},
      { heading: "Whisper", items: [
        { id: "config-whisper",    label: "Config",     module: "./panels/config-whisper.js", perms: ["moderator"] },
      ]},
      { heading: "Confessions", items: [
        { id: "config-confessions",  label: "Config",     module: "./panels/config-confessions.js", adminOnly: true },
      ]},
      { heading: "Pen Pals", items: [
        { id: "config-pen-pals",  label: "Config",     module: "./panels/config-pen-pals.js", adminOnly: true },
        { id: "games-pen-pals",   label: "Questions",  module: "./panels/games-pen-pals.js" },
        { id: "games-pen-pals-studio", label: "Prompts & AI", module: "./panels/games-studio.js", gt: "pen_pals" },
      ]},
    ],
  },
  {
    // Standalone feature — pulled out of the Games menu/scheduler. Same
    // game-host/admin gating as Games (endpoints use require_game_host).
    id: "photo-challenge", label: "Photo Challenge", perms: ["admin"], gameHostRole: true,
    items: [
      { id: "photo-challenge",        label: "Setup & Schedule", module: "./panels/photo-challenge.js" },
      { id: "photo-challenge-studio", label: "Prompts & AI",     module: "./panels/games-studio.js", gt: "photo" },
    ],
  },
  HELP_NAV_SECTION,
  {
    id: "dev", label: "Dev", perms: ["admin"],
    items: [
      { id: "help-owner",    label: "Developer Tools", module: "./panels/help.js" },
      { id: "live-log",      label: "Live Log",        module: "./panels/live-log.js" },
      { id: "system-stats",  label: "System Stats",    module: "./panels/system-stats.js" },
      { id: "qa-tracker",    label: "QA Tracker",      module: "./panels/qa-tracker.js" },
    ],
  },
];

// Flatten all page items for lookup
function allPages(section) {
  const items = section.items || [];
  const grouped = section.groups ? section.groups.flatMap((g) => g.items) : [];
  return [...items, ...grouped];
}

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

  visibleSections = SECTIONS.filter((sec) => {
    // Game host role: show Games section to admins OR configured role holders.
    // NOT moderators — every Games endpoint is gated by require_game_host
    // (admin OR game-host role), which excludes plain moderators, so a
    // moderator-visible Games section would only ever 403 on the backend.
    if (sec.gameHostRole) {
      if (userPerms.has("admin")) return true;
      const hostRoleId = window.__dk_user?.games_editor_role_id;
      return !!(hostRoleId && userRoleIds.has(hostRoleId));
    }

    // Economy manager role: show the Economy section to admins OR the configured
    // manager-role holders (every endpoint is gated by require_economy_manager,
    // which excludes plain moderators — same reasoning as gameHostRole).
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
  });

  // Config is per-guild. For a non-primary guild, show every Config page except
  // those marked `primaryOnly` (genuinely-global settings like the AI models,
  // which live under guild_id=0 and apply bot-wide).
  if (isNonPrimaryGuild) {
    visibleSections = visibleSections
      .map((sec) =>
        sec.id === "config"
          ? { ...sec, items: (sec.items || []).filter((it) => !it.primaryOnly) }
          : sec
      )
      .filter((sec) => sec.id !== "config" || (sec.items && sec.items.length > 0));
  }

  // Per-item permission gating. An item shows only if the user satisfies the
  // section's perms AND the item's own requirements. This hides nav links to
  // pages a user can't actually use even though they can see the section:
  //   - adminOnly: true       → only admins (shorthand for perms: ["admin"])
  //   - perms: ["manage_server", …] → all listed perms required
  // Note `admin` implies moderator/manage_server (see auth.resolve_discord_perms),
  // so admins satisfy every check here.
  const itemAllowed = (it) => {
    if (it.adminOnly && !userPerms.has("admin")) return false;
    if (it.perms && !it.perms.every((p) => userPerms.has(p))) return false;
    return true;
  };
  visibleSections = visibleSections.map((sec) => {
    const newItems = (sec.items || []).filter(itemAllowed);
    const newGroups = sec.groups
      ? sec.groups.map((g) => ({ ...g, items: g.items.filter(itemAllowed) })).filter((g) => g.items.length > 0)
      : sec.groups;
    return { ...sec, items: newItems, groups: newGroups };
  });

  // Drop sections left with nothing to show after item filtering, so we never
  // render an empty section header.
  visibleSections = visibleSections.filter((sec) => allPages(sec).length > 0);

  ALL_PAGES = visibleSections.flatMap(allPages);
  PAGE_TO_SECTION = {};
  for (const sec of visibleSections) {
    for (const page of allPages(sec)) {
      PAGE_TO_SECTION[page.id] = sec;
    }
  }
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

let currentPanel = null;

// ── Sidebar collapse (desktop) + mobile open/close ─────────────────

function closeMobileSidebar() {
  sidebarEl.classList.remove("open");
  sidebarBackdropEl.classList.remove("open");
}

function openMobileSidebar() {
  sidebarEl.classList.add("open");
  sidebarBackdropEl.classList.add("open");
}

sidebarToggleEl.addEventListener("click", (e) => {
  e.stopPropagation();
  if (window.innerWidth <= 768) {
    closeMobileSidebar();
  } else {
    sidebarEl.classList.toggle("collapsed");
  }
});
sidebarBackdropEl.addEventListener("click", closeMobileSidebar);

// Mobile hamburger button
const mobileMenuBtnEl = document.getElementById("mobile-menu-btn");
if (mobileMenuBtnEl) {
  mobileMenuBtnEl.addEventListener("click", openMobileSidebar);
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

if (navFilterEl) {
  navFilterEl.addEventListener("input", () => {
    const q = navFilterEl.value.trim().toLowerCase();
    // While a query is active, matches must show through collapsed groups
    sidebarItemsEl.classList.toggle("filtering", !!q);
    const items = sidebarItemsEl.querySelectorAll(".nav-item");
    items.forEach((it) => {
      const txt = it.dataset.search ||
        it.querySelector(".lbl")?.textContent.toLowerCase() || "";
      it.classList.toggle("filtered-out", !!q && !txt.includes(q));
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
  });
}

// ── Hash parsing ────────────────────────────────────────────────────

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

// Return a copy of the items, sorted alphabetically by label (case-insensitive).
// Copies rather than mutating so the source SECTIONS order is preserved.
function byLabel(items) {
  return [...(items || [])].sort((a, b) =>
    (a.label || "").localeCompare(b.label || "", undefined, { sensitivity: "base" })
  );
}

function makeNavItem(item, activeId, { isSubitem = false } = {}) {
  const btn = document.createElement("button");
  btn.className = "nav-item" + (isSubitem ? " is-subitem" : "");
  btn.type = "button";
  btn.dataset.pageId = item.id;

  const icn = document.createElement("span");
  icn.className = "icn";
  icn.textContent = "#";
  btn.appendChild(icn);

  const lbl = document.createElement("span");
  lbl.className = "lbl";
  lbl.textContent = item.label;
  btn.appendChild(lbl);

  if (item.id === activeId) btn.classList.add("active");

  btn.addEventListener("click", () => {
    const qs = item.gt ? `?gt=${item.gt}` : "";
    window.location.hash = `#/${item.id}${qs}`;
  });
  return btn;
}

function renderNav(activeId) {
  sidebarItemsEl.innerHTML = "";

  const activeSection = PAGE_TO_SECTION[activeId];

  for (const sec of visibleSections) {
    if (sec.direct) {
      const el = makeNavItem(sec.items[0], activeId);
      el.dataset.search = `${sec.label} ${sec.items[0].label}`.toLowerCase();
      el.classList.add("nav-direct");
      sidebarItemsEl.appendChild(el);
      continue;
    }

    const group = document.createElement("div");
    group.className = "nav-group";
    group.textContent = sec.label;
    group.setAttribute("role", "button");
    group.tabIndex = 0;
    // Collapse by default, except the group containing the active page
    const startCollapsed = !activeSection || sec.id !== activeSection.id;
    if (startCollapsed) group.classList.add("collapsed");
    group.setAttribute("aria-expanded", String(!startCollapsed));
    const toggleGroup = () => {
      group.classList.toggle("collapsed");
      const hidden = group.classList.contains("collapsed");
      group.setAttribute("aria-expanded", String(!hidden));
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

    // Top-level items (rendered before any subgroup), alphabetized by label
    for (const item of byLabel(sec.items)) {
      const el = makeNavItem(item, activeId);
      el.dataset.search = `${sec.label} ${item.label}`.toLowerCase();
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
          const el = makeNavItem(item, activeId, { isSubitem: true });
          el.dataset.search = `${sec.label} ${g.heading} ${item.label}`.toLowerCase();
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
    navFilterEl.dispatchEvent(new Event("input"));
  }
}

// ── Mount panel ─────────────────────────────────────────────────────

async function mountPanel() {
  closeMobileSidebar();
  const { id, params } = parseHash();
  const page = ALL_PAGES.find((p) => p.id === id) || ALL_PAGES[0];
  renderNav(page.id);

  if (currentPanel && currentPanel.unmount) {
    try { currentPanel.unmount(); } catch (_) {}
  }
  rootEl.innerHTML = `<div class="panel"><div class="panel-loading">Loading ${esc(page.label)}…</div></div>`;

  try {
    // The ?v=1 literal is rewritten to the current boot id by the server's
    // _CacheBustJS middleware, so each reboot yields a fresh panel URL. Without
    // it, dynamically-imported panels (a variable specifier the import-rewrite
    // regex can't see) would stay immutable-cached forever and never pick up
    // changes to their module graph.
    const mod = await import(`${page.module}?v=3`);
    currentPanel = mod.mount(rootEl, params) || null;
  } catch (err) {
    rootEl.innerHTML = `<div class="panel"><div class="error">Failed to load ${esc(page.label)}: ${esc(err.message)}</div></div>`;
  }
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
    renderNav(parseHash().id);
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
    meEl.innerHTML = `<div class="user-meta"><small style="color:var(--red)">auth error: ${esc(err.message)}</small></div>`;
  }
  window.addEventListener("hashchange", mountPanel);
  mountPanel();
}

boot();
