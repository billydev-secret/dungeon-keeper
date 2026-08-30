// Dashboard boot + hash-based panel router.
import { api, apiPost, esc } from "./api.js";
import { toast } from "./ui.js";
import { HELP_GROUPS, HELP_EXTRA_PAGES, assistantHelpLabel } from "./panels/help-sections.js?v=25";
import { sectionIconNode } from "./nav-icons.js?v=2";
import { setPageIds } from "./nav-registry.js";
import { _resetPanelSpecCache } from "./panel-post.js";
import { resetMetaCaches } from "./config-helpers.js";


// The Help nav is generated from help-sections.js (single source shared with
// the help panel) so nav entries can't drift from the manual's sections.
const _helpNavItem = ({ page, label, order, brand, keywords }) =>
  ({ id: page, label, order, brand, keywords, module: "./panels/help.js" });
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

const SECTIONS = [
  {
    id: "home", label: "Dashboard", perms: [], icon: "⌂",
    items: [
      // "Home", not "Dashboard": the section label above it already says
      // Dashboard, and IA2 fixed this exact collision for Economy's Bank.
      { id: "home", label: "Home", module: "./panels/home.js", keywords: "dashboard overview" },
      { id: "help-quickref", label: "Quick Reference", module: "./panels/help.js" },
    ],
  },
  {
    id: "reports", label: "Reports", perms: ["moderator"], icon: "▤",
    groups: [
      // IA3 (2026-08-29): "General" became "Activity" — the one heading that
      // named nothing — and gained DAU/MAU, its volume-metric sibling.
      // NSFW by Gender moved beside Sentiment & Tone: it is content
      // analytics read with a moderation eye, not an engagement metric.
      { heading: "Moderation", items: [
        { id: "health-sentiment",       label: "Sentiment & Tone",  module: "./panels/health-sentiment.js" },
        { id: "nsfw-gender",          label: "NSFW by Gender",       module: "./panels/nsfw-gender.js" },
        { id: "health-mod-workload",    label: "Mod Workload",       module: "./panels/health-mod-workload.js" },
        { id: "health-mod-engagement",  label: "Mod Engagement",     module: "./panels/health-mod-engagement.js" },
      ]},
      { heading: "Activity", items: [
        { id: "health-heatmap",         label: "Activity Heatmap",   module: "./panels/health-heatmap.js" },
        { id: "activity",             label: "Activity",             module: "./panels/activity.js" },
        { id: "channels",             label: "Channels",             module: "./panels/channels.js", keywords: "channel health comparison staleness" },
        { id: "health-dau-mau",         label: "DAU/MAU",            module: "./panels/health-dau-mau.js", keywords: "daily monthly active users" },
      ]},
      { heading: "Engagement", items: [
        { id: "health-gini",            label: "Participation Gini", module: "./panels/health-gini.js" },
        { id: "retention",            label: "Activity Drops",        module: "./panels/retention.js", keywords: "retention churn drop-off" },
        { id: "voice-activity",       label: "Voice Activity",        module: "./panels/voice-activity.js", keywords: "voice usage peak hours top users", related: ["config-voice-master"] },
        { id: "xp-leaderboard",       label: "XP Leaderboard",       module: "./panels/xp-leaderboard.js", keywords: "levels rank experience", related: ["config-xp"], help: "help-community" },
        { id: "quality-score",        label: "Quality Score",        module: "./panels/quality-score.js" },
      ]},
      { heading: "Social Graph", items: [
        { id: "interaction-graph",    label: "Interactions",          module: "./panels/interaction-graph.js", help: "help-network" },
        // Restored 2026-08-26 under its original id, so deep links saved
        // before it was removed in 5b4cd71d still resolve.
        { id: "connection-graph",     label: "Connection Graph",      module: "./panels/connection-graph.js", help: "help-network", keywords: "network visual map force-directed clusters communities bridge users density reciprocity small-world isolates" },
        { id: "one-sided-attention",  label: "One-Sided Attention",   module: "./panels/one-sided-attention.js" },
      ]},
      { heading: "Greeter", items: [
        { id: "health-newcomer-funnel", label: "Newcomer Funnel",    module: "./panels/health-newcomer-funnel.js" },
        { id: "health-cohort-retention",label: "Cohort Retention",   module: "./panels/health-cohort-retention.js" },
        { id: "greeter-response",     label: "Greeter Response",     module: "./panels/greeter-response.js" },
        { id: "intake-report",        label: "Intake Queue",         module: "./panels/intake-report.js", keywords: "intake queue cards welcome", related: ["config-intake"] },
        { id: "time-to-level5",       label: "Time to Level 5",      module: "./panels/time-to-level5.js" },
        { id: "invite-effectiveness", label: "Invite Effectiveness", module: "./panels/invite-effectiveness.js" },
        { id: "join-times",           label: "Join Times",           module: "./panels/join-times.js" },
      ]},
      { heading: "Member Lists", items: [
        { id: "inactive-report",      label: "Inactive Report",      module: "./panels/inactive-report.js", keywords: "inactive members role list oldest sfw report", related: ["config-inactive", "config-prune"] },
        { id: "birthday-calendar",    label: "Birthday Calendar",    module: "./panels/birthday-calendar.js", keywords: "birthdays report", related: ["config-birthday"] },
      ]},
    ],
  },
  {
    id: "moderation", label: "Moderation", perms: ["moderator"], icon: "⚖",
    // IA3 (2026-08-29): the eight bare items gained the "Queues & Workflows"
    // heading dashboard_ia.md already used for them, making the section
    // groups-only like its siblings. Image Guard's two diagnostic reports
    // left Audit Logs (they're classifier diagnostics, not records of mod
    // actions), and Grant Audit leads Audit Logs — it is the one entry a
    // moderator can open, and it sat below nine locked rows.
    groups: [
      { heading: "Queues & Workflows", items: [
        { id: "mod-todo",       label: "Todo List",      module: "./panels/todo.js", keywords: "tasks board recurring chores qotd reminders", help: "help-todo" },
        { id: "mod-jails",      label: "Jails",          module: "./panels/mod-jails.js", help: "help-jail" },
        { id: "mod-tickets",    label: "Tickets",        module: "./panels/mod-tickets.js", help: "help-tickets", keywords: "post panel support ticket panel open ticket button" },
        { id: "mod-warnings",   label: "Warnings",       module: "./panels/mod-warnings.js", help: "help-tickets" },
        { id: "mod-policy-tickets", label: "Policy Tickets", module: "./panels/policy-tickets.js", help: "help-policies", keywords: "policy proposals votes deadline settings" },
        { id: "rules-watch",    label: "Rules Watch",    module: "./panels/rules-watch.js", help: "help-rules-watch", keywords: "rules watch alerts queue ledger", related: ["config-rules-watch"] },
        { id: "message-search", label: "Message Search",  module: "./panels/message-search.js", keywords: "messages logs find" },
        { id: "no-contact",     label: "No-Contact List", module: "./panels/no-contact.js", help: "help-no-contact", keywords: "block harassment separate pair safety whisper ama confession stalking" },
      ]},
      { heading: "Image Guard", items: [
        { id: "nsfw-blocks",       label: "Image Guard Blocks", module: "./panels/nsfw-blocks-report.js", adminOnly: true, keywords: "blocked images nsfw explicit removed deleted spoiler sfw prevention false positive image guard", related: ["config-spoiler"] },
        { id: "nsfw-tags",         label: "Image Guard Tags",   module: "./panels/nsfw-tags-report.js", adminOnly: true, keywords: "image tags nsfw nudity labels detections classifier metrics score distribution", related: ["config-spoiler"] },
      ]},
      { heading: "Audit Logs", items: [
        { id: "grant-audit",       label: "Grant Audit",      module: "./panels/grant-audit.js", keywords: "role grants audit post panel audit card", related: ["config-roles"] },
        { id: "mod-audit",         label: "Audit Log",        module: "./panels/mod-audit.js", adminOnly: true },
        { id: "mod-dm-audit",      label: "DM Audit",         module: "./panels/mod-dm-audit.js", adminOnly: true },
        { id: "quotes-audit",      label: "Quotes Audit",     module: "./panels/quotes-audit.js", adminOnly: true, related: ["config-quote-border"] },
        { id: "guess-audit",       label: "Guess Who Audit",  module: "./panels/guess-audit.js", adminOnly: true, related: ["config-guess"] },
        { id: "mod-whisper-audit", label: "Whisper Audit",    module: "./panels/mod-whisper-audit.js", adminOnly: true, related: ["config-whisper"] },
        { id: "confessions-audit", label: "Confessions Audit", module: "./panels/mod-confessions-audit.js", adminOnly: true, related: ["config-confessions"] },
        { id: "anon-audit",        label: "Anonymity Audit", module: "./panels/mod-anon-audit.js", adminOnly: true, keywords: "anonymous features ama ffa hot takes fantasies clapback wyr would you rather compliment anonymous audit retention" },
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
        { id: "config-global",     label: "Global",          module: "./panels/config-global.js", adminOnly: true, help: "help-config", keywords: "timezone offset bypass roles mod notification channel bot allowlist greeter role" },
        { id: "config-branding",   label: "Branding",        module: "./panels/config-branding.js", adminOnly: true, keywords: "accent color embed color avatar nickname bot name assistant name casino name" },
        { id: "announcements",     label: "Announcements",     module: "./panels/announcements.js", adminOnly: true, help: "help-announcements" },
        { id: "config-bump-tracker", label: "Bump Tracker",    module: "./panels/config-bump-tracker.js", adminOnly: true, keywords: "bump disboard listing sites reminders" },
      ]},
      { heading: "Roles", items: [
        { id: "config-roles",         label: "Role Grants",      module: "./panels/config-roles.js", adminOnly: true, help: "help-setup", related: ["grant-audit"] },
        { id: "role-menus",        label: "Role Menus",        module: "./panels/role-menus.js", help: "help-role-menus" },
      ]},
      // IA3 (2026-08-29): the set-up-the-newcomer-experience job spanned three
      // headings (Welcome under Members, Auto-Role/Onboarding under Roles,
      // Greeting Watch under Moderation & Safety). One heading now carries it
      // end to end, in the order a newcomer meets each piece.
      { heading: "New Members", items: [
        { id: "config-welcome",    label: "Welcome & Leave",  module: "./panels/config-welcome.js", adminOnly: true, keywords: "greeting join leave messages" },
        // Intake: revived 2026-08-29 with its pre-merge id (deleted by the
        // 2026-07-28 merge with no MOVED_PAGES entry), so old deep links
        // resolve again and the id's telemetry series resumes.
        { id: "config-intake",     label: "Intake Cards",      module: "./panels/intake-settings.js", adminOnly: true, keywords: "intake cards steps procedure reference codes stale nudge", related: ["intake-report"] },
        { id: "config-auto-role",   label: "Auto-Role",         module: "./panels/config-auto-role.js", adminOnly: true, keywords: "autorole join role automatic" },
        { id: "onboarding",        label: "Discord Onboarding", module: "./panels/onboarding.js", adminOnly: true, keywords: "channels and roles customize community server guide opt-in pings" },
        { id: "config-greeting-watch", label: "Greeting Watch",  module: "./panels/config-greeting-watch.js", adminOnly: true, help: "help-greeting-watch", keywords: "unanswered greeting newcomer ping moderation safety" },
      ]},
      { heading: "Members", items: [
        // XP / Birthdays: revived 2026-08-29 with their pre-merge ids — same
        // treatment as config-intake above.
        { id: "config-xp",         label: "XP & Leveling",     module: "./panels/xp-settings.js", adminOnly: true, keywords: "xp levels settings curve rewards", related: ["xp-leaderboard"], help: "help-community" },
        { id: "config-bios",       label: "Bios",              module: "./panels/config-bios.js", adminOnly: true, keywords: "profile introduction icebreaker" },
        { id: "config-birthday",   label: "Birthdays",         module: "./panels/birthday-settings.js", adminOnly: true, keywords: "birthday announcements channel message pin", related: ["birthday-calendar"] },
        { id: "gender-admin",      label: "Gender Tagging",   module: "./panels/gender-admin.js", adminOnly: true },
        { id: "config-wellness",   label: "Wellness",          module: "./panels/wellness-admin.js", perms: ["manage_server"], keywords: "caps limits gambling blackouts", help: "help-wellness" },
        // The inactive pair reads as a parallel pair on purpose: same subject,
        // the differing verb is the distinction. Old labels stay as keywords.
        { id: "config-prune",      label: "Inactive Role Removal", module: "./panels/config-prune.js", adminOnly: true, keywords: "prune auto-remove inactive role removal", related: ["inactive-report"] },
        { id: "config-inactive",   label: "Inactive Kick Sweep",   module: "./panels/config-inactive.js", adminOnly: true, keywords: "inactive sweep purge kick", related: ["inactive-report"] },
      ]},
      { heading: "Moderation & Safety", items: [
        // "& Privacy": this page holds message_storage_level — the biggest
        // privacy dial on the dashboard — which the bare label hid entirely.
        { id: "config-moderation", label: "Moderation & Privacy", module: "./panels/config-moderation.js", adminOnly: true, help: "help-moderation", keywords: "privacy data retention message storage content stored", related: ["config-cleanup", "mod-dm-audit"] },
        // Revived 2026-08-29 with its pre-merge id (deleted by d2348dbf with
        // no MOVED_PAGES entry) — same treatment as config-intake above.
        { id: "config-rules-watch", label: "Rules Watch",      module: "./panels/rules-watch-settings.js", adminOnly: true, help: "help-rules-watch", keywords: "rules watch alerts settings", related: ["rules-watch"] },
        { id: "config-spoiler",      label: "Image Guard",       module: "./panels/config-spoiler.js", adminOnly: true, keywords: "spoiler nsfw nudity explicit classifier", related: ["nsfw-blocks", "nsfw-tags"] },
        { id: "config-dms",        label: "DM Permissions",   module: "./panels/config-dms.js", adminOnly: true, help: "help-dms" },
      ]},
      { heading: "Channels & Messages", items: [
        { id: "config-cleanup",    label: "Cleanup",           module: "./panels/config-cleanup.js", adminOnly: true, keywords: "purge retention delete auto-delete bulk cleanup schedules", help: "help-cleanup" },
        { id: "config-needle",     label: "Auto-Thread",       module: "./panels/config-needle.js", adminOnly: true, keywords: "needle thread replies" },
        { id: "config-auto-react", label: "Auto-React",        module: "./panels/config-auto-react.js", adminOnly: true, keywords: "auto react reactions emoji tips tipping" },
        { id: "config-starboard",  label: "Starboard",         module: "./panels/config-starboard.js", adminOnly: true },
        { id: "chat-revive",       label: "Chat Revive",       module: "./panels/chat-revive.js", keywords: "dead chat prompts", help: "help-chat-revive" },
        { id: "music-playlist",    label: "Music Playlist",    module: "./panels/music-playlist.js", adminOnly: true, help: "help-music-playlist", keywords: "spotify rolling playlist songs tracks watched channel youtube links review unmatched window" },
        { id: "config-quote-border", label: "Quote Tool",     module: "./panels/config-quote-border.js", adminOnly: true, keywords: "quotes border color", related: ["quotes-audit"] },
        { id: "docs",              label: "Docs",              module: "./panels/docs.js", keywords: "channel docs documentation publish" },
      ]},
      { heading: "Voice", items: [
        // Revived 2026-08-29 with its pre-merge id (deleted by 18a3c691 with
        // no MOVED_PAGES entry), so deep links saved before the merge resolve
        // again and its telemetry series resumes.
        { id: "config-voice-master", label: "Voice Control", module: "./panels/voice-settings.js", adminOnly: true, help: "help-voice", keywords: "voice master hub temporary channels post panel owner control panel", related: ["voice-activity"] },
        { id: "config-voice-transcription", label: "Voice Transcription", module: "./panels/config-voice-transcription.js", adminOnly: true },
      ]},
      { heading: "AI & Maintenance", items: [
        // "AI Models" (not "AI (Local LLM)"): this page and "AI Assistant" sit
        // next to each other, and the old label only distinguished them for
        // someone who already knew the architecture. The old wording stays as
        // a search keyword.
        { id: "config-ai",         label: "AI Models",         module: "./panels/config-ai.js", primaryOnly: true, adminOnly: true, keywords: "models prompts llm local llm ai moderation", help: "help-ai" },
        // Neutral label: the assistant's name is per-guild branding now, and
        // the nav is built once from this static list. "Billy-bot" stays as a
        // search keyword so the old name still finds the page.
        { id: "config-advisor",    label: "AI Assistant",      module: "./panels/config-advisor.js", adminOnly: true, keywords: "advisor assistant ai ask billy billy-bot", help: "help-ask" },
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
    // Four subgroups (IA2, 2026-08), the same treatment Games got in IA1: the
    // flat twelve-item list mixed the daily desk with per-feature dials, and
    // a feature's two pages could sit eight entries apart.
    //
    // Headings are the job — run it, pay it out, take it back, wager it —
    // and inside a heading a multi-page feature stays together. Both rules
    // matter: grouping only by job splits Quests from its Claims queue and
    // QOTD from its sponsored queue, which is where someone actually looks
    // for them.
    //
    // Two labels changed with the regroup. "Operations" collided with the
    // heading above it and "Bank" is what the feature is called everywhere
    // else (`/bank`, the bank channel); "Sinks" was economics jargon for the
    // page where everything a member can spend on lives, and it has to read
    // as a sibling of the shop pages landing next to it. Ids are frozen, and
    // both old names survive as search keywords.
    groups: [
      { heading: "Operations", items: [
        { id: "economy-bank-manager", label: "Bank", module: "./panels/economy-bank-manager.js", keywords: "operations bank manager balance grants refunds rentals ledger audit", help: "help-economy" },
        { id: "economy-stats", label: "Statistics", module: "./panels/economy-stats.js", help: "help-economy" },
        { id: "economy-config", label: "Settings", module: "./panels/economy-config.js", adminOnly: true, keywords: "economy currency settings post panel channel panel how-to guide leaderboard perk shop", help: "help-economy" },
      ]},
      // Everything that pays coins out. A feature that spans two pages keeps
      // them adjacent — Claims is the quest sign-off queue and Sponsored QOTD
      // is QOTD's paid queue, so each sits under its own feature rather than
      // being hoisted into a queues-only group.
      { heading: "Earning", items: [
        { id: "economy-income-sources", label: "Income Sources", module: "./panels/economy-income-sources.js", keywords: "faucet rates triggers daily streak login message rewards bonus earn", help: "help-economy" },
        { id: "economy-quests", label: "Quests", module: "./panels/economy-quests.js", keywords: "community goals settle progress payout", help: "help-economy" },
        { id: "economy-claims", label: "Claims", module: "./panels/economy-claims.js", keywords: "quest sign-off queue approve deny pending", help: "help-economy" },
        { id: "mention-awards", label: "Mention Awards", module: "./panels/config-mention-awards.js", adminOnly: true, keywords: "trigger phrase mention pay award hot seat member-run game host" },
        // One page for the feature: the ping role (admins) plus the paid
        // queue (managers too). The separate adminOnly `economy-qotd` page
        // owned a single role id and is retired into this one — see
        // MOVED_PAGES for the deep link.
        { id: "economy-qotd-submissions", label: "QOTD", module: "./panels/economy-qotd-submissions.js", keywords: "question of the day qotd ping role sponsored paid queue submissions approve decline" },
      ]},
      // Everything a member can spend on. One 1,339-line page until it was split
      // three ways by what each part IS: work you action, things you curate,
      // numbers you set. `order` states that frequency rather than leaving it to
      // alphabetical accident — the queue is what you open most and the prices
      // are what you open least.
      { heading: "Spending", items: [
        // Deliberately NOT adminOnly. /api/economy/emoji-submissions is gated
        // `require_economy_manager`, so the backend lets a manager work this
        // queue — but it lived on the adminOnly page below, which meant a
        // manager could never reach it. Claims and QOTD, the comparable queues,
        // are not adminOnly either.
        { id: "shop-approvals", label: "Approvals", order: 1, module: "./panels/shop-approvals.js", keywords: "emoji submissions approve deny queue orders waiting staff fulfil refund sponsored", help: "help-economy" },
        { id: "economy-sinks", label: "Shop & Perks", order: 2, module: "./panels/economy-sinks.js", adminOnly: true, keywords: "sinks shop perks icons catalog palette colors swatches custom items store", help: "help-economy" },
        { id: "pricing", label: "Pricing", order: 3, module: "./panels/pricing.js", adminOnly: true, keywords: "prices perk rent consumables raffle hoard tax demurrage rake sponsored", help: "help-economy" },
      ]},
      // Staking coins on an outcome — the house takes a cut, so these are
      // sinks too, but they are run and tuned as games.
      { heading: "Wagering", items: [
        { id: "config-casino", label: "Casino", module: "./panels/config-casino.js", adminOnly: true, keywords: "gambling slots blackjack roulette keno dice mines baccarat war race", help: "help-casino", related: ["config-pools"] },
        // Keywords lean market-specific: "pools" alone also matches confession
        // pools and the pen-pals pool.
        { id: "config-pools", label: "Pools", module: "./panels/config-pools.js", adminOnly: true, keywords: "prediction market daily over under parimutuel takeout burn", help: "help-pools", related: ["config-casino"] },
      ]},
    ],
  },
  {
    id: "wellness", label: "Wellness", perms: [], wellnessGate: true, icon: "♥",
    items: [
      { id: "wellness-home",      label: "Overview",   module: "./panels/wellness-home.js", help: "help-wellness" },
      { id: "wellness-caps",      label: "Caps",       module: "./panels/wellness-caps.js", help: "help-wellness" },
      { id: "wellness-blackouts", label: "Blackouts",  module: "./panels/wellness-blackouts.js", help: "help-wellness" },
      { id: "wellness-away",      label: "Away",       module: "./panels/wellness-away.js", help: "help-wellness" },
      { id: "wellness-history",   label: "History",    module: "./panels/wellness-history.js", help: "help-wellness" },
    ],
  },
  {
    // Section gate: admins OR configured game-host role holders — every Games
    // endpoint is gated by require_game_host. `games-external` carries an
    // explicit `perms` list so it survives a failed section gate: external
    // result tracking is a moderator job, and its backend is moderator-gated.
    //
    // Three subgroups (IA1, 2026-08). The flat list had grown to 23 entries
    // mixing ops pages, per-game dials and question banks; the four social
    // features that were parked here moved to their own Social section below.
    id: "games", label: "Games", perms: ["admin"], gameHostRole: true, icon: "⚄",
    groups: [
      { heading: "Operations", items: [
        { id: "games-logs",         label: "Overview & Logs",   module: "./panels/games-logs.js", help: "help-games" },
        { id: "games-scheduling",   label: "Scheduling",        module: "./panels/games-scheduling.js", help: "help-games" },
        { id: "games-config",       label: "Global Config",     module: "./panels/games-config.js", adminOnly: true, help: "help-games" },
        { id: "games-external",     label: "External Tracking", module: "./panels/games-external.js", perms: ["moderator"], keywords: "track external bot results bank payout watch channel" },
        // IA3 (2026-08-29): the one game-night page that lived outside Games.
        // It echoes game events into main chat, so it works the same shift as
        // Scheduling; adminOnly keeps its gate, the section move only costs
        // non-host moderators the locked-entry visibility.
        { id: "config-event-echo", label: "Event Echo",        module: "./panels/config-event-echo.js", adminOnly: true, keywords: "echo announce games main chat jump link", help: "help-event-echo" },
      ]},
      // One page per game: the dials for a game that runs live in a channel.
      { heading: "Live Games", items: [
        { id: "games-legitlibs",    label: "LegitLibs",         module: "./panels/games-legitlibs.js", keywords: "mad libs madlibs templates blanks" },
        { id: "config-risky-rolls",  label: "Risky Rolls",     module: "./panels/config-risky-rolls.js", adminOnly: true },
        { id: "config-games-pressure", label: "Pressure Cooker", module: "./panels/config-games-pressure.js", adminOnly: true },
        { id: "config-games-quickdraw", label: "Quickdraw", module: "./panels/config-games-quickdraw.js", adminOnly: true },
        { id: "config-games-hotpotato", label: "Hot Potato", module: "./panels/config-games-hotpotato.js", adminOnly: true },
        { id: "config-games-hotpotatogroup", label: "Hot Potato (Group)", module: "./panels/config-games-hotpotatogroup.js", adminOnly: true },
        { id: "config-games-chicken", label: "Chicken", module: "./panels/config-games-chicken.js", adminOnly: true },
        { id: "config-games-musicalchairs", label: "Musical Chairs", module: "./panels/config-games-musicalchairs.js", adminOnly: true },
        // Was its own one-item top-level section; the gate is identical
        // (admins or the game-host role), so it folds in here rather than
        // keeping a heading to itself.
        { id: "photo-challenge",    label: "Photo Challenge",   module: "./panels/photo-challenge.js", help: "help-photo", keywords: "setup schedule photo theme" },
        { id: "survivor",           label: "Survivor",          module: "./panels/survivor.js", adminOnly: true, help: "help-survivor", keywords: "nfl football pickem survival pool season reckoning" },
        { id: "mahjong",            label: "Meadow Mahjong",    module: "./panels/mahjong.js", adminOnly: true, help: "help-mahjong", keywords: "mahjong tiles card charleston duel stakes escrow" },
      ]},
      // Question-bank games: one page of prompts each, no live channel state.
      { heading: "Question Banks", items: [
        { id: "games-wyr",      label: "Would You Rather",  module: "./panels/games-wyr.js" },
        { id: "games-nhie",     label: "Never Have I Ever", module: "./panels/games-nhie.js" },
        { id: "games-mlt",      label: "Most Likely To",    module: "./panels/games-mlt.js" },
        { id: "games-rushmore", label: "Rushmore",          module: "./panels/games-rushmore.js" },
        { id: "games-price",    label: "Price",             module: "./panels/games-price.js" },
        { id: "games-clapback", label: "Clapback",          module: "./panels/games-clapback.js" },
        { id: "games-ama",      label: "AMA",               module: "./panels/games-ama.js" },
        { id: "games-ffa", label: "FFA / Truth or Dare", module: "./panels/games-ffa.js" },
        { id: "games-traditional", label: "Traditional Truth or Dare", module: "./panels/games-traditional.js" },
      ]},
    ],
  },
  {
    // The anonymous / pairing features. They lived under Games because that's
    // where they were built, but none of them is a game: each is an ongoing
    // social surface a moderator runs, and Confessions' audit trail already
    // lives under Moderation. A moderator section gate is their natural home —
    // it also retires the per-item `perms: ["moderator"]` markers they needed
    // purely to survive the Games host gate.
    //
    // Confessions keeps `adminOnly` (its endpoints require admin), so
    // moderators get the standard locked entry rather than a page they can
    // open — the same treatment Confessions Audit already gets next door.
    id: "social", label: "Social", perms: ["moderator"], icon: "☺",
    items: [
      { id: "config-guess", label: "Guess Who", module: "./panels/config-guess.js", help: "help-guess", keywords: "post panel submit prompt games", related: ["guess-audit"] },
      { id: "config-whisper",    label: "Whisper",     module: "./panels/config-whisper.js", help: "help-whisper", keywords: "anonymous message games", related: ["mod-whisper-audit"] },
      { id: "pen-pals",          label: "Pen Pals",    module: "./panels/pen-pals.js", help: "help-pen-pals", keywords: "pen pals matching questions conversation starters games" },
      { id: "config-confessions",  label: "Confessions",     module: "./panels/config-confessions.js", adminOnly: true, help: "help-confessions", keywords: "anonymous confession games", related: ["confessions-audit"] },
    ],
  },
  HELP_NAV_SECTION,
  {
    id: "dev", label: "Dev", perms: ["admin"], icon: "⚒",
    items: [
      { id: "help-owner",    label: "Developer Tools", module: "./panels/help.js" },
      { id: "live-log",      label: "Live Log",        module: "./panels/live-log.js", keywords: "console output tail" },
      { id: "system-stats",  label: "System Stats",    module: "./panels/system-stats.js" },
      // IA3 (2026-08-29): moved from Reports, where it was the sole entry of
      // a one-item adminOnly heading in the moderators' section. Its job —
      // which commands and panels are dead — is owner tooling.
      { id: "usage-telemetry", label: "Command & Panel Usage", module: "./panels/usage-telemetry.js", adminOnly: true, help: "help-usage-telemetry", keywords: "telemetry slash commands dashboard panels unused dead never used analytics bot usage" },
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
// Publish the unfiltered id list for the usage report's never-opened list.
// Unfiltered on purpose: ALL_PAGES is narrowed to what the current viewer may
// see, which would make every admin-only panel look "never opened".
setPageIds(FULL_PAGE_INDEX.keys());
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
    // Wellness: opted-in members (server truth from /api/me, NOT a role-name
    // string match — the role is assigned by id and can be renamed freely).
    // Admins see it too so the owner can inspect the member surface.
    if (sec.wellnessGate) {
      if (userPerms.has("manage_server") || userPerms.has("admin")) return true;
      return !!window.__dk_user?.wellness_opted_in;
    }
    return !sec.perms || sec.perms.length === 0 || sec.perms.every((p) => userPerms.has(p));
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
let currentPageId = null;

// ── Dev-only unmount tripwire (F3) ──────────────────────────────────
// Leaked polls and ResizeObservers keep coming back: a panel arms one, returns
// no handle, and every visit adds another that runs forever. Counting the
// registrations a panel makes while it is mounted and complaining at
// navigation time — when we know whether it handed back an unmount() — catches
// the whole family, including the ones armed inside an async load().
//
// Localhost only, so it can never add noise to a production console, and it
// only ever warns.
const DEV_HOST = ["localhost", "127.0.0.1", "[::1]", ""].includes(location.hostname);
let _panelSideEffects = 0;

if (DEV_HOST) {
  const _setInterval = window.setInterval;
  window.setInterval = function (...args) {
    _panelSideEffects++;
    return _setInterval.apply(this, args);
  };
  if (window.ResizeObserver) {
    const _RO = window.ResizeObserver;
    window.ResizeObserver = class extends _RO {
      constructor(...args) { super(...args); _panelSideEffects++; }
    };
  }
}

function warnIfLeaky() {
  if (!DEV_HOST) return;
  if (_panelSideEffects > 0 && !currentPanel?.unmount) {
    console.warn(
      `[dk] panel "${currentPageId}" armed ${_panelSideEffects} timer(s)/observer(s) `
      + "but returned no unmount() handle — they keep running after navigation. "
      + "Return { unmount() { … } } from mount().",
    );
  }
  _panelSideEffects = 0;
}

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
// dataset.search on each item = section + subgroup + label + id + keywords.
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

// ── Command palette (Ctrl/Cmd+K) ────────────────────────────────────
//
// Strictly additive to the sidebar filter above, which keeps working exactly as
// it did. The difference is what it searches and how it answers: a flat ranked
// list of "Section › Label" across the *whole* nav — no expanding groups, no
// scrolling the tree — plus a second tier over the manual's headings, so
// "how do I jail someone" lands on the guide as readily as on the page.
//
// It renders ALL_PAGES, which rebuildIndex() has already filtered to what this
// viewer may open (locked admin-only entries are excluded), so the palette can
// never surface a page the nav wouldn't.
//
// Combobox semantics (WAI-ARIA): focus stays in the input, results are options
// referenced by aria-activedescendant, arrows move the selection, Enter opens,
// Escape closes and returns focus where it came from.

const PALETTE_TIER_LIMIT = 8;

let paletteEl = null;
let paletteInput = null;
let paletteListEl = null;
let paletteEmptyEl = null;
let paletteResults = [];
let paletteIndex = 0;
let paletteReturnFocus = null;
let paletteManual = null;   // cached manual headings, loaded on first query
let paletteQueryToken = 0;

function palettePageResults(tokens) {
  const first = tokens[0];
  const out = [];
  for (const p of ALL_PAGES) {
    const sec = PAGE_TO_SECTION[p.id];
    const label = (p.label || "").toLowerCase();
    // The id is part of the haystack: ids appear in deep links, docs and
    // telemetry, and where label and id have drifted ("shop-approvals" is
    // labelled "Approvals") the id is the term people actually hold.
    const hay = `${sec ? sec.label : ""} ${p.label} ${p.id} ${p.keywords || ""}`.toLowerCase();
    if (!tokens.every((t) => hay.includes(t))) continue;
    out.push({
      label: p.label,
      context: sec ? sec.label : "",
      href: `#/${p.id}`,
      // A label that starts with what you typed is what you meant; a
      // keyword-only hit is the long tail.
      rank: label.startsWith(first) ? 0 : label.includes(first) ? 1 : 2,
    });
  }
  out.sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label));
  return out.slice(0, PALETTE_TIER_LIMIT);
}

async function paletteManualResults(tokens) {
  if (!paletteManual) {
    try {
      // Same specifier the router uses, so this shares the panel's module
      // instance (and its single parse of manual.html) rather than making a
      // second copy.
      const mod = await import("./panels/help.js?v=3");
      paletteManual = await mod.manualHeadings();
    } catch (_) {
      paletteManual = [];
    }
  }
  const visible = window.__dkVisiblePages;
  const out = [];
  for (const entry of paletteManual) {
    if (visible && !visible.has(entry.page)) continue;
    const hay = entry.path.toLowerCase();
    if (!tokens.every((t) => hay.includes(t))) continue;
    out.push({
      label: entry.title,
      context: `Guide — ${entry.path}`,
      href: `#/${entry.page}?focus=${encodeURIComponent(entry.anchor)}`,
      rank: entry.title.toLowerCase().startsWith(tokens[0]) ? 0 : 1,
    });
  }
  out.sort((a, b) => a.rank - b.rank || a.label.localeCompare(b.label));
  return out.slice(0, PALETTE_TIER_LIMIT);
}

function renderPaletteResults() {
  paletteListEl.replaceChildren();
  paletteEmptyEl.hidden = paletteResults.length > 0;
  paletteEmptyEl.textContent = paletteInput.value.trim()
    ? "No page or guide section matches — try a feature word, or an old name for it."
    : "Type to search every page you can open, and the reference guide.";
  paletteResults.forEach((r, i) => {
    const opt = document.createElement("div");
    opt.className = "dk-palette-option" + (i === paletteIndex ? " active" : "");
    opt.id = `dk-palette-opt-${i}`;
    opt.setAttribute("role", "option");
    opt.setAttribute("aria-selected", i === paletteIndex ? "true" : "false");
    opt.dataset.href = r.href;
    opt.style.cssText =
      "display:flex;flex-direction:column;gap:2px;padding:8px 12px;cursor:pointer;border-radius:var(--r-sm);"
      + (i === paletteIndex ? "background:var(--active);" : "");
    const label = document.createElement("span");
    label.textContent = r.label;
    label.style.cssText = "color:var(--ink-bright);";
    const ctx = document.createElement("span");
    ctx.textContent = r.context;
    ctx.style.cssText = "color:var(--ink-mute);font-size:12px;";
    opt.append(label, ctx);
    opt.addEventListener("mousedown", (e) => {
      e.preventDefault(); // keep focus in the input until we navigate
      paletteIndex = i;
      openPaletteSelection();
    });
    paletteListEl.appendChild(opt);
  });
  paletteInput.setAttribute(
    "aria-activedescendant",
    paletteResults.length ? `dk-palette-opt-${paletteIndex}` : ""
  );
  paletteListEl.querySelector(".dk-palette-option.active")?.scrollIntoView({ block: "nearest" });
}

async function runPaletteQuery() {
  const token = ++paletteQueryToken;
  const tokens = paletteInput.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!tokens.length) {
    paletteResults = [];
    paletteIndex = 0;
    renderPaletteResults();
    return;
  }
  paletteResults = palettePageResults(tokens);
  paletteIndex = 0;
  renderPaletteResults();
  const manual = await paletteManualResults(tokens);
  // The manual index loads asynchronously; a newer keystroke wins.
  if (token !== paletteQueryToken || !paletteEl) return;
  paletteResults = [...palettePageResults(tokens), ...manual];
  if (paletteIndex >= paletteResults.length) paletteIndex = 0;
  renderPaletteResults();
}

function openPaletteSelection() {
  const target = paletteResults[paletteIndex];
  if (!target) return;
  closePalette();
  // Assigning an identical hash fires no hashchange, so nudge the router.
  if (window.location.hash === target.href) mountPanel();
  else window.location.hash = target.href;
}

function closePalette() {
  if (!paletteEl) return;
  paletteEl.remove();
  paletteEl = paletteInput = paletteListEl = paletteEmptyEl = null;
  paletteResults = [];
  const back = paletteReturnFocus;
  paletteReturnFocus = null;
  if (back && document.contains(back)) back.focus();
}

function openPalette() {
  if (paletteEl) return;
  paletteReturnFocus = document.activeElement;

  paletteEl = document.createElement("div");
  paletteEl.className = "dk-palette-backdrop";
  paletteEl.style.cssText =
    "position:fixed;inset:0;z-index:80;background:rgba(0,0,0,0.55);display:flex;"
    + "align-items:flex-start;justify-content:center;padding:12vh 16px 16px;";

  const box = document.createElement("div");
  box.className = "dk-palette";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-label", "Search pages and the guide");
  box.style.cssText =
    "width:min(560px,100%);max-height:70vh;display:flex;flex-direction:column;overflow:hidden;"
    + "background:var(--bg-card);border:1px solid var(--rule);border-radius:var(--r);"
    + "box-shadow:0 16px 48px rgba(0,0,0,0.5);";

  paletteInput = document.createElement("input");
  paletteInput.type = "text";
  paletteInput.className = "dk-palette-input";
  paletteInput.placeholder = "Jump to a page or the guide…";
  paletteInput.setAttribute("aria-label", "Search pages and the guide");
  paletteInput.setAttribute("role", "combobox");
  paletteInput.setAttribute("aria-expanded", "true");
  paletteInput.setAttribute("aria-controls", "dk-palette-list");
  paletteInput.setAttribute("aria-autocomplete", "list");
  paletteInput.autocomplete = "off";
  paletteInput.style.cssText =
    "width:100%;padding:14px 16px;border:0;border-bottom:1px solid var(--rule);"
    + "background:var(--bg-input);color:var(--ink-bright);font-size:15px;outline:none;";

  paletteListEl = document.createElement("div");
  paletteListEl.id = "dk-palette-list";
  paletteListEl.setAttribute("role", "listbox");
  paletteListEl.setAttribute("aria-label", "Results");
  paletteListEl.style.cssText = "overflow-y:auto;padding:6px;";

  paletteEmptyEl = document.createElement("div");
  paletteEmptyEl.className = "dk-palette-empty";
  paletteEmptyEl.textContent =
    "Type to search every page you can open, and the reference guide.";
  paletteEmptyEl.style.cssText = "padding:14px 16px;color:var(--ink-mute);font-size:13px;";

  box.append(paletteInput, paletteListEl, paletteEmptyEl);
  paletteEl.appendChild(box);

  // Click outside closes; clicks inside must not bubble to the backdrop.
  paletteEl.addEventListener("mousedown", (e) => {
    if (e.target === paletteEl) closePalette();
  });

  paletteEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closePalette();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!paletteResults.length) return;
      const next = paletteIndex + (e.key === "ArrowDown" ? 1 : -1);
      paletteIndex = (next + paletteResults.length) % paletteResults.length;
      renderPaletteResults();
    } else if (e.key === "Enter") {
      e.preventDefault();
      openPaletteSelection();
    } else if (e.key === "Tab") {
      // Focus trap: the dialog's only focusable control is the input, per the
      // combobox pattern — results are reached with the arrow keys.
      e.preventDefault();
      paletteInput.focus();
    }
  });
  paletteInput.addEventListener("input", runPaletteQuery);

  document.body.appendChild(paletteEl);
  paletteInput.focus();
}

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "k" || e.key === "K")) {
    e.preventDefault();
    if (paletteEl) closePalette();
    else openPalette();
  }
});

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

