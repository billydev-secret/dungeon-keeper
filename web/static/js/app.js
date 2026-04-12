// Dashboard boot + hash-based panel router.
import { api } from "./api.js";

// ── Section definitions ─────────────────────────────────────────────

const SECTIONS = [
  {
    id: "home", label: "Home", perms: [],
    items: [
      { id: "home", label: "Home", module: "./panels/home.js" },
    ],
  },
  {
    id: "reports", label: "Reports", perms: ["admin"],
    groups: [
      { heading: "Health", items: [
        { id: "health-dashboard",       label: "Dashboard",          module: "./panels/health-dashboard.js" },
        { id: "health-dau-mau",         label: "DAU/MAU",            module: "./panels/health-dau-mau.js" },
        { id: "health-heatmap",         label: "Activity Heatmap",   module: "./panels/health-heatmap.js" },
        { id: "health-channel-health",  label: "Channel Health",     module: "./panels/health-channel-health.js" },
        { id: "health-gini",            label: "Participation Gini", module: "./panels/health-gini.js" },
        { id: "health-social-graph",    label: "Social Graph",       module: "./panels/health-social-graph.js" },
        { id: "health-sentiment",       label: "Sentiment & Tone",   module: "./panels/health-sentiment.js" },
        { id: "health-newcomer-funnel", label: "Newcomer Funnel",    module: "./panels/health-newcomer-funnel.js" },
        { id: "health-cohort-retention",label: "Cohort Retention",   module: "./panels/health-cohort-retention.js" },
        { id: "health-churn-risk",      label: "Churn Risk",         module: "./panels/health-churn-risk.js" },
        { id: "health-mod-workload",    label: "Mod Workload",       module: "./panels/health-mod-workload.js" },
        { id: "health-incidents",       label: "Incidents",          module: "./panels/health-incidents.js" },
        { id: "health-composite-score", label: "Health Score",       module: "./panels/health-composite-score.js" },
      ]},
      { heading: "General", items: [
        { id: "activity",             label: "Activity",             module: "./panels/activity.js" },
        { id: "role-growth",          label: "Role Growth",          module: "./panels/role-growth.js" },
        { id: "channel-comparison",   label: "Channels",              module: "./panels/channel-comparison.js" },
      ]},
      { heading: "Messages", items: [
        { id: "message-cadence",      label: "Message Cadence",      module: "./panels/message-cadence.js" },
        { id: "message-rate",         label: "Message Rate",         module: "./panels/message-rate.js" },
        { id: "message-rate-drops",   label: "Rate Drops",            module: "./panels/message-rate-drops.js" },
        { id: "burst-ranking",        label: "Burst Ranking",         module: "./panels/burst-ranking.js" },
      ]},
      { heading: "People", items: [
        { id: "retention",            label: "Retention",             module: "./panels/retention.js" },
        { id: "interaction-graph",    label: "Interactions",          module: "./panels/interaction-graph.js" },
        { id: "connection-graph",     label: "Connection Graph",      module: "./panels/connection-graph.js" },
        { id: "voice-activity",       label: "Voice Activity",        module: "./panels/voice-activity.js" },
        { id: "xp-leaderboard",       label: "XP Leaderboard",       module: "./panels/xp-leaderboard.js" },
        { id: "reaction-analytics",   label: "Reactions",             module: "./panels/reaction-analytics.js" },
        { id: "nsfw-gender",          label: "NSFW by Gender",       module: "./panels/nsfw-gender.js" },
        { id: "quality-score",        label: "Quality Score",        module: "./panels/quality-score.js" },
      ]},
      { heading: "Greeter", items: [
        { id: "greeter-response",     label: "Greeter Response",     module: "./panels/greeter-response.js" },
        { id: "invite-effectiveness", label: "Invite Effectiveness", module: "./panels/invite-effectiveness.js" },
        { id: "join-times",           label: "Join Times",           module: "./panels/join-times.js" },
      ]},
    ],
  },
  {
    id: "moderation", label: "Moderation", perms: ["moderator"],
    items: [
      { id: "mod-jails",      label: "Jails",          module: "./panels/mod-jails.js" },
      { id: "mod-tickets",    label: "Tickets",        module: "./panels/mod-tickets.js" },
      { id: "mod-warnings",   label: "Warnings",       module: "./panels/mod-warnings.js" },
      { id: "mod-policy-tickets", label: "Policy Tickets", module: "./panels/mod-policy-tickets.js" },
      { id: "mod-audit",      label: "Audit Log",      module: "./panels/mod-audit.js" },
      { id: "message-search", label: "Message Review",  module: "./panels/message-search.js" },
    ],
  },
  {
    id: "config", label: "Config", perms: ["admin"],
    items: [
      { id: "config-global",     label: "Global",          module: "./panels/config-global.js" },
      { id: "config-welcome",    label: "Welcome & Leave",  module: "./panels/config-welcome.js" },
      { id: "config-roles",      label: "Role Grants",      module: "./panels/config-roles.js" },
      { id: "config-xp",         label: "XP Logging",       module: "./panels/config-xp.js" },
      { id: "config-moderation", label: "Moderation",        module: "./panels/config-moderation.js" },
      { id: "config-prune",      label: "Inactivity Prune", module: "./panels/config-prune.js" },
      { id: "config-spoiler",    label: "Spoiler Guard",     module: "./panels/config-spoiler.js" },
      { id: "config-ai",         label: "AI Commands",       module: "./panels/config-ai.js" },
      { id: "config-wellness",   label: "Wellness",          module: "./panels/wellness-admin.js" },
      { id: "live-log",          label: "Live Log",          module: "./panels/live-log.js" },
      { id: "system-stats",      label: "System Stats",      module: "./panels/system-stats.js" },
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
];

// Flatten all page items for lookup
function allPages(section) {
  if (section.groups) return section.groups.flatMap((g) => g.items);
  return section.items || [];
}

let userPerms = new Set();
let userRoleIds = new Set();
let userRoleNames = [];
let visibleSections = SECTIONS;
let ALL_PAGES = SECTIONS.flatMap(allPages);
let PAGE_TO_SECTION = {};

function rebuildIndex() {
  visibleSections = SECTIONS.filter((sec) => {
    const permOk = !sec.perms || sec.perms.length === 0 || sec.perms.every((p) => userPerms.has(p));
    if (!permOk) return false;
    if (sec.roles && sec.roles.length > 0) {
      if (userPerms.has("manage_server") || userPerms.has("admin")) return true;
      return sec.roles.some((r) => userRoleNames.includes(r));
    }
    return true;
  });
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

const topbarTabsEl = document.getElementById("topbar-tabs");
const sidebarEl = document.getElementById("sidebar");
const sidebarItemsEl = document.getElementById("sidebar-items");
const rootEl = document.getElementById("panel-root");
const meEl = document.getElementById("me");
const sidebarToggleEl = document.getElementById("sidebar-toggle");
const sidebarBackdropEl = document.getElementById("sidebar-backdrop");

let currentPanel = null;

// ── Mobile sidebar toggle ──────────────────────────────────────────

function closeSidebar() {
  sidebarEl.classList.remove("open");
  sidebarBackdropEl.classList.remove("open");
}

sidebarToggleEl.addEventListener("click", () => {
  const opening = !sidebarEl.classList.contains("open");
  sidebarEl.classList.toggle("open", opening);
  sidebarBackdropEl.classList.toggle("open", opening);
});
sidebarBackdropEl.addEventListener("click", closeSidebar);

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

function renderNav(activeId) {
  const activeSection = PAGE_TO_SECTION[activeId] || SECTIONS[0];

  // Top bar tabs
  topbarTabsEl.innerHTML = "";
  for (const sec of visibleSections) {
    const btn = document.createElement("button");
    btn.textContent = sec.label;
    if (sec.id === activeSection.id) btn.classList.add("active");
    btn.addEventListener("click", () => {
      const firstPage = allPages(sec)[0];
      if (firstPage) window.location.hash = `#/${firstPage.id}`;
    });
    topbarTabsEl.appendChild(btn);
  }

  // Sidebar items for active section
  const pages = allPages(activeSection);
  if (pages.length <= 1) {
    sidebarEl.classList.add("hidden");
    sidebarToggleEl.classList.add("hidden");
    return;
  }
  sidebarEl.classList.remove("hidden");
  sidebarToggleEl.classList.remove("hidden");
  sidebarItemsEl.innerHTML = "";

  if (activeSection.groups) {
    // Grouped sidebar (Reports)
    for (const group of activeSection.groups) {
      const subLabel = document.createElement("button");
      subLabel.className = "nav-sub-label";
      subLabel.textContent = group.heading;
      subLabel.addEventListener("click", () => {
        subLabel.classList.toggle("collapsed");
      });
      sidebarItemsEl.appendChild(subLabel);

      const subBody = document.createElement("div");
      subBody.className = "nav-sub-body";
      for (const item of group.items) {
        const btn = document.createElement("button");
        btn.className = "sidebar-item";
        btn.textContent = item.label;
        if (item.id === activeId) btn.classList.add("active");
        btn.addEventListener("click", () => {
          window.location.hash = `#/${item.id}`;
        });
        subBody.appendChild(btn);
      }
      sidebarItemsEl.appendChild(subBody);
    }
  } else {
    // Flat sidebar
    for (const item of activeSection.items) {
      const btn = document.createElement("button");
      btn.className = "sidebar-item";
      btn.textContent = item.label;
      if (item.id === activeId) btn.classList.add("active");
      btn.addEventListener("click", () => {
        window.location.hash = `#/${item.id}`;
      });
      sidebarItemsEl.appendChild(btn);
    }
  }
}

// ── Mount panel ─────────────────────────────────────────────────────

async function mountPanel() {
  closeSidebar();
  const { id, params } = parseHash();
  const page = ALL_PAGES.find((p) => p.id === id) || ALL_PAGES[0];
  renderNav(page.id);

  if (currentPanel && currentPanel.unmount) {
    try { currentPanel.unmount(); } catch (_) {}
  }
  rootEl.innerHTML = `<div class="panel"><div class="empty">Loading ${page.label}…</div></div>`;

  try {
    const mod = await import(page.module);
    currentPanel = mod.mount(rootEl, params) || null;
  } catch (err) {
    rootEl.innerHTML = `<div class="panel"><div class="error">Failed to load ${page.label}: ${err.message}</div></div>`;
  }
}

// ── Boot ────────────────────────────────────────────────────────────

async function boot() {
  try {
    const me = await api("/api/me");
    if (!me) return; // redirecting to login

    userPerms = new Set(me.perms);
    userRoleIds = new Set(me.role_ids || []);
    userRoleNames = me.role_names || [];

    // Expose user info globally so panel modules can access it.
    window.__dk_user = {
      user_id: me.user_id,
      username: me.username,
      perms: userPerms,
      role_ids: userRoleIds,
      role_names: userRoleNames,
    };

    rebuildIndex();

    meEl.textContent = me.username;
    if (me.user_id !== "0") {
      const sep = document.createTextNode(" \u00b7 ");
      const link = document.createElement("a");
      link.href = "/logout";
      link.textContent = "Logout";
      link.style.cssText = "color:var(--text-dim);font-size:12px;text-decoration:none;";
      link.addEventListener("mouseenter", () => { link.style.color = "var(--text)"; });
      link.addEventListener("mouseleave", () => { link.style.color = "var(--text-dim)"; });
      meEl.appendChild(sep);
      meEl.appendChild(link);
    }
  } catch (err) {
    meEl.textContent = `auth error: ${err.message}`;
  }
  window.addEventListener("hashchange", mountPanel);
  mountPanel();
}

boot();
