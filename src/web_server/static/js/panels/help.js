// Help panel — renders sections of /static/manual.html natively in the
// dashboard's dark theme, plus full-text search across the whole manual.
// The page→anchor mapping lives in help-sections.js (shared with app.js's
// nav so the sidebar can't drift from the manual).

import { HELP_PAGES } from "./help-sections.js?v=24";
import { apiPost, esc } from "../api.js";

// Render the advisor's plaintext answer with a safe markdown-lite pass. `esc`
// runs first, so the tag substitutions below can only ever produce our own tags.
function renderAnswerHtml(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+?)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}

// The "Ask the Guide" box: a grounded chat over the manual (POST /api/help/advisor).
function buildAskBox() {
  const box = document.createElement("div");
  box.className = "dk-help-ask";
  box.style.cssText =
    "margin:12px 0 4px;padding:12px;border:1px solid var(--rule);border-radius:var(--r-sm);background:var(--surface,transparent);";

  const form = document.createElement("form");
  form.style.cssText = "display:flex;gap:8px;align-items:center;";

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "Ask the guide — e.g. “How do I start a game?”";
  input.setAttribute("aria-label", "Ask the guide a question");
  input.maxLength = 500;
  input.style.cssText = "flex:1;min-width:0;";

  const btn = document.createElement("button");
  btn.type = "submit";
  btn.className = "btn";
  btn.textContent = "Ask";

  form.append(input, btn);

  const answer = document.createElement("div");
  answer.className = "dk-help-answer";
  answer.hidden = true;
  answer.style.cssText =
    "margin-top:10px;padding:10px 12px;border-radius:var(--r-sm);background:var(--code-bg,rgba(127,127,127,0.08));line-height:1.55;white-space:normal;";

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    btn.disabled = true;
    input.disabled = true;
    answer.hidden = false;
    answer.textContent = "Thinking…";
    try {
      const res = await apiPost("/api/help/advisor", { question: q });
      answer.innerHTML = renderAnswerHtml(res.answer || "");
    } catch (err) {
      answer.textContent =
        err && /429/.test(String(err.message))
          ? "You're asking a lot quickly — give it a few seconds and try again."
          : "Couldn't reach the guide just now — try again in a moment.";
    } finally {
      btn.disabled = false;
      input.disabled = false;
      input.focus();
    }
  });

  box.append(form, answer);
  return box;
}

let _manualPromise = null;
function loadManual() {
  if (_manualPromise) return _manualPromise;
  // cache: "no-cache" forces revalidation (ETag/304) so a browser-cached
  // manual can't go stale against the versioned JS that parses it.
  _manualPromise = fetch("/static/manual.html", { credentials: "same-origin", cache: "no-cache" })
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
  link.href = "/static/help-panel.css?v=24";
  document.head.appendChild(link);
}

function currentSection() {
  const raw = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  return HELP_PAGES.find((s) => s.page === raw) || HELP_PAGES[0];
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
  // Manual uses href="#section-id"; rewrite links whose target is a known
  // help page (h2 or h3 anchor) to dashboard hash routes. Other intra-section
  // anchors are left alone (browser scrolls within the rendered fragment if
  // the target id exists; otherwise it's a no-op).
  const byAnchor = Object.fromEntries(HELP_PAGES.map((s) => [s.anchor, s.page]));
  for (const a of root.querySelectorAll('a[href^="#"]')) {
    const target = a.getAttribute("href").slice(1);
    const page = byAnchor[target];
    if (page) a.setAttribute("href", `#/${page}`);
  }
}

// ── Full-text search across the manual ─────────────────────────────

let _searchIndex = null;

// One entry per h2/h3 block: { title, path, page, anchor, text }
function buildSearchIndex(doc) {
  if (_searchIndex) return _searchIndex;
  const pageByAnchor = Object.fromEntries(HELP_PAGES.map((s) => [s.anchor, s.page]));
  const entries = [];
  let currentH2 = null;
  let currentH2Page = null;

  const headingTitle = (h) => {
    const clone = h.cloneNode(true);
    clone.querySelectorAll(".section-num").forEach((n) => n.remove());
    return clone.textContent.trim();
  };

  for (const h of doc.querySelectorAll("h2[id], h3[id]")) {
    if (h.tagName === "H2") {
      currentH2 = headingTitle(h);
      currentH2Page = pageByAnchor[h.id] || null;
      if (!currentH2Page) continue; // cover/unmapped section
      entries.push({ title: currentH2, path: currentH2, page: currentH2Page, anchor: h.id, text: "" });
    } else {
      if (!currentH2Page) continue;
      const title = headingTitle(h);
      entries.push({
        title,
        path: `${currentH2} › ${title}`,
        // Prefer a dedicated subsection page when one exists (deep link),
        // otherwise route to the parent section and scroll to the anchor.
        page: pageByAnchor[h.id] || currentH2Page,
        anchor: h.id,
        text: "",
      });
    }
    // Collect the block's text (until the next h2/h3).
    const entry = entries[entries.length - 1];
    let node = h.nextElementSibling;
    const parts = [];
    while (node && node.tagName !== "H2" && node.tagName !== "H3") {
      parts.push(node.textContent || "");
      node = node.nextElementSibling;
    }
    entry.text = parts.join(" ").replace(/\s+/g, " ").trim();
  }
  _searchIndex = entries;
  return _searchIndex;
}

