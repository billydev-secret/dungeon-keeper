import { api, esc, fmtTs } from "../api.js";
import { toChannelOptions } from "../config-helpers.js";
import { filterSelect, multiFilterSelect } from "../filter-select.js";
import { memberSearch } from "../config-helpers.js";
import { renderEmpty, renderError, renderLoading } from "../states.js";

/** Format a sentiment score as a short label with emoji. */
function sentimentBadge(val) {
  if (val == null) return "";
  const n = Number(val);
  if (isNaN(n)) return "";
  let icon = "\u{1F610}"; // 😐
  if (n >= 0.5) icon = "\u{1F60A}"; // 😊
  else if (n >= 0.05) icon = "\u{1F642}"; // 🙂
  else if (n <= -0.5) icon = "\u{1F620}"; // 😠
  else if (n <= -0.05) icon = "\u{1F641}"; // 🙁
  return `<span class="msg-sentiment" title="Sentiment: ${n.toFixed(2)}">${icon} ${n.toFixed(2)}</span>`;
}

function emotionBadge(val) {
  if (!val) return "";
  return `<span class="msg-emotion">${esc(val)}</span>`;
}

/** How each deletion source reads to a moderator. */
const DELETED_LABELS = {
  auto_delete: { text: "auto-deleted", title: "Removed by an auto-delete rule" },
  discord: { text: "deleted", title: "Deleted on Discord" },
};

function deletedBadge(m) {
  if (m.deleted_at == null) return "";
  const kind = DELETED_LABELS[m.deleted_source] || DELETED_LABELS.discord;
  const when = fmtTs(m.deleted_at);
  return `<span class="badge badge-danger msg-deleted" title="${esc(kind.title)} — ${esc(when)}">${esc(kind.text)}</span>`;
}

/**
 * "Open in Discord" for a message that still exists there. The server decides:
 * it sends discord_url = null for anything flagged deleted, so a link that
 * would land on nothing is never rendered.
 */
function discordLink(m) {
  if (!m.discord_url) return "";
  return `<a class="msg-jump" href="${esc(m.discord_url)}" target="_blank" rel="noopener" title="Open in Discord">↗</a>`;
}

// ── The filter catalogue ──────────────────────────────────────────────
//
// Every filter beyond the regex box lives here and is added on demand, so an
// unused filter costs no screen space. An entry supplies either a `picker` (a
// shared filter-select reused across add/remove cycles) or a `render` that
// builds its own control, plus an `apply` that writes it into the outgoing
// query. A filter whose value is empty is simply not sent, so a half-filled
// chip narrows nothing rather than erroring.

const EMOTIONS = ["joy", "playful", "anger", "frustration", "neutral"];

const SORTS = [
  ["newest", "Newest first"],
  ["oldest", "Oldest first"],
  ["most_reacted", "Most reacted"],
  ["longest", "Longest first"],
  ["most_positive", "Most positive"],
  ["most_negative", "Most negative"],
];

const DELETED_OPTIONS = [
  ["any", "Any"],
  ["only", "Deleted only"],
  ["live", "Not deleted"],
  ["discord", "Deleted on Discord"],
  ["auto_delete", "Auto-deleted"],
];

