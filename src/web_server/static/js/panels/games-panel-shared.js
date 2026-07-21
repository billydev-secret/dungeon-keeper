import { api, apiPost, esc } from "../api.js";
import { apiPut, apiDelete, showStatus, loadRoles, roleSelect } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

// hasStatus=false drops the Enabled/options section for features that manage
// their own enable switch outside games_game_config (e.g. Pen Pals config).
export function mountGamePanel(container, { gameType, gameName, gameIcon, hasBank = false, hasStatus = true, optSchema = [], bankHint = "", bankCategories = null }) {
  function ctrl(name) { return container.querySelector('[data-ctrl="' + name + '"]'); }
  function region(name) { return container.querySelector('[data-region="' + name + '"]'); }

  // Category mode: instead of free-form tags, every question carries exactly
  // one required category (a single reserved tag). The tag inputs become a
  // required <select>, and the server rejects anything else. Used by
  // Traditional Truth-or-Dare, whose four categories double as bank tags.
  const catMode = Array.isArray(bankCategories) && bankCategories.length > 0;
  const catLabel = (v) => { const c = catMode && bankCategories.find(x => x.value === v); return c ? c.label : v; };

  // Tag state (used by buildBankHtml below, which runs during initial render).
  const tagDatalistId = "bank-tags-dl-" + gameType;
  let knownTags = [];

  const optFieldsHtml = optSchema.map(opt => {
    if (opt.type === "bool") {
      return '<div class="field mb-8"><label style="display:flex;align-items:center;gap:8px;cursor:pointer;">' +
        '<input type="checkbox" data-opt="' + esc(opt.key) + '" style="width:16px;height:16px;cursor:pointer;" />' +
        "<span>" + esc(opt.label) + "</span></label></div>";
    }
    if (opt.type === "text") {
      return '<div class="field"><label>' + esc(opt.label) +
        '<input type="text" data-opt="' + esc(opt.key) + '"' + (opt.placeholder ? ' placeholder="' + esc(opt.placeholder) + '"' : "") + ' style="width:240px;" /></label>' +
        (opt.hint ? '<div class="field-hint">' + esc(opt.hint) + "</div>" : "") + "</div>";
    }
    if (opt.type === "role") {
      // Options are filled in loadConfig() once the role list has loaded.
      return '<div class="field"><label>' + esc(opt.label) +
        '<select data-opt="' + esc(opt.key) + '" style="width:240px;"></select></label>' +
        (opt.hint ? '<div class="field-hint">' + esc(opt.hint) + "</div>" : "") + "</div>";
    }
    return '<div class="field"><label>' + esc(opt.label) +
      '<input type="number" data-opt="' + esc(opt.key) + '" min="' + (opt.min ?? 0) + '" max="' + (opt.max ?? 9999) + '" style="width:120px;" /></label>' +
      (opt.hint ? '<div class="field-hint">' + esc(opt.hint) + "</div>" : "") + "</div>";
  }).join("");

  const bankHtml = hasBank ? buildBankHtml() : "";

  const statusHtml = hasStatus
    ? "<section>" +
      '<div class="section-label">Status</div>' +
      '<div style="margin-bottom:' + (optSchema.length ? "16px" : "12px") + ';">' +
      '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:600;">' +
      '<input type="checkbox" data-ctrl="enabled" style="width:18px;height:18px;cursor:pointer;" />' +
      "<span>Enabled on this server</span></label></div>" +
      optFieldsHtml +
      '<div style="display:flex;align-items:center;gap:8px;margin-top:4px;">' +
      '<button class="btn btn-primary" data-action="save-config">Save</button>' +
      '<span data-status="config" class="save-status"></span></div></section>'
    : "";

  container.innerHTML = '<div class="panel"><header><h2>' + esc(gameIcon) + " " + esc(gameName) + "</h2></header>" +
    statusHtml + bankHtml + "</div>";

  async function loadConfig() {
    try {
      const data = await api("/api/games/config/games/" + encodeURIComponent(gameType));
      ctrl("enabled").checked = data.enabled !== false;
      const opts = data.options || {};
      // Fill any role <select> with the guild's roles before assigning values.
      if (optSchema.some(o => o.type === "role")) {
        const roles = await loadRoles();
        for (const opt of optSchema) {
          if (opt.type !== "role") continue;
          const el = container.querySelector('[data-opt="' + opt.key + '"]');
          if (el) el.innerHTML = roleSelect(roles, null, { allowNone: true });
        }
      }
      for (const opt of optSchema) {
        const el = container.querySelector('[data-opt="' + opt.key + '"]');
        if (!el) continue;
        const val = Object.prototype.hasOwnProperty.call(opts, opt.key) ? opts[opt.key] : opt.default;
        if (opt.type === "bool") el.checked = val !== false && val !== 0;
        else if (opt.type === "role") el.value = String(val || "0");
        else el.value = val ?? "";
      }
    } catch (err) {
      console.error("Failed to load game config:", err);
    }
  }

  if (hasStatus) {
    container.querySelector('[data-action="save-config"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="config"]');
      const options = {};
      for (const opt of optSchema) {
        const el = container.querySelector('[data-opt="' + opt.key + '"]');
        if (!el) continue;
        if (opt.type === "bool") options[opt.key] = el.checked;
        else if (opt.type === "text") options[opt.key] = el.value.trim();
        else if (opt.type === "role") options[opt.key] = el.value === "0" ? "" : el.value;
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
  }
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
  function makeTagWidget(initial, onChange) {
    const tags = [];
    const notify = () => { if (typeof onChange === "function") onChange(tags.slice()); };
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
        x.addEventListener("click", () => { tags.splice(i, 1); render(); notify(); });
        chip.appendChild(x);
        wrap.insertBefore(chip, input);
      });
    }
    function commit(val) {
      val = (val || "").trim().replace(/,+$/, "").trim();
      const added = val && !tags.includes(val);
      if (added) tags.push(val);
      input.value = "";
      render();
      if (added) notify();
    }
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(input.value); }
      else if (e.key === "Backspace" && !input.value && tags.length) { tags.pop(); render(); notify(); }
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

  // A required single-category <select> with the same {el, getTags, setTags}
  // shape as makeTagWidget, so the rest of the bank code is mode-agnostic.
  // In filter context (includeAll) a leading "All" clears the filter; in
  // add/edit context the leading placeholder forces an explicit choice.
  function makeCategoryWidget(initial, onChange, includeAll) {
    const sel = document.createElement("select");
    sel.style.cssText = "border:1px solid var(--rule);border-radius:var(--r);padding:5px 6px;background:var(--bg);color:inherit;font-size:12px;min-width:140px;";
    sel.appendChild(new Option(includeAll ? "All categories" : "— choose category —", ""));
    for (const c of bankCategories) sel.appendChild(new Option(c.label, c.value));
    const init = parseTags(initial).find(t => bankCategories.some(c => c.value === t)) || "";
    sel.value = init;
    if (typeof onChange === "function") sel.addEventListener("change", () => onChange(sel.value ? [sel.value] : []));
    return {
      el: sel,
      getTags() { return sel.value ? [sel.value] : []; },
      setTags(arr) { sel.value = parseTags(arr).find(t => bankCategories.some(c => c.value === t)) || ""; },
    };
  }

  function makeChooser(initial, onChange, includeAll) {
    return catMode ? makeCategoryWidget(initial, onChange, includeAll) : makeTagWidget(initial, onChange);
  }

  function buildBankHtml() {
    const tagWord = catMode ? "Category" : "Tags";
    const defaultHint = "Questions used by this game. Tag content to organize it; the reserved <strong>nsfw</strong> tag marks adult content, which games include by default.";
    const hint = bankHint || defaultHint;
    // The multi-tag "Match: All/Any" control is meaningless with a single
    // required category, so it's dropped in category mode.
    const matchHtml = catMode ? "" :
      '<div class="field m-0"><label>Match<select data-ctrl="filter-match" style="width:80px;"><option value="all">All</option><option value="any">Any</option></select></label></div>';
    return '<section>' +
      '<div class="section-label">Question Bank</div>' +
      '<div class="field-hint" style="margin-bottom:12px;">' + hint + "</div>" +
      '<datalist data-ctrl="tags-datalist"></datalist>' +
      '<div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;">' +
      '<div style="flex:1;min-width:260px;">' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px;">' +
      '<div class="field" style="margin:0;min-width:180px;"><label>' + tagWord + '<div data-ctrl="filter-tags"></div></label></div>' +
      matchHtml +
      '<div class="field" style="margin:0;flex:1;min-width:160px;"><label>Search<input class="w-full" type="text" data-ctrl="search" placeholder="Filter..." /></label></div>' +
      '<button class="btn" data-action="search-btn">Search</button>' +
      "</div>" +
      '<div data-region="bank-list">' + renderLoading("Loading...") + "</div>" +
      '<div data-region="bank-pagination" style="display:flex;gap:6px;align-items:center;margin-top:8px;flex-wrap:wrap;"></div>' +
      "</div>" +
      '<div style="width:300px;flex-shrink:0;">' +
      '<section style="background:var(--bg);border:1px solid var(--rule);border-radius:var(--r);padding:16px;">' +
      '<div class="section-label mb-10">Add Question</div>' +
      '<div class="field"><label>' + tagWord + '<div data-ctrl="add-tags"></div></label></div>' +
      '<div class="field"><label>Question<textarea class="w-full" data-ctrl="add-text" rows="3"></textarea></label></div>' +
      '<div class="row-8"><button class="btn btn-primary" data-action="add-question">Add</button><span data-status="add" class="save-status"></span></div>' +
      "</section>" +
      '<section style="background:var(--bg-card);border:1px solid var(--rule);border-radius:var(--r);padding:16px;margin-top:12px;">' +
      '<div class="section-label mb-10">Bulk Import</div>' +
      '<div class="field"><label>' + tagWord + ' (applied to all)<div data-ctrl="bulk-tags"></div></label></div>' +
      '<div class="field"><label>Lines (one per line)<textarea data-ctrl="bulk-text" rows="6" style="width:100%;font-size:12px;"></textarea></label></div>' +
      '<div class="row-8"><button class="btn btn-primary" data-action="bulk-import">Import</button><span data-status="bulk" class="save-status"></span></div>' +
      "</section>" +
      '<section style="background:var(--bg-card);border:1px solid var(--rule);border-radius:var(--r);padding:16px;margin-top:12px;">' +
      '<div class="section-label mb-10">Global Pool</div>' +
      '<div class="field-hint" style="margin-bottom:10px;">A question pool shared by every game. Each question’s <strong>Pool</strong> button copies it there; browse the pool to import questions into this bank.</div>' +
      '<button class="btn" data-action="toggle-pool">Browse pool</button>' +
      "</section></div></div>" +
      buildPoolBrowserHtml() +
      "</section>";
  }

  function buildPoolBrowserHtml() {
    const importHint = catMode
      ? "Tick questions, choose the category to file them under, then import. Duplicates already in this bank are skipped."
      : "Tick questions to import; their pool tags carry over. Duplicates already in this bank are skipped.";
    return '<div data-region="pool-browser" style="display:none;margin-top:16px;border:1px solid var(--rule);border-radius:var(--r);padding:16px;background:var(--bg-card);">' +
      '<div class="section-label mb-10">Global Pool</div>' +
      '<div class="field-hint" style="margin-bottom:10px;">' + importHint + "</div>" +
      '<div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-bottom:10px;">' +
      '<div class="field m-0" style="flex:1;min-width:160px;"><label>Search<input class="w-full" type="text" data-ctrl="pool-search" placeholder="Filter..." /></label></div>' +
      '<button class="btn" data-action="pool-search-btn">Search</button>' +
      (catMode ? '<div class="field m-0"><label>Import as<div data-ctrl="pool-cat"></div></label></div>' : "") +
      '<button class="btn btn-primary" data-action="pool-import">Import selected</button>' +
      '<span data-status="pool" class="save-status"></span>' +
      "</div>" +
      '<div data-region="pool-list">' + renderLoading("Loading...") + "</div>" +
      "</div>";
  }

  function initBank() {
    let currentPage = 1;
    let currentTags = [];
    let currentSearch = "";
    let poolLoaded = false;

    // Mount the multi-tag list filter; adding/removing a chip re-runs the
    // search immediately (no need to press Search).
    const filterTags = makeChooser([], (tags) => {
      currentTags = tags;
      currentPage = 1;
      loadBank();
    }, /* includeAll */ true);
    ctrl("filter-tags").appendChild(filterTags.el);

    // Mount the persistent add/bulk tag widgets.
    const addTags = makeChooser([]);
    ctrl("add-tags").appendChild(addTags.el);
    const bulkTags = makeChooser([]);
    ctrl("bulk-tags").appendChild(bulkTags.el);

    async function loadBank() {
      const el = region("bank-list");
      el.innerHTML = renderLoading("Loading...");
      const params = new URLSearchParams({ game_type: gameType, page: currentPage, per_page: 50 });
      currentTags.forEach(t => params.append("tag", t));
      if (currentTags.length > 1) params.set("match", ctrl("filter-match").value);
      if (currentSearch) params.set("search", currentSearch);
      try {
        const data = await api("/api/games/bank?" + params);
        const qs = data.questions || [];
        if (!qs.length) {
          el.innerHTML = renderEmpty("No questions found.");
          region("bank-pagination").innerHTML = "";
          return;
        }
        let rows = "";
        for (const q of qs) {
          const chips = parseTags(q.tags).map(t => '<span class="ll-tag">' + esc(catMode ? catLabel(t) : t) + "</span>").join(" ");
          // In category mode chips show the label, so stash the raw category
          // value on the row for the inline editor to restore.
          const catAttr = catMode ? ' data-cat="' + esc(parseTags(q.tags)[0] || "") + '"' : "";
          rows += '<tr data-qid="' + q.question_id + '"' + catAttr + '>' +
            '<td class="bank-tags-cell" style="width:160px;"><div class="ll-tags">' + chips + "</div></td>" +
            '<td class="bank-text-cell" style="padding-right:8px;">' + esc(q.question_text) + "</td>" +
            '<td style="width:170px;white-space:nowrap;">' +
            '<button class="btn" style="padding:2px 6px;font-size:12px;margin-right:4px;" data-action="edit-q" data-qid="' + q.question_id + '">Edit</button>' +
            '<button class="btn" style="padding:2px 6px;font-size:12px;margin-right:4px;" data-action="pool-q" data-qid="' + q.question_id + '" title="Copy to the global pool">Pool</button>' +
            '<button class="btn" style="padding:2px 6px;font-size:12px;" data-action="del-q" data-qid="' + q.question_id + '">Del</button>' +
            "</td></tr>";
        }
        el.innerHTML = '<table style="width:100%;border-collapse:collapse;" class="data-table">' +
          '<thead><tr><th style="width:160px;">Tags</th><th>Question</th><th style="width:170px;"></th></tr></thead>' +
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
        el.innerHTML = renderError(err);
      }
    }

    // Delegated edit/save/delete handler — attached once; loadBank() only
    // replaces the list's innerHTML, so re-attaching there would stack
    // handlers. Edit is handled here too (rather than as a per-row direct
    // listener) so a single click can't both open the editor AND bubble into
    // the save branch after the button flips to data-action="save-q".
    region("bank-list").addEventListener("click", async (e) => {
      const btn = e.target.closest('[data-action="edit-q"],[data-action="save-q"],[data-action="del-q"],[data-action="pool-q"]');
      if (!btn) return;
      const qid = btn.dataset.qid;
      const list = region("bank-list");
      const row = list.querySelector('tr[data-qid="' + qid + '"]');
      if (btn.dataset.action === "edit-q") {
        const textCell = row.querySelector(".bank-text-cell");
        const tagsCell = row.querySelector(".bank-tags-cell");
        const origText = textCell.textContent;
        const origTags = catMode
          ? (row.dataset.cat ? [row.dataset.cat] : [])
          : Array.from(tagsCell.querySelectorAll(".ll-tag")).map(s => s.textContent.trim());
        textCell.innerHTML = '<textarea class="field-input" style="width:100%;min-height:60px;">' + esc(origText) + "</textarea>";
        const widget = makeChooser(origTags);
        tagsCell.innerHTML = "";
        tagsCell.appendChild(widget.el);
        row._tagWidget = widget;
        btn.textContent = "Save";
        btn.dataset.action = "save-q";
      } else if (btn.dataset.action === "save-q") {
        const newText = row.querySelector("textarea") && row.querySelector("textarea").value.trim();
        const newTags = row._tagWidget ? row._tagWidget.getTags() : [];
        if (!newText) return;
        if (catMode && !newTags.length) { toast("Choose a category.", "error"); return; }
        try { await apiPut("/api/games/bank/" + qid, { question_text: newText, tags: newTags }); await loadTags(); loadBank(); }
        catch (err) { toast("Save failed: " + err.message, "error"); }
      } else if (btn.dataset.action === "pool-q") {
        btn.disabled = true;
        try {
          const res = await apiPost("/api/games/bank/" + qid + "/pool", {});
          toast(res.duplicate ? "Already in the global pool." : "Copied to the global pool.", res.duplicate ? "info" : "");
          if (res.sent && poolLoaded) loadPool();
        } catch (err) { toast("Send failed: " + err.message, "error"); }
        btn.disabled = false;
      } else if (btn.dataset.action === "del-q") {
        if (!(await confirmDialog("Delete this question?", { danger: true, confirmLabel: "Delete" }))) return;
        try { await apiDelete("/api/games/bank/" + qid); loadBank(); }
        catch (err) { toast("Delete failed: " + err.message, "error"); }
      }
    });

    if (ctrl("filter-match")) ctrl("filter-match").addEventListener("change", () => { if (currentTags.length > 1) { currentPage = 1; loadBank(); } });
    ctrl("search").addEventListener("keydown", e => {
      if (e.key === "Enter") { currentSearch = ctrl("search").value.trim(); currentPage = 1; loadBank(); }
    });
    container.querySelector('[data-action="search-btn"]').addEventListener("click", () => {
      currentSearch = ctrl("search").value.trim();
      currentTags = filterTags.getTags();
      currentPage = 1; loadBank();
    });
    container.querySelector('[data-action="add-question"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="add"]');
      const text = ctrl("add-text").value.trim();
      if (!text) { showStatus(st, false, "Question text required"); return; }
      const addTagList = addTags.getTags();
      if (catMode && !addTagList.length) { showStatus(st, false, "Choose a category"); return; }
      try {
        await apiPost("/api/games/bank", { game_type: gameType, tags: addTagList, question_text: text });
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
      const bulkTagList = bulkTags.getTags();
      if (catMode && !bulkTagList.length) { showStatus(st, false, "Choose a category"); return; }
      try {
        const res = await apiPost("/api/games/bank/bulk", { game_type: gameType, tags: bulkTagList, lines });
        ctrl("bulk-text").value = "";
        showStatus(st, true, "Imported " + res.added);
        await loadTags();
        loadBank();
      } catch (err) { showStatus(st, false, err.message); }
    });

    // ── Global pool browser ────────────────────────────────────────────────
    // Lazy: the pool list is only fetched the first time the browser opens.
    const poolCat = catMode ? makeCategoryWidget([]) : null;
    if (poolCat) ctrl("pool-cat").appendChild(poolCat.el);

    container.querySelector('[data-action="toggle-pool"]').addEventListener("click", (e) => {
      const reg = region("pool-browser");
      const open = reg.style.display !== "none";
      reg.style.display = open ? "none" : "";
      e.target.textContent = open ? "Browse pool" : "Hide pool";
      if (!open && !poolLoaded) { poolLoaded = true; loadPool(); }
    });

    async function loadPool() {
      const el = region("pool-list");
      el.innerHTML = renderLoading("Loading...");
      const params = new URLSearchParams({ game_type: "global", per_page: 200 });
      const search = ctrl("pool-search").value.trim();
      if (search) params.set("search", search);
      try {
        const data = await api("/api/games/bank?" + params);
        const qs = data.questions || [];
        if (!qs.length) {
          el.innerHTML = renderEmpty("The global pool is empty. Use a question's Pool button in any game's bank to add to it.");
          return;
        }
        let rows = "";
        for (const q of qs) {
          const chips = parseTags(q.tags).map(t => '<span class="ll-tag">' + esc(t) + "</span>").join(" ");
          rows += "<tr>" +
            '<td style="width:28px;"><input type="checkbox" data-pool-check="' + q.question_id + '" /></td>' +
            '<td style="width:160px;"><div class="ll-tags">' + chips + "</div></td>" +
            '<td style="padding-right:8px;">' + esc(q.question_text) + "</td>" +
            '<td style="width:50px;white-space:nowrap;"><button class="btn" style="padding:2px 6px;font-size:12px;" data-action="pool-del" data-qid="' + q.question_id + '">Del</button></td>' +
            "</tr>";
        }
        const note = data.total > qs.length ? " — showing the first " + qs.length + ", refine your search" : "";
        el.innerHTML = '<table style="width:100%;border-collapse:collapse;" class="data-table">' +
          '<thead><tr><th style="width:28px;"><input type="checkbox" data-ctrl="pool-select-all" /></th><th style="width:160px;">Tags</th><th>Question</th><th style="width:50px;"></th></tr></thead>' +
          "<tbody>" + rows + "</tbody></table>" +
          '<div style="font-size:12px;color:var(--ink-muted);margin-top:6px;">' + data.total + " question" + (data.total !== 1 ? "s" : "") + " in the pool" + note + "</div>";
        el.querySelector('[data-ctrl="pool-select-all"]').addEventListener("change", (e) => {
          el.querySelectorAll("[data-pool-check]").forEach(c => { c.checked = e.target.checked; });
        });
      } catch (err) {
        el.innerHTML = renderError(err);
      }
    }

    // Delegated Del handler — loadPool() replaces the list's innerHTML, so a
    // per-render listener would stack (same reasoning as the bank list above).
    region("pool-browser").addEventListener("click", async (e) => {
      const btn = e.target.closest('[data-action="pool-del"]');
      if (!btn) return;
      if (!(await confirmDialog("Remove this question from the global pool?", { danger: true, confirmLabel: "Remove" }))) return;
      try { await apiDelete("/api/games/bank/" + btn.dataset.qid); loadPool(); }
      catch (err) { toast("Remove failed: " + err.message, "error"); }
    });

    container.querySelector('[data-action="pool-search-btn"]').addEventListener("click", () => loadPool());
    ctrl("pool-search").addEventListener("keydown", e => {
      if (e.key === "Enter") loadPool();
    });

    container.querySelector('[data-action="pool-import"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="pool"]');
      const ids = Array.from(region("pool-list").querySelectorAll("[data-pool-check]:checked"))
        .map(c => parseInt(c.dataset.poolCheck, 10));
      if (!ids.length) { showStatus(st, false, "Select questions first"); return; }
      const body = { game_type: gameType, question_ids: ids };
      if (catMode) {
        const cat = poolCat.getTags();
        if (!cat.length) { showStatus(st, false, "Choose a category"); return; }
        body.tags = cat;
      }
      try {
        const res = await apiPost("/api/games/bank/pool/import", body);
        showStatus(st, true, "Imported " + res.imported + (res.skipped ? " (" + res.skipped + " already in bank)" : ""));
        region("pool-list").querySelectorAll("[data-pool-check]:checked").forEach(c => { c.checked = false; });
        await loadTags();
        loadBank();
      } catch (err) { showStatus(st, false, err.message); }
    });

    loadTags();
    loadBank();
  }
}