function searchManual(doc, query) {
  const q = query.toLowerCase();
  const results = [];
  for (const entry of buildSearchIndex(doc)) {
    const inTitle = entry.title.toLowerCase().includes(q);
    const idx = entry.text.toLowerCase().indexOf(q);
    if (!inTitle && idx === -1) continue;
    let snippet = "";
    if (idx !== -1) {
      const start = Math.max(0, idx - 60);
      const end = Math.min(entry.text.length, idx + q.length + 90);
      snippet = (start > 0 ? "…" : "") + entry.text.slice(start, end) + (end < entry.text.length ? "…" : "");
    } else {
      snippet = entry.text.slice(0, 150) + (entry.text.length > 150 ? "…" : "");
    }
    results.push({ ...entry, snippet, rank: inTitle ? 0 : 1 });
  }
  results.sort((a, b) => a.rank - b.rank);
  return results;
}

function renderSearchResults(body, results, query) {
  body.replaceChildren();
  const list = document.createElement("div");
  list.className = "dk-help-results";

  const count = document.createElement("p");
  count.className = "dk-help-results-count";
  count.textContent = results.length
    ? `${results.length} match${results.length === 1 ? "" : "es"} for “${query}”`
    : `No matches for “${query}” — try a command name (e.g. “jail”) or a feature word (e.g. “anonymous”).`;
  list.appendChild(count);

  const q = query.toLowerCase();
  for (const r of results.slice(0, 30)) {
    const a = document.createElement("a");
    a.className = "dk-help-result";
    a.href = `#/${r.page}?focus=${encodeURIComponent(r.anchor)}`;

    const title = document.createElement("div");
    title.className = "dk-help-result-title";
    title.textContent = r.path;
    a.appendChild(title);

    if (r.snippet) {
      const snip = document.createElement("div");
      snip.className = "dk-help-result-snippet";
      // Highlight the first match inside the snippet.
      const i = r.snippet.toLowerCase().indexOf(q);
      if (i === -1) {
        snip.textContent = r.snippet;
      } else {
        snip.append(
          document.createTextNode(r.snippet.slice(0, i)),
          Object.assign(document.createElement("mark"), { textContent: r.snippet.slice(i, i + q.length) }),
          document.createTextNode(r.snippet.slice(i + q.length)),
        );
      }
      a.appendChild(snip);
    }
    list.appendChild(a);
  }
  body.appendChild(list);
}

// ── Panel ───────────────────────────────────────────────────────────

export async function mount(container, params = {}) {
  injectStylesheet();
  const meta = currentSection();

  container.replaceChildren();
  const panel = document.createElement("div");
  panel.className = "panel";

  const header = document.createElement("header");
  header.style.cssText = "display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;";

  const titleBox = document.createElement("div");
  const h2 = document.createElement("h2");
  h2.textContent = meta.label;
  const subtitle = document.createElement("div");
  subtitle.className = "subtitle";
  subtitle.textContent = "DungeonKeeper Reference Guide";
  titleBox.appendChild(h2);
  titleBox.appendChild(subtitle);

  const tools = document.createElement("div");
  tools.style.cssText = "display:flex;align-items:center;gap:10px;";

  const search = document.createElement("input");
  search.type = "search";
  search.className = "dk-help-search";
  search.placeholder = "Search the whole guide…";
  search.setAttribute("aria-label", "Search the reference guide");

  const openLink = document.createElement("a");
  openLink.href = `/static/manual.html#${meta.anchor}`;
  openLink.target = "_blank";
  openLink.rel = "noopener";
  openLink.textContent = "Standalone ↗";
  openLink.style.cssText = "color:var(--ink-dim);font-size:12px;white-space:nowrap;text-decoration:none;border:1px solid var(--rule);padding:4px 10px;border-radius:var(--r-sm);";
  openLink.title = "Open the printable standalone reference in a new tab";

  tools.appendChild(search);
  tools.appendChild(openLink);
  header.appendChild(titleBox);
  header.appendChild(tools);
  panel.appendChild(header);

  panel.appendChild(buildAskBox());

  const body = document.createElement("div");
  body.className = "dk-help";
  panel.appendChild(body);

  const loading = document.createElement("div");
  loading.className = "panel-loading";
  loading.textContent = "Loading…";
  body.appendChild(loading);

  container.appendChild(panel);

  let sectionFragment = null;

  const showSection = () => {
    body.replaceChildren();
    if (sectionFragment) {
      body.appendChild(sectionFragment.cloneNode(true));
      rewriteInternalLinks(body);
    } else {
      const err = document.createElement("p");
      err.textContent = `Section "${meta.anchor}" not found in manual.`;
      body.appendChild(err);
    }
  };

  try {
    const doc = await loadManual();
    sectionFragment = extractSectionContent(doc, meta.anchor);
    showSection();

    // Deep link from a search result: scroll to the anchor and flash it.
    if (params.focus) {
      const target = body.querySelector(`#${CSS.escape(params.focus)}`);
      if (target) {
        target.scrollIntoView({ block: "start" });
        target.classList.add("dk-help-focus");
        setTimeout(() => target.classList.remove("dk-help-focus"), 2500);
      }
    }

    let debounce = null;
    search.addEventListener("input", () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        const q = search.value.trim();
        if (q.length < 2) {
          showSection();
          return;
        }
        renderSearchResults(body, searchManual(doc, q), q);
      }, 150);
    });
  } catch (err) {
    body.replaceChildren();
    const e = document.createElement("p");
    e.textContent = `Failed to load reference: ${err.message}`;
    body.appendChild(e);
  }
}
