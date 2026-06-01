// Help panel — renders sections of /static/manual.html natively in the
// dashboard's dark theme. Sidebar entries map to manual section anchors.

const SECTIONS = [
  { page: "help-overview",    anchor: "functional-blocks",  title: "Overview" },
  { page: "help-moderation",  anchor: "moderation",         title: "Moderation Core" },
  { page: "help-tickets",     anchor: "tickets",            title: "Tickets, Policies & Warnings" },
  { page: "help-analytics",   anchor: "analytics",          title: "Analytics & Watch List" },
  { page: "help-ai",          anchor: "ai-tools",           title: "AI Moderation Tools" },
  { page: "help-community",   anchor: "community",          title: "Community & XP" },
  { page: "help-voice",       anchor: "voice",              title: "Voice Channels" },
  { page: "help-music",       anchor: "music",              title: "Music & TTS" },
  { page: "help-confessions", anchor: "confessions",        title: "Confessions" },
  { page: "help-dms",         anchor: "dm-perms",           title: "DM Permissions" },
  { page: "help-wellness",    anchor: "wellness",           title: "Wellness System" },
  { page: "help-self",        anchor: "self-service",       title: "Member Self-Service" },
  { page: "help-network",     anchor: "network-analytics",  title: "Network Analytics" },
  { page: "help-cleanup",     anchor: "server-ops",         title: "Cleanup & Tagging" },
  { page: "help-guess",       anchor: "guess",              title: "Guess Who Image Game" },
  { page: "help-whisper",     anchor: "whisper",            title: "Whisper" },
  { page: "help-games",       anchor: "games",              title: "Games Night" },
  { page: "help-config",      anchor: "config",             title: "Configuration Reference" },
  { page: "help-setup",       anchor: "setup",              title: "Setup & Permissions" },
  { page: "help-owner",       anchor: "owner-tools",        title: "Developer / Owner Tools" },
  { page: "help-quickref",    anchor: "quickref",           title: "Quick Reference" },
];

let _manualPromise = null;
function loadManual() {
  if (_manualPromise) return _manualPromise;
  _manualPromise = fetch("/static/manual.html", { credentials: "same-origin" })
    .then((r) => r.text())
    .then((html) => new DOMParser().parseFromString(html, "text/html"))
    .catch((err) => {
      _manualPromise = null;
      throw err;
    });
  return _manualPromise;
}

function injectStylesheet() {
  if (document.getElementById("dk-help-panel-css")) return;
  const link = document.createElement("link");
  link.id = "dk-help-panel-css";
  link.rel = "stylesheet";
  link.href = "/static/help-panel.css";
  document.head.appendChild(link);
}

function currentSection() {
  const raw = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  return SECTIONS.find((s) => s.page === raw) || SECTIONS[0];
}

function extractSectionContent(doc, anchorId) {
  const start = doc.getElementById(anchorId);
  if (!start) return null;
  let heading;
  if (start.tagName === "H2" || start.tagName === "H3") {
    heading = start;
  } else {
    heading = start.closest("h2");
  }
  if (!heading) return null;
  const isH3 = heading.tagName === "H3";
  const frag = document.createDocumentFragment();
  let node = heading;
  while (node) {
    const next = node.nextElementSibling;
    frag.appendChild(node.cloneNode(true));
    if (!next) break;
    if (next.tagName === "H2" || (isH3 && next.tagName === "H3")) break;
    node = next;
  }
  return frag;
}

function rewriteInternalLinks(root) {
  // Manual uses href="#section-id"; rewrite section-level links to dashboard
  // hash routes. Intra-section anchors are left alone (browser scrolls within
  // the rendered fragment if the target id exists; otherwise it's a no-op).
  const byAnchor = Object.fromEntries(SECTIONS.map((s) => [s.anchor, s.page]));
  for (const a of root.querySelectorAll('a[href^="#"]')) {
    const target = a.getAttribute("href").slice(1);
    const page = byAnchor[target];
    if (page) a.setAttribute("href", `#/${page}`);
  }
}

export async function mount(container) {
  injectStylesheet();
  const meta = currentSection();

  container.replaceChildren();
  const panel = document.createElement("div");
  panel.className = "panel";

  const header = document.createElement("header");
  header.style.cssText = "display:flex;align-items:flex-start;justify-content:space-between;gap:16px;";

  const titleBox = document.createElement("div");
  const h2 = document.createElement("h2");
  h2.textContent = meta.title;
  const subtitle = document.createElement("div");
  subtitle.className = "subtitle";
  subtitle.textContent = "DungeonKeeper Reference Guide";
  titleBox.appendChild(h2);
  titleBox.appendChild(subtitle);

  const openLink = document.createElement("a");
  openLink.href = `/static/manual.html#${meta.anchor}`;
  openLink.target = "_blank";
  openLink.rel = "noopener";
  openLink.textContent = "Standalone ↗";
  openLink.style.cssText = "color:var(--ink-dim);font-size:12px;white-space:nowrap;text-decoration:none;border:1px solid var(--rule);padding:4px 10px;border-radius:var(--r-sm);";
  openLink.title = "Open the printable standalone reference in a new tab";

  header.appendChild(titleBox);
  header.appendChild(openLink);
  panel.appendChild(header);

  const body = document.createElement("div");
  body.className = "dk-help";
  panel.appendChild(body);

  const loading = document.createElement("div");
  loading.className = "panel-loading";
  loading.textContent = "Loading…";
  body.appendChild(loading);

  container.appendChild(panel);

  try {
    const doc = await loadManual();
    const fragment = extractSectionContent(doc, meta.anchor);
    body.replaceChildren();
    if (fragment) {
      body.appendChild(fragment);
      rewriteInternalLinks(body);
    } else {
      const err = document.createElement("p");
      err.textContent = `Section "${meta.anchor}" not found in manual.`;
      body.appendChild(err);
    }
  } catch (err) {
    body.replaceChildren();
    const e = document.createElement("p");
    e.textContent = `Failed to load reference: ${err.message}`;
    body.appendChild(e);
  }
}
