// Dashboard boot + hash-based panel router.
import { api } from "./api.js";

// ── Section definitions ─────────────────────────────────────────────

const SECTIONS = [
  {
    id: "home", label: "Home",
    items: [
      { id: "home", label: "Home", module: "./panels/home.js" },
    ],
  },
  {
    id: "reports", label: "Reports",
    groups: [
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
    id: "moderation", label: "Moderation",
    items: [
      { id: "mod-jails",    label: "Jails",     module: "./panels/mod-jails.js" },
      { id: "mod-tickets",  label: "Tickets",   module: "./panels/mod-tickets.js" },
      { id: "mod-warnings", label: "Warnings",  module: "./panels/mod-warnings.js" },
      { id: "mod-audit",    label: "Audit Log",  module: "./panels/mod-audit.js" },
    ],
  },
  {
    id: "messages", label: "Message Review",
    items: [
      { id: "message-search", label: "Search", module: "./panels/message-search.js" },
    ],
  },
  {
    id: "config", label: "Config",
    items: [
      { id: "config-global",     label: "Global",          module: "./panels/config-global.js" },
      { id: "config-welcome",    label: "Welcome & Leave",  module: "./panels/config-welcome.js" },
      { id: "config-roles",      label: "Role Grants",      module: "./panels/config-roles.js" },
      { id: "config-xp",         label: "XP Logging",       module: "./panels/config-xp.js" },
      { id: "config-moderation", label: "Moderation",        module: "./panels/config-moderation.js" },
      { id: "config-prune",      label: "Inactivity Prune", module: "./panels/config-prune.js" },
      { id: "config-spoiler",    label: "Spoiler Guard",     module: "./panels/config-spoiler.js" },
    ],
  },
];

// Flatten all page items for lookup
function allPages(section) {
  if (section.groups) return section.groups.flatMap((g) => g.items);
  return section.items || [];
}
const ALL_PAGES = SECTIONS.flatMap(allPages);

// Map page id -> section
const PAGE_TO_SECTION = {};
for (const sec of SECTIONS) {
  for (const page of allPages(sec)) {
    PAGE_TO_SECTION[page.id] = sec;
  }
}

// ── DOM refs ────────────────────────────────────────────────────────

const topbarTabsEl = document.getElementById("topbar-tabs");
const sidebarEl = document.getElementById("sidebar");
const sidebarItemsEl = document.getElementById("sidebar-items");
const rootEl = document.getElementById("panel-root");
const meEl = document.getElementById("me");

let currentPanel = null;

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
  for (const sec of SECTIONS) {
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
    return;
  }
  sidebarEl.classList.remove("hidden");
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
    meEl.textContent = `${me.username} · ${me.guild_name || me.guild_id}`;
  } catch (err) {
    meEl.textContent = `auth error: ${err.message}`;
  }
  window.addEventListener("hashchange", mountPanel);
  mountPanel();
}

boot();
