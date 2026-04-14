// Widget registry — single catalog of all available homepage widgets.
// Each entry defines metadata used by the grid renderer and layout persistence.

const WIDGETS = [
  // ── Home tiles ───────────────────────────────────────────────────
  { id: "home-messages",    label: "Messages (24h)",          category: "Home", perms: [], source: "home", wide: false, nav: "activity" },
  { id: "home-nsfw",        label: "NSFW (24h)",              category: "Home", perms: [], source: "home", wide: false, nav: "nsfw-gender" },
  { id: "home-presence",    label: "Presence",                category: "Home", perms: [], source: "home", wide: false, nav: "health-dau-mau" },
  { id: "home-xp",          label: "XP Today",                category: "Home", perms: [], source: "home", wide: false, nav: "xp-leaderboard" },
  { id: "home-joins",       label: "Recent Joins",            category: "Home", perms: [], source: "home", wide: false, nav: "join-times" },
  { id: "home-moderation",  label: "Moderation",              category: "Home", perms: [], source: "home", wide: false, nav: "mod-jails" },
  { id: "home-voice",       label: "In Voice Now",            category: "Home", perms: [], source: "home", wide: false, nav: "voice-activity" },
  { id: "home-channels",    label: "Hottest Channels (1h)",   category: "Home", perms: [], source: "home", wide: false, nav: "channel-comparison" },
  { id: "home-users",       label: "Most Active Users (1h)",  category: "Home", perms: [], source: "home", wide: false, nav: "activity" },
  { id: "home-returned",    label: "Returned After Break",    category: "Home", perms: [], source: "home", wide: false, nav: "retention" },
  { id: "home-starters",    label: "Conversation Starters",   category: "Home", perms: [], source: "home", wide: false, nav: "interaction-graph" },
  { id: "home-butterflies", label: "Social Butterflies",      category: "Home", perms: [], source: "home", wide: false, nav: "connection-graph" },
  { id: "home-loyalists",   label: "Channel Loyalists",       category: "Home", perms: [], source: "home", wide: false, nav: "health-channel-health" },
  { id: "home-mod-actions", label: "Recent Mod Actions",      category: "Home", perms: [], source: "home", wide: true,  nav: "mod-audit" },

  // ── Health tiles ─────────────────────────────────────────────────
  { id: "health-composite",        label: "Community Health",    category: "Health", perms: ["admin"], source: "health", tileKey: "composite",        wide: true,  nav: "health-composite-score", needsNames: false },
  { id: "health-dau-mau",          label: "DAU/MAU Stickiness",  category: "Health", perms: ["admin"], source: "health", tileKey: "dau_mau",          wide: false, nav: "health-dau-mau",         needsNames: false },
  { id: "health-heatmap",          label: "Activity Heatmap",    category: "Health", perms: ["admin"], source: "health", tileKey: "heatmap",          wide: false, nav: "health-heatmap",         needsNames: false },
  { id: "health-gini",             label: "Participation Gini",  category: "Health", perms: ["admin"], source: "health", tileKey: "gini",             wide: false, nav: "health-gini",            needsNames: false },
  { id: "health-channel-health",   label: "Channel Health",      category: "Health", perms: ["admin"], source: "health", tileKey: "channel_health",   wide: false, nav: "health-channel-health",  needsNames: true  },
  { id: "health-social-graph",     label: "Social Graph",        category: "Health", perms: ["admin"], source: "health", tileKey: "social_graph",     wide: false, nav: "connection-graph",       needsNames: false },
  { id: "health-sentiment",        label: "Sentiment & Tone",    category: "Health", perms: ["admin"], source: "health", tileKey: "sentiment",        wide: false, nav: "health-sentiment",       needsNames: true  },
  { id: "health-newcomer-funnel",  label: "Newcomer Funnel",     category: "Health", perms: ["admin"], source: "health", tileKey: "newcomer_funnel",  wide: false, nav: "health-newcomer-funnel", needsNames: false },
  { id: "health-cohort-retention", label: "Cohort Retention",    category: "Health", perms: ["admin"], source: "health", tileKey: "cohort_retention", wide: false, nav: "health-cohort-retention",needsNames: false },
  { id: "health-churn-risk",       label: "Churn Risk",          category: "Health", perms: ["admin"], source: "health", tileKey: "churn_risk",       wide: false, nav: "health-churn-risk",      needsNames: false },
  { id: "health-mod-workload",     label: "Mod Workload",        category: "Health", perms: ["admin"], source: "health", tileKey: "mod_workload",     wide: false, nav: "health-mod-workload",    needsNames: true  },
  { id: "health-incidents",        label: "Incidents",           category: "Health", perms: ["admin"], source: "health", tileKey: "incidents",        wide: false, nav: "health-incidents",       needsNames: false },
  { id: "health-sentiment-feed",  label: "Sentiment Feed",      category: "Health", perms: ["admin"], source: "health", tileKey: "sentiment_feed",   wide: true,  nav: "health-sentiment-feed", needsNames: true,  maxRows: 4 },
  { id: "health-message-feed",   label: "Message Feed",         category: "Health", perms: ["admin"], source: "health", tileKey: "message_feed",    wide: true,  nav: "health-message-feed",  needsNames: true,  maxRows: 4 },
];

