import { api, apiPost, esc } from "../api.js";
import { apiPut, apiDelete, showStatus } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

export function mountGamePanel(container, { gameType, gameName, gameIcon, hasBank = false, optSchema = [] }) {
  function ctrl(name) { return container.querySelector('[data-ctrl="' + name + '"]'); }
  function region(name) { return container.querySelector('[data-region="' + name + '"]'); }

  // Tag state (used by buildBankHtml below, which runs during initial render).
  const tagDatalistId = "bank-tags-dl-" + gameType;
  let knownTags = [];

  const optFieldsHtml = optSchema.map(opt => {
    if (opt.type === "bool") {
      return '<div class="field" style="margin-bottom:8px;"><label style="display:flex;align-items:center;gap:8px;cursor:pointer;">' +
        '<input type="checkbox" data-opt="' + esc(opt.key) + '" style="width:16px;height:16px;cursor:pointer;" />' +
        "<span>" + esc(opt.label) + "</span></label></div>";
    }
    if (opt.type === "text") {
      return '<div class="field"><label>' + esc(opt.label) +
        '<input type="text" data-opt="' + esc(opt.key) + '"' + (opt.placeholder ? ' placeholder="' + esc(opt.placeholder) + '"' : "") + ' style="width:240px;" /></label>' +
        (opt.hint ? '<div class="field-hint">' + esc(opt.hint) + "</div>" : "") + "</div>";
    }
    return '<div class="field"><label>' + esc(opt.label) +
      '<input type="number" data-opt="' + esc(opt.key) + '" min="' + (opt.min ?? 0) + '" max="' + (opt.max ?? 9999) + '" style="width:120px;" /></label>' +
      (opt.hint ? '<div class="field-hint">' + esc(opt.hint) + "</div>" : "") + "</div>";
  }).join("");

  const bankHtml = hasBank ? buildBankHtml() : "";

  container.innerHTML = '<div class="panel"><header><h2>' + esc(gameIcon) + " " + esc(gameName) + "</h2></header>" +
    "<section>" +
    '<div class="section-label">Status</div>' +
    '<div style="margin-bottom:' + (optSchema.length ? "16px" : "12px") + ';">' +
    '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:600;">' +
    '<input type="checkbox" data-ctrl="enabled" style="width:18px;height:18px;cursor:pointer;" />' +
    "<span>Enabled on this server</span></label></div>" +
    optFieldsHtml +
    '<div style="display:flex;align-items:center;gap:8px;margin-top:4px;">' +
    '<button class="btn btn-primary" data-action="save-config">Save</button>' +
    '<span data-status="config" class="save-status"></span></div></section>' +
    bankHtml + "</div>";

  async function loadConfig() {
    try {
      const data = await api("/api/games/config/games/" + encodeURIComponent(gameType));
      ctrl("enabled").checked = data.enabled !== false;
      const opts = data.options || {};
      for (const opt of optSchema) {
        const el = container.querySelector('[data-opt="' + opt.key + '"]');
        if (!el) continue;
        const val = Object.prototype.hasOwnProperty.call(opts, opt.key) ? opts[opt.key] : opt.default;
        if (opt.type === "bool") el.checked = val !== false && val !== 0;
        else el.value = val ?? "";
      }
    } catch (err) {
      console.error("Failed to load game config:", err);
    }
  }

  container.querySelector('[data-action="save-config"]').addEventListener("click", async () => {
    const st = container.querySelector('[data-status="config"]');
    const options = {};
    for (const opt of optSchema) {
      const el = container.querySelector('[data-opt="' + opt.key + '"]');
      if (!el) continue;
      if (opt.type === "bool") options[opt.key] = el.checked;
      else if (opt.type === "text") options[opt.key] = el.value.trim();
      else options[opt.key] = parseInt(el.value, 10) || 0;
    }
    try {
      await apiPut("/api/games/config/games/" + encodeURIComponent(gameType), { enabled: ctrl("enabled").checked, options });
      showStatus(st, true, "Saved");
    } catch (err) {
      showStatus(st, false, err.message);
    }
  });

  loadConfig();
  if (hasBank) initBank();

  return { unmount() {} };

  // ── Tag helpers (shared by list filter, add, bulk, inline edit) ────────────
  function parseTags(raw) {
    if (Array.isArray(raw)) return raw;
    if (typeof raw === "string" && raw) return raw.split(",").map(s => s.trim()).filter(Boolean);
    return [];
  }

  function refreshDatalist() {
    const dl = container.querySelector('[data-ctrl="tags-datalist"]');
    if (dl) dl.innerHTML = knownTags.map(t => '<option value="' + esc(t) + '"></option>').join("");
  }

  async function loadTags() {
    try {
      const data = await api("/api/games/bank/tags?game_type=" + encodeURIComponent(gameType));
      knownTags = data.tags || [];
    } catch { knownTags = []; }
    refreshDatalist();
  }

  // A chip-input widget: type a tag + Enter/comma to add a chip; click × to remove.
  // Suggestions come from the shared datalist. Returns { el, getTags, setTags }.
  function makeTagWidget(initial) {
    const tags = [];
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;flex-wrap:wrap;gap:4px;align-items:center;border:1px solid var(--rule);border-radius:var(--r);padding:4px 6px;min-height:32px;background:var(--bg);";
    const input = document.createElement("input");
    input.type = "text";
    input.setAttribute("list", tagDatalistId);
    input.placeholder = "add tag…";
    input.style.cssText = "border:none;outline:none;background:transparent;flex:1;min-width:70px;font-size:12px;color:inherit;";

    function render() {
      wrap.querySelectorAll(".bank-chip").forEach(c => c.remove());
      tags.forEach((tag, i) => {
        const chip = document.createElement("span");
        chip.className = "ll-tag bank-chip";
        chip.style.cssText = "display:inline-flex;align-items:center;gap:4px;";
        chip.appendChild(document.createTextNode(tag));
        const x = document.createElement("span");
        x.textContent = "×";
        x.style.cssText = "cursor:pointer;font-weight:700;";
        x.addEventListener("click", () => { tags.splice(i, 1); render(); });
        chip.appendChild(x);
        wrap.insertBefore(chip, input);
      });
    }
    function commit(val) {
      val = (val || "").trim().replace(/,+$/, "").trim();
      if (val && !tags.includes(val)) tags.push(val);
      input.value = "";
      render();
    }
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(input.value); }
      else if (e.key === "Backspace" && !input.value && tags.length) { tags.pop(); render(); }
    });
    input.addEventListener("blur", () => commit(input.value));
    wrap.appendChild(input);
    parseTags(initial).forEach(t => { const v = String(t).trim(); if (v && !tags.includes(v)) tags.push(v); });
    render();
    return {
      el: wrap,
      getTags() { commit(input.value); return tags.slice(); },
      setTags(arr) { tags.length = 0; parseTags(arr).forEach(t => tags.push(t)); render(); },
    };
  }

  function buildBankHtml() {
    return '<section>' +
      '<div class="section-label">Question Bank</div>' +
      '<div class="field-hint" style="margin-bottom:12px;">Questions used by this game. Tag content to organize it; the reserved <strong>nsfw</strong> tag marks adult content, which games include by default.</div>' +
      '<datalist data-ctrl="tags-datalist"></datalist>' +
      '<div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;">' +
      '<div style="flex:1;min-width:260px;">' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px;">' +
      '<div class="field" style="margin:0;"><label>Tag<input type="text" data-ctrl="filter-tag" list="' + esc(tagDatalistId) + '" placeholder="Any" style="width:120px;" /></label></div>' +
      '<div class="field" style="margin:0;flex:1;min-width:160px;"><label>Search<input type="text" data-ctrl="search" placeholder="Filter..." style="width:100%;" /></label></div>' +
      '<button class="btn" data-action="search-btn">Search</button>' +
      "</div>" +
      '<div data-region="bank-list"><div class="empty">Loading...</div></div>' +
      '<div data-region="bank-pagination" style="display:flex;gap:6px;align-items:center;margin-top:8px;flex-wrap:wrap;"></div>' +
      "</div>" +
      '<div style="width:300px;flex-shrink:0;">' +
      '<section style="background:var(--bg);border:1px solid var(--rule);border-radius:var(--r);padding:16px;">' +
      '<div class="section-label" style="margin-bottom:10px;">Add Question</div>' +
      '<div class="field"><label>Tags<div data-ctrl="add-tags"></div></label></div>' +
      '<div class="field"><label>Question<textarea data-ctrl="add-text" rows="3" style="width:100%;"></textarea></label></div>' +
      '<div style="display:flex;gap:8px;align-items:center;"><button class="btn btn-primary" data-action="add-question">Add</button><span data-status="add" class="save-status"></span></div>' +
      "</section>" +
      '<section style="background:var(--bg-card);border:1px solid var(--rule);border-radius:var(--r);padding:16px;margin-top:12px;">' +
      '<div class="section-label" style="margin-bottom:10px;">Bulk Import</div>' +
      '<div class="field"><label>Tags (applied to all)<div data-ctrl="bulk-tags"></div></label></div>' +
      '<div class="field"><label>Lines (one per line)<textarea data-ctrl="bulk-text" rows="6" style="width:100%;font-size:12px;"></textarea></label></div>' +
      '<div style="display:flex;gap:8px;align-items:center;"><button class="btn btn-primary" data-action="bulk-import">Import</button><span data-status="bulk" class="save-status"></span></div>' +
      "</section></div></div></section>";
  }

  function initBank() {
    let currentPage = 1;
    let currentTag = "";
    let currentSearch = "";

    // Mount the persistent add/bulk tag widgets.
    const addTags = makeTagWidget([]);
    ctrl("add-tags").appendChild(addTags.el);
    const bulkTags = makeTagWidget([]);
    ctrl("bulk-tags").appendChild(bulkTags.el);

    async function loadBank() {
      const el = region("bank-list");
      el.innerHTML = '<div class="empty">Loading...</div>';
      const params = new URLSearchParams({ game_type: gameType, page: currentPage, per_page: 50 });
      if (currentTag) params.set("tag", currentTag);
      if (currentSearch) params.set("search", currentSearch);
      try {
        const data = await api("/api/games/bank?" + params);
        const qs = data.questions || [];
        if (!qs.length) {
          el.innerHTML = '<div class="empty">No questions found.</div>';
          region("bank-pagination").innerHTML = "";
          return;
        }
        let rows = "";
        for (const q of qs) {
          const chips = parseTags(q.tags).map(t => '<span class="ll-tag">' + esc(t) + "</span>").join(" ");
          rows += '<tr data-qid="' + q.question_id + '">' +
            '<td class="bank-tags-cell" style="width:160px;"><div class="ll-tags">' + chips + "</div></td>" +
            '<td class="bank-text-cell" style="padding-right:8px;">' + esc(q.question_text) + "</td>" +
            '<td style="width:120px;white-space:nowrap;">' +
            '<button class="btn" style="padding:2px 6px;font-size:12px;margin-right:4px;" data-action="edit-q" data-qid="' + q.question_id + '">Edit</button>' +
            '<button class="btn" style="padding:2px 6px;font-size:12px;" data-action="del-q" data-qid="' + q.question_id + '">Del</button>' +
            "</td></tr>";
        }
        el.innerHTML = '<table style="width:100%;border-collapse:collapse;" class="data-table">' +
          '<thead><tr><th style="width:160px;">Tags</th><th>Question</th><th style="width:120px;"></th></tr></thead>' +
          "<tbody>" + rows + "</tbody></table>";

        const pag = region("bank-pagination");
        const totalPages = data.total_pages || 1;
        const count = data.total || 0;
        if (totalPages <= 1) {
          pag.innerHTML = '<span style="font-size:12px;color:var(--ink-muted);">' + count + " question" + (count !== 1 ? "s" : "") + "</span>";
          return;
        }
        let pagHtml = '<span style="font-size:12px;color:var(--ink-muted);margin-right:4px;">' + count + " questions</span>";
        if (currentPage > 1) pagHtml += '<button class="btn" data-page="' + (currentPage - 1) + '" style="padding:2px 8px;font-size:12px;">&#8249;</button>';
        pagHtml += '<span style="font-size:12px;padding:0 4px;">Page ' + currentPage + " / " + totalPages + "</span>";
        if (currentPage < totalPages) pagHtml += '<button class="btn" data-page="' + (currentPage + 1) + '" style="padding:2px 8px;font-size:12px;">&#8250;</button>';
        pag.innerHTML = pagHtml;
        pag.querySelectorAll("[data-page]").forEach(b => {
          b.addEventListener("click", () => { currentPage = parseInt(b.dataset.page, 10); loadBank(); });
        });
      } catch (err) {
        el.innerHTML = '<div class="empty">Error: ' + esc(err.message) + "</div>";
      }
    }

    // Delegated edit/save/delete handler — attached once; loadBank() only
    // replaces the list's innerHTML, so re-attaching there would stack
    // handlers. Edit is handled here too (rather than as a per-row direct
    // listener) so a single click can't both open the editor AND bubble into
    // the save branch after the button flips to data-action="save-q".
    region("bank-list").addEventListener("click", async (e) => {
      const btn = e.target.closest('[data-action="edit-q"],[data-action="save-q"],[data-action="del-q"]');
      if (!btn) return;
      const qid = btn.dataset.qid;
      const list = region("bank-list");
      const row = list.querySelector('tr[data-qid="' + qid + '"]');
      if (btn.dataset.action === "edit-q") {
        const textCell = row.querySelector(".bank-text-cell");
        const tagsCell = row.querySelector(".bank-tags-cell");
        const origText = textCell.textContent;
        const origTags = Array.from(tagsCell.querySelectorAll(".ll-tag")).map(s => s.textContent.trim());
        textCell.innerHTML = '<textarea class="field-input" style="width:100%;min-height:60px;">' + esc(origText) + "</textarea>";
        const widget = makeTagWidget(origTags);
        tagsCell.innerHTML = "";
        tagsCell.appendChild(widget.el);
        row._tagWidget = widget;
        btn.textContent = "Save";
        btn.dataset.action = "save-q";
      } else if (btn.dataset.action === "save-q") {
        const newText = row.querySelector("textarea") && row.querySelector("textarea").value.trim();
        const newTags = row._tagWidget ? row._tagWidget.getTags() : [];
        if (!newText) return;
        try { await apiPut("/api/games/bank/" + qid, { question_text: newText, tags: newTags }); await loadTags(); loadBank(); }
        catch (err) { toast("Save failed: " + err.message, "error"); }
      } else if (btn.dataset.action === "del-q") {
        if (!(await confirmDialog("Delete this question?", { danger: true, confirmLabel: "Delete" }))) return;
        try { await apiDelete("/api/games/bank/" + qid); loadBank(); }
        catch (err) { toast("Delete failed: " + err.message, "error"); }
      }
    });

    ctrl("filter-tag").addEventListener("change", () => { currentTag = ctrl("filter-tag").value.trim(); currentPage = 1; loadBank(); });
    ctrl("search").addEventListener("keydown", e => {
      if (e.key === "Enter") { currentSearch = ctrl("search").value.trim(); currentPage = 1; loadBank(); }
    });
    container.querySelector('[data-action="search-btn"]').addEventListener("click", () => {
      currentSearch = ctrl("search").value.trim();
      currentTag = ctrl("filter-tag").value.trim();
      currentPage = 1; loadBank();
    });
    container.querySelector('[data-action="add-question"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="add"]');
      const text = ctrl("add-text").value.trim();
      if (!text) { showStatus(st, false, "Question text required"); return; }
      try {
        await apiPost("/api/games/bank", { game_type: gameType, tags: addTags.getTags(), question_text: text });
        ctrl("add-text").value = "";
        addTags.setTags([]);
        showStatus(st, true, "Added");
        await loadTags();
        loadBank();
      } catch (err) { showStatus(st, false, err.message); }
    });
    container.querySelector('[data-action="bulk-import"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="bulk"]');
      const raw = ctrl("bulk-text").value;
      const lines = raw.split("\n").map(l => l.trim()).filter(Boolean);
      if (!lines.length) { showStatus(st, false, "No lines to import"); return; }
      try {
        const res = await apiPost("/api/games/bank/bulk", { game_type: gameType, tags: bulkTags.getTags(), lines });
        ctrl("bulk-text").value = "";
        showStatus(st, true, "Imported " + res.added);
        await loadTags();
        loadBank();
      } catch (err) { showStatus(st, false, err.message); }
    });
    loadTags();
    loadBank();
  }
}
