import { api } from "../api.js";

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/**
 * Build a filterable select widget: a text input that filters a dropdown list.
 * Returns { el, getValue } where el is the container DOM node and getValue()
 * returns the currently selected value (user ID string, or "").
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

  return {
    el: wrap,
    getValue: () => selectedId,
    getInput: () => input,
  };
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
        <label>Channel
          <select data-field="channel"><option value="">All channels</option></select>
        </label>
        <label>Regex
          <input type="text" data-field="regex" placeholder="PCRE pattern" />
        </label>
        <label>Sort
          <select data-field="sort">
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
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
  const resultsEl = container.querySelector("[data-results]");
  const pagerEl = container.querySelector("[data-pager]");
  const searchBtn = container.querySelector("[data-search]");

  // Placeholder filter-selects (replaced once members load)
  let authorFS = filterSelect("Loading…", []);
  let mentionsFS = filterSelect("Loading…", []);
  let replyFS = filterSelect("Loading…", []);

  container.querySelector('[data-slot="author"]').appendChild(authorFS.el);
  container.querySelector('[data-slot="mentions"]').appendChild(mentionsFS.el);
  container.querySelector('[data-slot="reply_to"]').appendChild(replyFS.el);

  // Load members and channels in parallel
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
      }));

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

    params.set("sort", sortSel.value);
    params.set("page", String(page));
    params.set("per_page", "50");

    resultsEl.innerHTML = `<div class="empty">Searching…</div>`;
    pagerEl.innerHTML = "";

    try {
      const data = await api(`/api/messages/search?${params}`);
      renderResults(data);
    } catch (err) {
      resultsEl.innerHTML = `<div class="error">${err.message}</div>`;
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
