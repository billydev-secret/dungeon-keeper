import { api, esc } from "../api.js";

/**
 * Build a filterable select widget: a text input that filters a dropdown list.
 * Returns { el, getValue, getInput, setValue } where el is the container DOM node,
 * getValue() returns the currently selected value (user ID string, or ""),
 * and setValue(id) programmatically sets the selection.
 */
function filterSelect(placeholder, options) {
  // options: [{ id, label }]
  const wrap = document.createElement("div");
  wrap.className = "filter-select";

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = placeholder;
  input.className = "filter-select-input";
  wrap.appendChild(input);

  const list = document.createElement("div");
  list.className = "filter-select-list";
  wrap.appendChild(list);

  let selectedId = "";
  let selectedLabel = "";

  function render(filter) {
    const lc = filter.toLowerCase();
    const matches = lc
      ? options.filter((o) => o.label.toLowerCase().includes(lc))
      : options;
    const show = matches.slice(0, 80);
    list.innerHTML = `<div class="filter-select-item" data-id="">
        <em style="color:var(--text-dim)">(any)</em>
      </div>` +
      show.map((o) => `<div class="filter-select-item" data-id="${esc(o.id)}">${esc(o.label)}</div>`).join("");
  }

  input.addEventListener("focus", () => {
    render(input.value);
    list.style.display = "block";
  });

  input.addEventListener("input", () => {
    selectedId = "";
    selectedLabel = "";
    render(input.value);
    list.style.display = "block";
  });

  list.addEventListener("mousedown", (e) => {
    // mousedown instead of click so it fires before blur
    const item = e.target.closest(".filter-select-item");
    if (!item) return;
    selectedId = item.dataset.id;
    selectedLabel = selectedId ? item.textContent : "";
    input.value = selectedLabel;
    list.style.display = "none";
  });

  input.addEventListener("blur", () => {
    // Small delay so mousedown on list fires first
    setTimeout(() => { list.style.display = "none"; }, 150);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      list.style.display = "none";
      input.blur();
    }
  });

  function setValue(id) {
    selectedId = String(id || "");
    const match = options.find((o) => o.id === selectedId);
    selectedLabel = match ? match.label : selectedId;
    input.value = selectedId ? selectedLabel : "";
  }

  return {
    el: wrap,
    getValue: () => selectedId,
    getInput: () => input,
    setValue,
  };
}

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

