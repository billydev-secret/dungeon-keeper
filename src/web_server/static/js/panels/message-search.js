import { api, esc, fmtTs } from "../api.js";
import { filterSelect, multiFilterSelect } from "../filter-select.js";
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

export function mount(container) {
  container.innerHTML = `
    <div class="panel" style="overflow-y:auto;">
      <header>
        <h2>Message Search</h2>
        <div class="subtitle">Search and read back stored messages</div>
      </header>
      <div class="controls msg-search-controls">
        <label>Author<span data-slot="author"></span></label>
        <label>Mentions<span data-slot="mentions"></span></label>
        <label>Reply To<span data-slot="reply_to"></span></label>
        <label>Channel<span data-slot="channel"></span></label>
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
            <option value="true">Has Attachments</option>
            <option value="false">No Attachments</option>
          </select>
        </label>
        <label>Reactions
          <select data-field="has_reactions">
            <option value="">Any</option>
            <option value="true">Has Reactions</option>
            <option value="false">No Reactions</option>
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
            <option value="newest">Newest First</option>
            <option value="oldest">Oldest First</option>
            <option value="most_reacted">Most Reacted</option>
            <option value="longest">Longest First</option>
            <option value="most_positive">Most Positive</option>
            <option value="most_negative">Most Negative</option>
          </select>
        </label>
        <label>&nbsp;
          <button data-search class="btn btn-primary">Search</button>
        </label>
        <label>&nbsp;
          <button data-download class="btn" style="display:none">Download JSON</button>
        </label>
      </div>
      <div data-results class="msg-results"></div>
      <div data-pager class="msg-pager"></div>
    </div>
  `;

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
  const downloadBtn = container.querySelector("[data-download]");

  // Placeholder filter-selects (replaced once members/channels load)
  const authorFS = multiFilterSelect("Type to filter…", [], { label: "Author" });
  const mentionsFS = filterSelect("Type to filter…", [], { label: "Mentions", emptyLabel: "(anyone)" });
  const replyFS = filterSelect("Type to filter…", [], { label: "Reply to", emptyLabel: "(anyone)" });
  const channelFS = multiFilterSelect("Type to filter…", [], { label: "Channel" });

  container.querySelector('[data-slot="author"]').appendChild(authorFS.el);
  container.querySelector('[data-slot="mentions"]').appendChild(mentionsFS.el);
  container.querySelector('[data-slot="reply_to"]').appendChild(replyFS.el);
  container.querySelector('[data-slot="channel"]').appendChild(channelFS.el);

  // Enter anywhere in a picker runs the search.
  for (const fs of [authorFS, mentionsFS, replyFS, channelFS]) {
    fs.getInput().addEventListener("keydown", (e) => {
      if (e.key === "Enter") doSearch(1);
    });
  }

  // Load members, channels, and AI models in parallel
  (async () => {
    try {
      const [members, channels] = await Promise.all([
        api("/api/meta/members"),
        api("/api/meta/channels"),
      ]);

      // Build member options
      const memberOpts = members.map((m) => ({
        id: m.id,
        label: m.display_name !== m.name ? `${m.display_name} (${m.name})` : m.name,
        left: !!m.left_server,
      })).sort((a, b) => a.left - b.left || a.label.localeCompare(b.label));
      memberOpts.forEach((o) => { if (o.left) o.label += " (left)"; });

      // Build channel options
      const channelOpts = channels.map((ch) => ({
        id: String(ch.id),
        label: `#${ch.name}`,
      }));

      authorFS.setOptions(memberOpts);
      channelFS.setOptions(channelOpts);
      mentionsFS.setOptions(memberOpts);
      replyFS.setOptions(memberOpts);
    } catch (_) {
      // Member/channel lists are optional — the other filters still search.
    }
  })();

  function buildFilterParams() {
    const params = new URLSearchParams();
    for (const id of authorFS.getValues()) params.append("author", id);
    for (const id of channelFS.getValues()) params.append("channel", id);
    const mentionsVal = mentionsFS.getValue();
    if (mentionsVal) params.set("mentions", mentionsVal);
    const replyVal = replyFS.getValue();
    if (replyVal) params.set("reply_to", replyVal);
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
    if (afterDtInput.value) {
      params.set("after", String(Math.floor(new Date(afterDtInput.value).getTime() / 1000)));
    }
    if (beforeDtInput.value) {
      params.set("before", String(Math.floor(new Date(beforeDtInput.value).getTime() / 1000)));
    }
    params.set("sort", sortSel.value);
    return params;
  }

  // --- Search ---
  async function doSearch(page = 1) {
    const params = buildFilterParams();
    params.set("page", String(page));
    params.set("per_page", "50");

    resultsEl.innerHTML = renderLoading("Searching messages…");
    pagerEl.innerHTML = "";

    downloadBtn.style.display = "none";
    try {
      const data = await api(`/api/messages/search?${params}`);
      renderResults(data);
      if (data.total > 0) downloadBtn.style.display = "";
    } catch (err) {
      resultsEl.innerHTML = renderError(`Couldn't run that search — ${err.message}. Check the regex pattern and try again.`);
    }
  }

  function renderResults(data) {
    if (!data.messages.length) {
      resultsEl.innerHTML = renderEmpty("No messages match these filters. Clear a filter, widen the date range, or check the regex pattern.");
      pagerEl.innerHTML = "";
      return;
    }

    const html = data.messages.map((m) => {
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
        pagerHtml += `<button class="btn btn-sm" data-page="${data.page - 1}">\u25C0 Prev</button> `;
      }
      if (data.page < data.pages) {
        pagerHtml += `<button class="btn btn-sm" data-page="${data.page + 1}">Next \u25B6</button>`;
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

  downloadBtn.addEventListener("click", () => {
    const params = buildFilterParams();
    window.location = `/api/messages/search/export?${params}`;
  });

  // Enter key on regex input
  regexInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch(1);
  });

  // Nothing to tear down beyond the DOM the router replaces, but every other
  // panel returns a handle — keep the contract uniform (W-D15).
  return { unmount() {} };
}