// Keyed lookup
export const WIDGET_MAP = Object.fromEntries(WIDGETS.map(w => [w.id, w]));

// Full list
export const ALL_WIDGETS = WIDGETS;

// Default layout for first-time users
export const DEFAULT_HOME = [
  "home-messages", "home-nsfw", "home-presence", "home-xp",
  "home-joins", "home-moderation", "home-voice", "home-channels",
  "home-users", "home-returned", "home-starters", "home-butterflies",
  "home-loyalists", "home-mod-actions",
];

// Admin default prepends composite health score
export const DEFAULT_ADMIN = ["health-composite", ...DEFAULT_HOME];

// Dynamic import loaders keyed by widget id
const TILE_LOADERS = {
  "home-messages":          () => import("./tiles/home-messages.js"),
  "home-nsfw":              () => import("./tiles/home-nsfw.js"),
  "home-presence":          () => import("./tiles/home-presence.js"),
  "home-xp":                () => import("./tiles/home-xp.js"),
  "home-joins":             () => import("./tiles/home-joins.js"),
  "home-moderation":        () => import("./tiles/home-moderation.js"),
  "home-voice":             () => import("./tiles/home-voice.js"),
  "home-channels":          () => import("./tiles/home-channels.js"),
  "home-users":             () => import("./tiles/home-users.js"),
  "home-returned":          () => import("./tiles/home-returned.js"),
  "home-starters":          () => import("./tiles/home-starters.js"),
  "home-butterflies":       () => import("./tiles/home-butterflies.js"),
  "home-loyalists":         () => import("./tiles/home-loyalists.js"),
  "home-mod-actions":       () => import("./tiles/home-mod-actions.js"),
  "health-composite":       () => import("./tiles/composite-score.js"),
  "health-dau-mau":         () => import("./tiles/dau-mau.js"),
  "health-heatmap":         () => import("./tiles/heatmap.js"),
  "health-gini":            () => import("./tiles/gini.js"),
  "health-channel-health":  () => import("./tiles/channel-health.js"),
  "health-social-graph":    () => import("./tiles/social-graph.js"),
  "health-sentiment":       () => import("./tiles/sentiment.js"),
  "health-newcomer-funnel": () => import("./tiles/newcomer-funnel.js"),
  "health-cohort-retention":() => import("./tiles/cohort-retention.js"),
  "health-churn-risk":      () => import("./tiles/churn-risk.js"),
  "health-mod-workload":    () => import("./tiles/mod-workload.js"),
  "health-incidents":       () => import("./tiles/incidents.js"),
  "health-sentiment-feed": () => import("./tiles/sentiment-feed.js"),
  "health-message-feed":  () => import("./tiles/message-feed.js"),
};

// Cache resolved modules so we import each tile at most once
const _cache = {};

export async function loadRenderer(widgetId) {
  if (_cache[widgetId]) return _cache[widgetId];
  const loader = TILE_LOADERS[widgetId];
  if (!loader) return null;
  const mod = await loader();
  _cache[widgetId] = mod.renderTile;
  return mod.renderTile;
}