function makeNavItem(item, activeId, { isSubitem = false, icon = "#", sectionId = null } = {}) {
  const btn = document.createElement("button");
  btn.className = "nav-item" + (isSubitem ? " is-subitem" : "");
  btn.type = "button";
  btn.dataset.pageId = item.id;
  // Tooltip label — the only visible label on the collapsed rail (W-N3).
  btn.title = item.locked ? `${item.label} — Admin only` : item.label;

  const icn = document.createElement("span");
  icn.className = "icn";
  // A fresh clone per item. Resolving one node per section and appending it to
  // every item would MOVE it, leaving the icon only on the last item in the
  // section; sectionIconNode parses once and clones, so this stays cheap.
  const iconNode = sectionId ? sectionIconNode(sectionId) : null;
  if (iconNode) {
    icn.appendChild(iconNode);
  } else {
    // Only reachable for a section with no drawn icon. test_nav_icons.py fails
    // the build in that case, so this is belt-and-braces for a section added at
    // runtime rather than a path the shipped nav takes.
    icn.textContent = icon;
  }
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

  if (item.id === activeId) {
    btn.classList.add("active");
    // The gold marker and the widened section heading are both purely visual.
    // Without this a screen reader hears ~176 identical buttons with nothing
    // saying which one is the page you are on.
    btn.setAttribute("aria-current", "page");
  }

  btn.addEventListener("click", () => {
    window.location.hash = `#/${item.id}`;
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
    // The icon identifies the SECTION, so it lives on the section header. It
    // used to be stamped on every item inside the section instead, which drew
    // eight identical shields down Moderation and said nothing about any of
    // them. Items keep an icon element for the collapsed rail, where it is the
    // only identifier a page has — see .nav-item .icn svg in app.css.
    const secSvg = sectionIconNode(sec.id);
    if (secSvg) {
      const si = document.createElement("span");
      si.className = "sec-icn";
      si.appendChild(secSvg);
      si.setAttribute("aria-hidden", "true");
      group.appendChild(si);
    }
    const secLbl = document.createElement("span");
    secLbl.className = "sec-lbl";
    secLbl.textContent = sec.label;
    group.appendChild(secLbl);
    group.setAttribute("role", "button");
    group.tabIndex = 0;
    // The rail marks where you are by setting this header wider (Archivo's
    // wdth axis, see .nav-group.current in app.css). It is deliberately not
    // keyed off aria-expanded: several sections can be open at once, so
    // "expanded" and "the section I am in" are different questions.
    if (activeSection && sec.id === activeSection.id) {
      group.classList.add("current");
      group.setAttribute("aria-current", "true");
    }

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
      const el = makeNavItem(item, activeId, { icon, sectionId: sec.id });
      el.dataset.search = `${sec.label} ${item.label} ${item.id} ${item.keywords || ""}`.trim().toLowerCase();
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
          const el = makeNavItem(item, activeId, { isSubitem: true, icon, sectionId: sec.id });
          el.dataset.search =
            `${sec.label} ${g.heading} ${item.label} ${item.id} ${item.keywords || ""}`.trim().toLowerCase();
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

// Fire-and-forget panel-view ping. Deliberately not awaited and errors are
// swallowed: telemetry must never delay a panel or surface an error toast.
// One row per mount — see web_server/routes/telemetry.py for why this isn't
// middleware over every request.
function recordPanelView(pageId) {
  // Ingest is moderator-gated so an unprivileged writer can't invalidate the
  // never-opened list. Skip the request for members who'd only get a 403 —
  // regular members do reach the dashboard for the Wellness section.
  if (!userPerms.has("moderator") && !userPerms.has("admin")) return;
  // apiPost is async, so it can only reject — .catch() is the whole guard.
  apiPost("/api/telemetry/panel", { panel: pageId }).catch(() => {});
}

// Pages that were split up rather than deleted. A bookmark to one would
// otherwise land on "this page doesn't exist", which is true but unhelpful —
// send it to the page that inherited most of what it did.
//   channel-panels: retired 2026-07-28, its seven post controls moved onto the
//   config page of the feature each panel belongs to. Economy took three of
//   them, so it's the closest thing to a successor.
//   economy-qotd: retired 2026-08-25, an 88-line page owning one role id. Its
//   settings card is now the top of the QOTD page, which already held the
//   sponsored queue — a true successor, not just the nearest one.
const MOVED_PAGES = {
  "channel-panels": "economy-config",
  "economy-qotd": "economy-qotd-submissions",
  // config-policy-tickets: retired 2026-07-28 when its one field (the voting
  // deadline) folded into the queue page — the only retirement that never got
  // a redirect until the 2026-08-29 IA audit found its dead deep links.
  "config-policy-tickets": "mod-policy-tickets",
  // health-composite-score: dropped 2026-08-26 (e6c06624), the blend
  // dissolved into its six metrics; DAU/MAU is the first of them.
  "health-composite-score": "health-dau-mau",
  // config-booster-roles: booster colors became the rentable Palette perk
  // (2a83904b), sold from Shop & Perks.
  "config-booster-roles": "economy-sinks",
};

/** Rewrite a retired page's hash to its successor. True if it redirected. */
function redirectMovedPage() {
  const { id } = parseHash();
  // hasOwn, not a bare lookup: ids like "toString" or "constructor" hit
  // Object.prototype and would "redirect" to a stringified native function
  // instead of rendering the page-unavailable notice.
  if (!Object.hasOwn(MOVED_PAGES, id)) return false;
  const to = MOVED_PAGES[id];
  window.location.replace(`#/${to}`);
  return true;
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
  if (redirectMovedPage()) return; // hashchange remounts on the new id
  const { id, params } = parseHash();
  const page =
    ALL_PAGES.find((p) => p.id === id) || EXTRA_ROUTES.find((p) => p.id === id);

  warnIfLeaky();
  if (currentPanel && currentPanel.unmount) {
    try { currentPanel.unmount(); } catch (_) {}
  }
  currentPanel = null;
  currentPageId = null;

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
    _panelSideEffects = 0; // anything armed from here on belongs to this panel
    currentPanel = mod.mount(rootEl, params) || null;
    currentPageId = page.id;
    renderPanelMeta(page);
    setDocTitle(page.label);
    recordPanelView(page.id);
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
    wellness_opted_in: !!me.wellness_opted_in,
  };

  // The panel registry resolves a spec's grant-role choices from the active
  // guild's config, and switchGuild re-mounts panels without reloading the
  // page — so the cached /api/panels payload has to go with the old guild.
  _resetPanelSpecCache();
  // Same reasoning, sharper consequence: /api/config and every /api/meta/*
  // list is scoped to the active guild, and config-helpers memoizes them in
  // module globals. Carried across a switch they made config panels list the
  // *previous* guild's channels/roles/members, and a save wrote a foreign
  // guild's snowflake into the new guild's config (S2).
  resetMetaCaches();

  // Recompute visible nav (Config pages are filtered per primary/non-primary)
  rebuildIndex();
  // Per-guild assistant branding for the Help nav (IA5). Deliberately not
  // awaited: the nav renders immediately with the default name and re-labels
  // itself if this guild calls the assistant something else.
  applyAssistantBrand();
}

// The Help nav used to hardcode "Ask Billy-bot (AI)" while the Config side had
// already been neutralised for per-guild branding. The nav is built once from a
// static list, so the name is patched in after the fact — and again on every
// guild switch, since branding is per guild.
let _brandedAssistantName = null;

async function applyAssistantBrand() {
  let name;
  try {
    const res = await api("/api/help/advisor/name");
    name = res && res.assistant_name;
  } catch (_) {
    return; // keep the default label; a nav label is never worth an error
  }
  if (!name || name === _brandedAssistantName) return;
  _brandedAssistantName = name;
  const label = assistantHelpLabel(name);
  let changed = false;
  for (const item of HELP_NAV_SECTION.items) {
    if (item.brand === "assistant" && item.label !== label) {
      item.label = label;
      changed = true;
    }
  }
  if (!changed) return;
  rebuildIndex();
  renderNav(parseHash().id);
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