function selectHtml(field, options, selected) {
  const opts = options
    .map(([v, label]) =>
      `<option value="${esc(v)}"${v === selected ? " selected" : ""}>${esc(label)}</option>`
    )
    .join("");
  return `<select data-field="${esc(field)}">${opts}</select>`;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel" style="overflow-y:auto;">
      <header>
        <h2>Message Search</h2>
        <div class="subtitle">Search and read back stored messages</div>
      </header>
      <div class="msg-searchbar">
        <input type="text" data-field="regex" class="msg-regex"
               placeholder="Regex pattern — leave blank to match everything"
               aria-label="Regex pattern" />
        <button data-add-filter class="btn" aria-haspopup="true" aria-expanded="false">+ Filter</button>
        <button data-search class="btn btn-primary">Search</button>
        <button data-download class="btn" style="display:none">Download JSON</button>
      </div>
      <div data-chips class="msg-chips"></div>
      <div data-results class="msg-results"></div>
      <div data-pager class="msg-pager"></div>
    </div>
  `;

  const regexInput = container.querySelector('[data-field="regex"]');
  const chipsEl = container.querySelector("[data-chips]");
  const resultsEl = container.querySelector("[data-results]");
  const pagerEl = container.querySelector("[data-pager]");
  const searchBtn = container.querySelector("[data-search]");
  const downloadBtn = container.querySelector("[data-download]");
  const addFilterBtn = container.querySelector("[data-add-filter]");

  // Member/channel pickers are built once and re-parented into their chip when
  // the filter is added, so the options loaded below survive add/remove cycles.
  //
  // The three member filters get `search`: /api/meta/members only prefetches a
  // bounded page, and searching an archive for a member who has since left is
  // one of the main things this panel is for.
  const authorFS = multiFilterSelect("Type to filter…", [],
    { label: "Author", search: memberSearch() });
  const channelFS = multiFilterSelect("Type to filter…", [], { label: "Channel" });
  const mentionsFS = filterSelect("Type to filter…", [],
    { label: "Mentions", emptyLabel: "(anyone)", search: memberSearch() });
  const replyFS = filterSelect("Type to filter…", [],
    { label: "Reply to", emptyLabel: "(anyone)", search: memberSearch() });

  for (const fs of [authorFS, channelFS, mentionsFS, replyFS]) {
    fs.getInput().addEventListener("keydown", (e) => {
      if (e.key === "Enter") doSearch(1);
    });
  }

  const FILTERS = {
    author: {
      label: "Author",
      picker: authorFS,
      apply: (p) => authorFS.getValues().forEach((id) => p.append("author", id)),
    },
    channel: {
      label: "Channel",
      picker: channelFS,
      apply: (p) => channelFS.getValues().forEach((id) => p.append("channel", id)),
    },
    mentions: {
      label: "Mentions",
      picker: mentionsFS,
      apply: (p) => { if (mentionsFS.getValue()) p.set("mentions", mentionsFS.getValue()); },
    },
    reply_to: {
      label: "Reply to",
      picker: replyFS,
      apply: (p) => { if (replyFS.getValue()) p.set("reply_to", replyFS.getValue()); },
    },
    deleted: {
      label: "Deleted",
      render: () => selectHtml("deleted", DELETED_OPTIONS, "only"),
      apply: (p, el) => p.set("deleted", val(el, "deleted")),
    },
    emotion: {
      label: "Emotion",
      render: () =>
        selectHtml("emotion", EMOTIONS.map((e) => [e, e[0].toUpperCase() + e.slice(1)]), "joy"),
      apply: (p, el) => p.set("emotion", val(el, "emotion")),
    },
    sentiment: {
      label: "Sentiment",
      render: () => `
        <input type="number" data-field="sentiment_min" min="-1" max="1" step="0.1" placeholder="min" />
        <span class="msg-chip-sep">to</span>
        <input type="number" data-field="sentiment_max" min="-1" max="1" step="0.1" placeholder="max" />`,
      apply: (p, el) => {
        if (val(el, "sentiment_min")) p.set("sentiment_min", val(el, "sentiment_min"));
        if (val(el, "sentiment_max")) p.set("sentiment_max", val(el, "sentiment_max"));
      },
    },
    length: {
      label: "Length",
      render: () => `
        <input type="number" data-field="min_length" min="0" placeholder="min" />
        <span class="msg-chip-sep">to</span>
        <input type="number" data-field="max_length" min="0" placeholder="max" />`,
      apply: (p, el) => {
        if (val(el, "min_length")) p.set("min_length", val(el, "min_length"));
        if (val(el, "max_length")) p.set("max_length", val(el, "max_length"));
      },
    },
    attachments: {
      label: "Attachments",
      render: () => selectHtml("has_attachments", [["true", "Has attachments"], ["false", "No attachments"]], "true"),
      apply: (p, el) => p.set("has_attachments", val(el, "has_attachments")),
    },
    reactions: {
      label: "Reactions",
      render: () => selectHtml("has_reactions", [["true", "Has reactions"], ["false", "No reactions"]], "true"),
      apply: (p, el) => p.set("has_reactions", val(el, "has_reactions")),
    },
    after: {
      label: "After",
      render: () => `<input type="datetime-local" data-field="after_dt" />`,
      apply: (p, el) => {
        const v = val(el, "after_dt");
        if (v) p.set("after", String(Math.floor(new Date(v).getTime() / 1000)));
      },
    },
    before: {
      label: "Before",
      render: () => `<input type="datetime-local" data-field="before_dt" />`,
      apply: (p, el) => {
        const v = val(el, "before_dt");
        if (v) p.set("before", String(Math.floor(new Date(v).getTime() / 1000)));
      },
    },
    show_bots: {
      label: "Show bots",
      render: () => `<span class="msg-chip-static">included</span>`,
      apply: (p) => p.set("include_bots", "true"),
    },
    sort: {
      label: "Sort",
      render: () => selectHtml("sort", SORTS, "newest"),
      apply: (p, el) => p.set("sort", val(el, "sort")),
    },
  };

  const val = (el, field) => {
    const node = el.querySelector(`[data-field="${field}"]`);
    return node ? node.value : "";
  };

  // ── Active chips ────────────────────────────────────────────────────

  /** key -> chip element, in insertion order. */
  const active = new Map();

  function addFilter(key) {
    if (active.has(key)) {
      // Already on screen — focus it rather than stacking a duplicate.
      const existing = active.get(key);
      const focusable = existing.querySelector("select, input");
      if (focusable) focusable.focus();
      return;
    }
    const spec = FILTERS[key];
    const chip = document.createElement("span");
    chip.className = "msg-chip";
    chip.innerHTML = `
      <span class="msg-chip-label">${esc(spec.label)}:</span>
      <span class="msg-chip-body"></span>
      <button class="msg-chip-x" data-remove aria-label="Remove ${esc(spec.label)} filter">✕</button>
    `;
    const body = chip.querySelector(".msg-chip-body");
    if (spec.picker) {
      body.appendChild(spec.picker.el);
    } else {
      body.innerHTML = spec.render();
    }
    chip.querySelector("[data-remove]").addEventListener("click", () => removeFilter(key));
    chipsEl.appendChild(chip);
    active.set(key, chip);

    const focusable = chip.querySelector("select, input");
    if (focusable) focusable.focus();
  }

  function removeFilter(key) {
    const chip = active.get(key);
    if (!chip) return;
    const spec = FILTERS[key];
    if (spec.picker) {
      // Clear the selection, then detach the shared picker before the chip is
      // destroyed — it gets re-used if the filter is added again.
      if (spec.picker.setValues) spec.picker.setValues([]);
      else if (spec.picker.setValue) spec.picker.setValue(null);
      spec.picker.el.remove();
    }
    chip.remove();
    active.delete(key);
  }

  // ── The "+ Filter" menu ─────────────────────────────────────────────

  let menuEl = null;

  function closeMenu() {
    if (menuEl) {
      menuEl.remove();
      menuEl = null;
      addFilterBtn.setAttribute("aria-expanded", "false");
    }
  }

  function openMenu() {
    closeMenu();
    menuEl = document.createElement("div");
    menuEl.className = "msg-filter-menu";
    menuEl.setAttribute("role", "menu");
    menuEl.innerHTML = Object.entries(FILTERS)
      .map(([key, spec]) =>
        `<button role="menuitem" data-key="${esc(key)}"${active.has(key) ? " disabled" : ""}>${esc(spec.label)}</button>`
      )
      .join("");
    menuEl.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-key]");
      if (!btn) return;
      addFilter(btn.dataset.key);
      closeMenu();
    });
    addFilterBtn.parentElement.insertBefore(menuEl, addFilterBtn.nextSibling);
    addFilterBtn.setAttribute("aria-expanded", "true");
  }

  addFilterBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menuEl) closeMenu();
    else openMenu();
  });

  const onDocClick = (e) => {
    if (menuEl && !menuEl.contains(e.target) && e.target !== addFilterBtn) closeMenu();
  };
  const onDocKey = (e) => {
    if (e.key === "Escape") closeMenu();
  };
  document.addEventListener("click", onDocClick);
  document.addEventListener("keydown", onDocKey);

  // ── Option loading ──────────────────────────────────────────────────

  (async () => {
    try {
      const [members, channels] = await Promise.all([
        api("/api/meta/members"),
        api("/api/meta/channels"),
      ]);

      // Deliberately not toSortedMemberOptions(): that spells the departure out
      // in full, and this panel is documented (test_frontend_wiring) as using
      // the terse suffix. It is not toMemberOptions() either, which bakes the
      // suffix in before any sort — departures have to sort to the bottom on
      // the bare label. Sorted-plus-terse is this panel's own combination.
      const memberOpts = members
        .map((m) => ({
          id: m.id,
          label: m.display_name !== m.name ? `${m.display_name} (${m.name})` : m.name,
          left: !!m.left_server,
        }))
        .sort((a, b) => a.left - b.left || a.label.localeCompare(b.label))
        .map((o) => (o.left ? { ...o, label: `${o.label} (left)` } : o));
      const channelOpts = toChannelOptions(channels);

      authorFS.setOptions(memberOpts);
      channelFS.setOptions(channelOpts);
      mentionsFS.setOptions(memberOpts);
      replyFS.setOptions(memberOpts);
    } catch (_) {
      // Member/channel lists are optional — the other filters still search.
    }
  })();

  // ── Search ──────────────────────────────────────────────────────────

  function buildFilterParams() {
    const params = new URLSearchParams();
    const regexVal = regexInput.value.trim();
    if (regexVal) params.set("regex", regexVal);
    for (const [key, chip] of active) FILTERS[key].apply(params, chip);
    return params;
  }

  async function doSearch(page = 1) {
    closeMenu();
    const params = buildFilterParams();
    params.set("page", String(page));
    params.set("per_page", "50");

    resultsEl.innerHTML = renderLoading("Searching messages…");
    pagerEl.innerHTML = "";
    openContext = null;

    downloadBtn.style.display = "none";
    try {
      const data = await api(`/api/messages/search?${params}`);
      renderResults(data);
      if (data.total > 0) downloadBtn.style.display = "";
    } catch (err) {
      resultsEl.innerHTML = renderError(`Couldn't run that search — ${err.message}. Check the regex pattern and try again.`);
    }
  }

  /** Markup shared by a search hit and a context row. */
  function messageHtml(m, { isHit = false } = {}) {
    const time = fmtTs(m.ts);
    const author = m.author_name || m.author_id;
    const channel = m.channel_name ? `#${m.channel_name}` : m.channel_id;

    let replyHtml = "";
    if (m.reply_to_id) {
      const replyAuthor = m.reply_to_author_name || m.reply_to_author_id || "unknown";
      replyHtml = `<div class="msg-reply">replying to <strong>${esc(replyAuthor)}</strong></div>`;
    }

    let attachHtml = "";
    if (m.attachments && m.attachments.length) {
      attachHtml = `<div class="msg-attachments">${m.attachments.map((u) =>
        `<a href="${esc(u)}" target="_blank" rel="noopener">[attachment]</a>`
      ).join(" ")}</div>`;
    }

    // A message stored under storage level "none" keeps its skeleton but no
    // text. Say so, rather than rendering a blank row that reads as a bug.
    const body = m.content
      ? `<div class="msg-content">${esc(m.content)}</div>`
      : `<div class="msg-content msg-content-empty">(no content stored)</div>`;

    return `
      <div class="msg-meta">
        <span class="msg-author">${esc(author)}</span>
        ${isHit ? `<span class="msg-channel">${esc(channel)}</span>` : ""}
        ${deletedBadge(m)}
        <span class="msg-time">${esc(time)}</span>
        ${sentimentBadge(m.sentiment)}
        ${emotionBadge(m.emotion)}
        ${discordLink(m)}
      </div>
      ${replyHtml}
      ${body}
      ${attachHtml}
    `;
  }

  function renderResults(data) {
    if (!data.messages.length) {
      resultsEl.innerHTML = renderEmpty("No messages match these filters. Clear a filter, widen the date range, or check the regex pattern.");
      pagerEl.innerHTML = "";
      return;
    }

    resultsEl.innerHTML = data.messages.map((m) => `
      <div class="msg-entry${m.deleted_at != null ? " msg-entry-deleted" : ""}" data-message-id="${esc(m.message_id)}">
        <div class="msg-entry-main" role="button" tabindex="0" aria-expanded="false">
          ${messageHtml(m, { isHit: true })}
        </div>
        <div class="msg-context" hidden></div>
      </div>
    `).join("");

    for (const main of resultsEl.querySelectorAll(".msg-entry-main")) {
      main.addEventListener("click", (e) => {
        // Let the deep link and attachment links work without expanding.
        if (e.target.closest("a")) return;
        toggleContext(main.parentElement);
      });
      main.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleContext(main.parentElement);
        }
      });
    }

    if (data.truncated) {
      pagerEl.innerHTML =
        `<span class="msg-pager-info">Showing the first ${data.total} matches — the scan hit its limit, so there may be more. Narrow the channel, author, or date range.</span> `;
    } else {
      pagerEl.innerHTML = "";
    }

    if (data.pages > 1) {
      let pagerHtml = `<span class="msg-pager-info">Page ${data.page} of ${data.pages} (${data.total} results)</span> `;
      if (data.page > 1) {
        pagerHtml += `<button class="btn btn-sm" data-page="${data.page - 1}">◀ Prev</button> `;
      }
      if (data.page < data.pages) {
        pagerHtml += `<button class="btn btn-sm" data-page="${data.page + 1}">Next ▶</button>`;
      }
      pagerEl.innerHTML += pagerHtml;
      pagerEl.querySelectorAll("button[data-page]").forEach((btn) => {
        btn.addEventListener("click", () => doSearch(parseInt(btn.dataset.page)));
      });
    } else if (!data.truncated) {
      pagerEl.innerHTML = `<span class="msg-pager-info">${data.total} result${data.total === 1 ? "" : "s"}</span>`;
    }
  }

  // ── Inline context ──────────────────────────────────────────────────
  //
  // One open at a time: opening another collapses the previous one, so the
  // results list never turns into a wall of nested scrollers.

  let openContext = null;

  function contextRowHtml(m, hitId) {
    const isHit = String(m.message_id) === String(hitId);
    return `
      <div class="msg-ctx-row${isHit ? " msg-ctx-hit" : ""}${m.deleted_at != null ? " msg-ctx-deleted" : ""}"
           data-message-id="${esc(m.message_id)}">
        ${messageHtml(m)}
      </div>
    `;
  }

  function renderContext(box, data, hitId) {
    const older = data.has_older
      ? `<button class="btn btn-sm" data-load="older">Load older</button>`
      : "";
    const newer = data.has_newer
      ? `<button class="btn btn-sm" data-load="newer">Load newer</button>`
      : "";
    box.innerHTML = `
      <div class="msg-ctx-end">${older}</div>
      <div class="msg-ctx-rows">${data.messages.map((m) => contextRowHtml(m, hitId)).join("")}</div>
      <div class="msg-ctx-end">${newer}</div>
    `;
    wireContextButtons(box, hitId);
    // Centre the hit in the scroll box rather than starting at the top.
    const hit = box.querySelector(".msg-ctx-hit");
    if (hit) box.scrollTop = Math.max(0, hit.offsetTop - box.clientHeight / 2);
  }

  function wireContextButtons(box, hitId) {
    for (const btn of box.querySelectorAll("[data-load]")) {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const direction = btn.dataset.load;
        const rows = box.querySelector(".msg-ctx-rows");
        const edge = direction === "older" ? rows.firstElementChild : rows.lastElementChild;
        if (!edge) return;
        const fromId = edge.dataset.messageId;

        btn.disabled = true;
        btn.textContent = "Loading…";
        try {
          const data = await api(
            `/api/messages/context?message_id=${encodeURIComponent(fromId)}&direction=${direction}`
          );
          const html = data.messages.map((m) => contextRowHtml(m, hitId)).join("");
          // Anchor the scroll position when prepending, so the rows the reader
          // is looking at don't jump off the top of the box.
          const before = box.scrollHeight;
          if (direction === "older") rows.insertAdjacentHTML("afterbegin", html);
          else rows.insertAdjacentHTML("beforeend", html);
          if (direction === "older") box.scrollTop += box.scrollHeight - before;

          if (data.has_more) {
            btn.disabled = false;
            btn.textContent = direction === "older" ? "Load older" : "Load newer";
          } else {
            btn.remove();
          }
        } catch (err) {
          btn.disabled = false;
          btn.textContent = "Retry";
        }
      });
    }
  }

  async function toggleContext(entry) {
    const box = entry.querySelector(".msg-context");
    const main = entry.querySelector(".msg-entry-main");

    if (openContext === entry) {
      box.hidden = true;
      box.innerHTML = "";
      main.setAttribute("aria-expanded", "false");
      openContext = null;
      return;
    }

    if (openContext) {
      const prev = openContext.querySelector(".msg-context");
      prev.hidden = true;
      prev.innerHTML = "";
      openContext.querySelector(".msg-entry-main").setAttribute("aria-expanded", "false");
    }

    openContext = entry;
    box.hidden = false;
    main.setAttribute("aria-expanded", "true");
    box.innerHTML = renderLoading("Loading context…");

    const messageId = entry.dataset.messageId;
    try {
      const data = await api(`/api/messages/context?message_id=${encodeURIComponent(messageId)}`);
      renderContext(box, data, messageId);
    } catch (err) {
      box.innerHTML = renderError(`Couldn't load the surrounding messages — ${err.message}.`);
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────

  searchBtn.addEventListener("click", () => doSearch(1));

  downloadBtn.addEventListener("click", () => {
    window.location = `/api/messages/search/export?${buildFilterParams()}`;
  });

  regexInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch(1);
  });

  return {
    unmount() {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onDocKey);
      for (const fs of [authorFS, channelFS, mentionsFS, replyFS]) fs.destroy();
    },
  };
}