export function mount(container) {
  container.innerHTML = `
    <div class="panel" style="overflow-y:auto;">
      <header>
        <h2>Message Search</h2>
        <div class="subtitle">Search and read back stored messages</div>
      </header>
      <div class="ai-query-bar">
        <select data-ai-model class="ai-model-select">
          <option value="">Loading models…</option>
        </select>
        <input type="text" data-ai-query placeholder="Ask AI: e.g. &quot;show me popular messages from today&quot;" class="ai-query-input" />
        <button data-ai-search class="msg-search-btn ai-btn">Ask AI</button>
        <span data-ai-status class="ai-status"></span>
      </div>
      <div class="controls msg-search-controls">
        <label>Author<span data-slot="author"></span></label>
        <label>Mentions<span data-slot="mentions"></span></label>
        <label>Reply To<span data-slot="reply_to"></span></label>
        <label>Channel
          <select data-field="channel"><option value="">All channels</option></select>
        </label>
        <label>Regex
          <input type="text" data-field="regex" placeholder="PCRE pattern" />
        </label>
        <label>Emotion
          <select data-field="emotion">
            <option value="">Any</option>
            <option value="joy">Joy</option>
            <option value="playful">Playful</option>
            <option value="anger">Anger</option>
            <option value="frustration">Frustration</option>
            <option value="neutral">Neutral</option>
          </select>
        </label>
        <label>Sentiment
          <div style="display:flex;gap:4px;align-items:center;">
            <input type="number" data-field="sentiment_min" min="-1" max="1" step="0.1" placeholder="Min" style="width:60px" />
            <span>to</span>
            <input type="number" data-field="sentiment_max" min="-1" max="1" step="0.1" placeholder="Max" style="width:60px" />
          </div>
        </label>
        <label>Attachments
          <select data-field="has_attachments">
            <option value="">Any</option>
            <option value="true">Has attachments</option>
            <option value="false">No attachments</option>
          </select>
        </label>
        <label>Reactions
          <select data-field="has_reactions">
            <option value="">Any</option>
            <option value="true">Has reactions</option>
            <option value="false">No reactions</option>
          </select>
        </label>
        <label>Min Length
          <input type="number" data-field="min_length" min="0" placeholder="chars" style="width:70px" />
        </label>
        <label>Max Length
          <input type="number" data-field="max_length" min="0" placeholder="chars" style="width:70px" />
        </label>
        <label>After
          <input type="datetime-local" data-field="after_dt" />
        </label>
        <label>Before
          <input type="datetime-local" data-field="before_dt" />
        </label>
        <label>Sort
          <select data-field="sort">
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="most_reacted">Most reacted</option>
            <option value="longest">Longest first</option>
            <option value="most_positive">Most positive</option>
            <option value="most_negative">Most negative</option>
          </select>
        </label>
        <label>&nbsp;
          <button data-search class="msg-search-btn">Search</button>
        </label>
      </div>
      <div data-results class="msg-results"></div>
      <div data-pager class="msg-pager"></div>
    </div>
  `;

  const channelSel = container.querySelector('[data-field="channel"]');
  const regexInput = container.querySelector('[data-field="regex"]');
  const sortSel = container.querySelector('[data-field="sort"]');
  const emotionSel = container.querySelector('[data-field="emotion"]');
  const sentMinInput = container.querySelector('[data-field="sentiment_min"]');
  const sentMaxInput = container.querySelector('[data-field="sentiment_max"]');
  const attachSel = container.querySelector('[data-field="has_attachments"]');
  const reactionsSel = container.querySelector('[data-field="has_reactions"]');
  const minLenInput = container.querySelector('[data-field="min_length"]');
  const maxLenInput = container.querySelector('[data-field="max_length"]');
  const afterDtInput = container.querySelector('[data-field="after_dt"]');
  const beforeDtInput = container.querySelector('[data-field="before_dt"]');
  const resultsEl = container.querySelector("[data-results]");
  const pagerEl = container.querySelector("[data-pager]");
  const searchBtn = container.querySelector("[data-search]");

  // AI elements
  const aiModelSel = container.querySelector("[data-ai-model]");
  const aiQueryInput = container.querySelector("[data-ai-query]");
  const aiSearchBtn = container.querySelector("[data-ai-search]");
  const aiStatusEl = container.querySelector("[data-ai-status]");

  // Placeholder filter-selects (replaced once members load)
  let authorFS = filterSelect("Loading…", []);
  let mentionsFS = filterSelect("Loading…", []);
  let replyFS = filterSelect("Loading…", []);

  container.querySelector('[data-slot="author"]').appendChild(authorFS.el);
  container.querySelector('[data-slot="mentions"]').appendChild(mentionsFS.el);
  container.querySelector('[data-slot="reply_to"]').appendChild(replyFS.el);

  // Load members, channels, and AI models in parallel
  (async () => {
    try {
      const [members, channels] = await Promise.all([
        api("/api/meta/members"),
        api("/api/meta/channels"),
      ]);

      // Populate channel dropdown
      for (const ch of channels) {
        const opt = document.createElement("option");
        opt.value = ch.id;
        opt.textContent = `#${ch.name}`;
        channelSel.appendChild(opt);
      }

      // Build member options
      const memberOpts = members.map((m) => ({
        id: m.id,
        label: m.display_name !== m.name ? `${m.display_name} (${m.name})` : m.name,
        left: !!m.left_server,
      })).sort((a, b) => a.left - b.left || a.label.localeCompare(b.label));
      memberOpts.forEach((o) => { if (o.left) o.label += " (left)"; });

      // Replace placeholder filter-selects with populated ones
      function replaceFS(slot, oldFS) {
        const fs = filterSelect("Type to filter…", memberOpts);
        oldFS.el.replaceWith(fs.el);
        return fs;
      }
      authorFS = replaceFS("author", authorFS);
      mentionsFS = replaceFS("mentions", mentionsFS);
      replyFS = replaceFS("reply_to", replyFS);

      // Wire up Enter key on the new inputs
      [authorFS, mentionsFS, replyFS].forEach((fs) => {
        fs.getInput().addEventListener("keydown", (e) => {
          if (e.key === "Enter") doSearch(1);
        });
      });
    } catch (_) {}
  })();

  // Load AI models
  (async () => {
    try {
      const data = await api("/api/messages/ai-models");
      aiModelSel.innerHTML = "";
      for (const m of data.models) {
        const opt = document.createElement("option");
        opt.value = m;
        // Show a friendly short name
        opt.textContent = m.replace("claude-", "").replace(/-\d{8}$/, "");
        aiModelSel.appendChild(opt);
      }
    } catch (_) {
      aiModelSel.innerHTML = `<option value="">No models available</option>`;
    }
  })();

  // --- AI Query ---
  async function doAiQuery() {
    const query = aiQueryInput.value.trim();
    if (!query) return;
    aiStatusEl.textContent = "Thinking…";
    aiSearchBtn.disabled = true;

    try {
      const res = await fetch("/api/messages/ai-query", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, model: aiModelSel.value || null }),
      });
      if (res.status === 401) { window.location = "/login"; return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);

      applyFilters(data.filters);
      aiStatusEl.textContent = data.explanation || "Filters applied";
      doSearch(1);
    } catch (err) {
      aiStatusEl.textContent = `Error: ${esc(err.message)}`;
    } finally {
      aiSearchBtn.disabled = false;
    }
  }

  function applyFilters(f) {
    // Reset all controls first
    channelSel.value = "";
    regexInput.value = "";
    sortSel.value = "newest";
    emotionSel.value = "";
    sentMinInput.value = "";
    sentMaxInput.value = "";
    attachSel.value = "";
    reactionsSel.value = "";
    minLenInput.value = "";
    maxLenInput.value = "";
    afterDtInput.value = "";
    beforeDtInput.value = "";
    authorFS.setValue("");
    mentionsFS.setValue("");
    replyFS.setValue("");

    if (f.author) authorFS.setValue(String(f.author));
    if (f.mentions) mentionsFS.setValue(String(f.mentions));
    if (f.reply_to) replyFS.setValue(String(f.reply_to));
    if (f.channel) channelSel.value = String(f.channel);
    if (f.regex) regexInput.value = f.regex;
    if (f.sort) sortSel.value = f.sort;
    if (f.emotion) emotionSel.value = f.emotion;
    if (f.sentiment_min != null) sentMinInput.value = f.sentiment_min;
    if (f.sentiment_max != null) sentMaxInput.value = f.sentiment_max;
    if (f.has_attachments != null) attachSel.value = String(f.has_attachments);
    if (f.has_reactions != null) reactionsSel.value = String(f.has_reactions);
    if (f.min_length != null) minLenInput.value = f.min_length;
    if (f.max_length != null) maxLenInput.value = f.max_length;

    // Convert unix timestamps to datetime-local format
    if (f.after) {
      afterDtInput.value = new Date(f.after * 1000).toISOString().slice(0, 16);
    }
    if (f.before) {
      beforeDtInput.value = new Date(f.before * 1000).toISOString().slice(0, 16);
    }
  }

  aiSearchBtn.addEventListener("click", doAiQuery);
  aiQueryInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doAiQuery();
  });

  // --- Search ---
  async function doSearch(page = 1) {
    const params = new URLSearchParams();

    const authorVal = authorFS.getValue();
    if (authorVal) params.set("author", authorVal);

    const mentionsVal = mentionsFS.getValue();
    if (mentionsVal) params.set("mentions", mentionsVal);

    const replyVal = replyFS.getValue();
    if (replyVal) params.set("reply_to", replyVal);

    const channelVal = channelSel.value;
    if (channelVal) params.set("channel", channelVal);

    const regexVal = regexInput.value.trim();
    if (regexVal) params.set("regex", regexVal);

    const emotionVal = emotionSel.value;
    if (emotionVal) params.set("emotion", emotionVal);

    if (sentMinInput.value !== "") params.set("sentiment_min", sentMinInput.value);
    if (sentMaxInput.value !== "") params.set("sentiment_max", sentMaxInput.value);

    if (attachSel.value) params.set("has_attachments", attachSel.value);
    if (reactionsSel.value) params.set("has_reactions", reactionsSel.value);

    if (minLenInput.value !== "") params.set("min_length", minLenInput.value);
    if (maxLenInput.value !== "") params.set("max_length", maxLenInput.value);

    // Convert datetime-local to unix timestamps
    if (afterDtInput.value) {
      params.set("after", String(Math.floor(new Date(afterDtInput.value).getTime() / 1000)));
    }
    if (beforeDtInput.value) {
      params.set("before", String(Math.floor(new Date(beforeDtInput.value).getTime() / 1000)));
    }

    params.set("sort", sortSel.value);
    params.set("page", String(page));
    params.set("per_page", "50");

    resultsEl.innerHTML = `<div class="empty">Searching…</div>`;
    pagerEl.innerHTML = "";

    try {
      const data = await api(`/api/messages/search?${params}`);
      renderResults(data);
    } catch (err) {
      resultsEl.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    }
  }

  function renderResults(data) {
    if (!data.messages.length) {
      resultsEl.innerHTML = `<div class="empty">No messages found</div>`;
      pagerEl.innerHTML = "";
      return;
    }

    const html = data.messages.map((m) => {
      const time = new Date(m.ts * 1000).toLocaleString();
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

      return `
        <div class="msg-entry">
          <div class="msg-meta">
            <span class="msg-author">${esc(author)}</span>
            <span class="msg-channel">${esc(channel)}</span>
            <span class="msg-time">${esc(time)}</span>
            ${sentimentBadge(m.sentiment)}
            ${emotionBadge(m.emotion)}
          </div>
          ${replyHtml}
          <div class="msg-content">${esc(m.content)}</div>
          ${attachHtml}
        </div>
      `;
    }).join("");

    resultsEl.innerHTML = html;

    // Pager
    if (data.pages > 1) {
      let pagerHtml = `<span class="msg-pager-info">Page ${data.page} of ${data.pages} (${data.total} results)</span> `;
      if (data.page > 1) {
        pagerHtml += `<button data-page="${data.page - 1}">\u25C0 Prev</button> `;
      }
      if (data.page < data.pages) {
        pagerHtml += `<button data-page="${data.page + 1}">Next \u25B6</button>`;
      }
      pagerEl.innerHTML = pagerHtml;
      pagerEl.querySelectorAll("button[data-page]").forEach((btn) => {
        btn.addEventListener("click", () => doSearch(parseInt(btn.dataset.page)));
      });
    } else {
      pagerEl.innerHTML = `<span class="msg-pager-info">${data.total} result${data.total === 1 ? "" : "s"}</span>`;
    }
  }

  searchBtn.addEventListener("click", () => doSearch(1));

  // Enter key on regex input
  regexInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch(1);
  });
}
