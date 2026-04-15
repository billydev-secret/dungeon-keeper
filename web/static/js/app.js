// Dashboard boot + hash-based panel router.
import { api, esc } from "./api.js";

const _moduleVer = "?v=15";

// ── Section definitions ─────────────────────────────────────────────

const SECTIONS = [
  {
    id: "home", label: "Dashboard", perms: [],
    items: [
      { id: "home", label: "Home", module: "./panels/home.js" },
    ],
  },
  {
    id: "reports", label: "Reports", perms: ["admin"],
    groups: [
      { heading: "Moderation", items: [
        { id: "health-incidents",       label: "Incidents",          module: "./panels/health-incidents.js" },
        { id: "health-sentiment",       label: "Sentiment & Tone",  module: "./panels/health-sentiment.js" },
        { id: "health-sentiment-feed",  label: "Sentiment Feed",    module: "./panels/health-sentiment-feed.js" },
        { id: "health-message-feed",   label: "Message Feed",       module: "./panels/health-message-feed.js" },
        { id: "health-mod-workload",    label: "Mod Workload",       module: "./panels/health-mod-workload.js" },
      ]},
      { heading: "General", items: [
        { id: "health-heatmap",         label: "Activity Heatmap",   module: "./panels/health-heatmap.js" },
        { id: "health-channel-health",  label: "Channel Health",     module: "./panels/health-channel-health.js" },
        { id: "health-composite-score", label: "Health Score",       module: "./panels/health-composite-score.js" },
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
        { id: "health-dau-mau",         label: "DAU/MAU",            module: "./panels/health-dau-mau.js" },
        { id: "health-gini",            label: "Participation Gini", module: "./panels/health-gini.js" },
        { id: "health-churn-risk",      label: "Churn Risk",         module: "./panels/health-churn-risk.js" },
        { id: "retention",            label: "Retention",             module: "./panels/retention.js" },
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
      { id: "config-roles",         label: "Role Grants",      module: "./panels/config-roles.js" },
      { id: "config-booster-roles", label: "Booster Roles",   module: "./panels/config-booster-roles.js" },
      { id: "config-xp",            label: "XP Logging",      module: "./panels/config-xp.js" },
      { id: "config-moderation", label: "Moderation",        module: "./panels/config-moderation.js" },
      { id: "config-prune",      label: "Inactivity Prune", module: "./panels/config-prune.js" },
      { id: "config-spoiler",      label: "Spoiler Guard",     module: "./panels/config-spoiler.js" },
      { id: "config-auto-delete", label: "Auto-Delete",      module: "./panels/config-auto-delete.js" },
      { id: "config-ai",          label: "AI Commands",      module: "./panels/config-ai.js" },
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
let primaryGuildId = null;
let visibleSections = SECTIONS;
let ALL_PAGES = SECTIONS.flatMap(allPages);
let PAGE_TO_SECTION = {};

function rebuildIndex() {
  const isNonPrimaryGuild = primaryGuildId && window.__dk_user &&
    window.__dk_user.guild_id !== primaryGuildId;

  visibleSections = SECTIONS.filter((sec) => {
    // Hide Config for non-primary guilds (config tables are not guild-scoped)
    if (sec.id === "config" && isNonPrimaryGuild) return false;

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
    const items = sidebarItemsEl.querySelectorAll(".nav-item");
    items.forEach((it) => {
      const txt = it.querySelector(".lbl")?.textContent.toLowerCase() || "";
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
    window.location.hash = `#/${item.id}`;
  });
  return btn;
}

function renderNav(activeId) {
  sidebarItemsEl.innerHTML = "";

  const activeSection = PAGE_TO_SECTION[activeId];

  for (const sec of visibleSections) {
    const group = document.createElement("div");
    group.className = "nav-group";
    group.textContent = sec.label;
    // Collapse by default, except the group containing the active page
    const startCollapsed = !activeSection || sec.id !== activeSection.id;
    if (startCollapsed) group.classList.add("collapsed");
    group.addEventListener("click", () => {
      group.classList.toggle("collapsed");
      const hidden = group.classList.contains("collapsed");
      let n = group.nextElementSibling;
      while (n && !n.matches(".nav-group")) {
        n.classList.toggle("group-hidden", hidden);
        n = n.nextElementSibling;
      }
    });
    sidebarItemsEl.appendChild(group);

    const children = [];
    if (sec.groups) {
      for (const g of sec.groups) {
        const subLabel = document.createElement("div");
        subLabel.className = "nav-subgroup";
        subLabel.textContent = g.heading;
        sidebarItemsEl.appendChild(subLabel);
        children.push(subLabel);
        for (const item of g.items) {
          const el = makeNavItem(item, activeId, { isSubitem: true });
          sidebarItemsEl.appendChild(el);
          children.push(el);
        }
      }
    } else {
      for (const item of sec.items || []) {
        const el = makeNavItem(item, activeId);
        sidebarItemsEl.appendChild(el);
        children.push(el);
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
    const mod = await import(page.module + _moduleVer);
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
  };

  // Hide Config section when viewing a non-primary guild
  rebuildIndex();
}

function populateGuildPicker(guilds, activeId) {
  const nameEl = guildSelectEl.querySelector(".guild-picker__name");
  const sigilEl = guildSelectEl.querySelector("[data-guild-sigil]");
  const menuEl = guildSelectEl.querySelector(".guild-picker__menu");
  menuEl.innerHTML = "";
  const active = guilds.find((g) => g.id === activeId) || guilds[0];
  if (active) {
    nameEl.textContent = active.name;
    if (sigilEl) {
      if (active.icon) {
        sigilEl.innerHTML = `<img class="guild-sigil-img" src="${escText(active.icon)}" alt="">`;
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
    li.addEventListener("click", () => {
      guildSelectEl.classList.remove("open");
      if (g.id !== activeId) switchGuild(g.id);
    });
    menuEl.appendChild(li);
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
  const statusLabel = isGuest ? "guest" : `${(me.perms || []).includes("admin") ? "Keeper" : "Member"} · ${status}`;
  const avatarInner = (!isGuest && me.avatar_url)
    ? `<img class="user-avatar-img" src="${escText(me.avatar_url)}" alt="">`
    : escText(initial);
  meEl.innerHTML = `
    <div class="user-avatar status-${escText(status)}">${avatarInner}</div>
    <div class="user-meta">
      <b>${escText(me.username || "")}</b>
      <small>${escText(statusLabel)}</small>
    </div>
    ${!isGuest ? `<a class="logout-link" href="/logout">Logout</a>` : ""}
  `;
}

function escText(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
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
      guildSelectEl.querySelector(".guild-picker__toggle").addEventListener("click", (e) => {
        // Only open the dropdown if there's more than one guild
        if (me.guilds.length <= 1) return;
        e.stopPropagation();
        guildSelectEl.classList.toggle("open");
      });
      document.addEventListener("click", (e) => {
        if (!guildSelectEl.contains(e.target)) guildSelectEl.classList.remove("open");
      });
    }

    renderUserBar(me);
  } catch (err) {
    meEl.innerHTML = `<div class="user-meta"><small style="color:var(--red)">auth error: ${escText(err.message)}</small></div>`;
  }
  window.addEventListener("hashchange", mountPanel);
  mountPanel();
}

boot();
