import { api, apiPost, esc } from "../api.js";
import { apiPut, apiDelete, showStatus } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

export function mountGamePanel(container, { gameType, gameName, gameIcon, hasBank = false, optSchema = [] }) {
  function ctrl(name) { return container.querySelector('[data-ctrl="' + name + '"]'); }
  function region(name) { return container.querySelector('[data-region="' + name + '"]'); }

  const optFieldsHtml = optSchema.map(opt => {
    if (opt.type === "bool") {
      return '<div class="field" style="margin-bottom:8px;"><label style="display:flex;align-items:center;gap:8px;cursor:pointer;">' +
        '<input type="checkbox" data-opt="' + esc(opt.key) + '" style="width:16px;height:16px;cursor:pointer;" />' +
        "<span>" + esc(opt.label) + "</span></label></div>";
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

  function buildBankHtml() {
    return '<section>' +
      '<div class="section-label">Question Bank</div>' +
      '<div class="field-hint" style="margin-bottom:12px;">Questions used by this game. SFW and NSFW are managed separately.</div>' +
      '<div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;">' +
      '<div style="flex:1;min-width:0;">' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px;">' +
      '<div class="field" style="margin:0;"><label>Category<select data-ctrl="filter-cat"><option value="">All</option><option value="sfw">SFW</option><option value="nsfw">NSFW</option></select></label></div>' +
      '<div class="field" style="margin:0;flex:1;min-width:160px;"><label>Search<input type="text" data-ctrl="search" placeholder="Filter..." style="width:100%;" /></label></div>' +
      '<button class="btn" data-action="search-btn">Search</button>' +
      "</div>" +
      '<div data-region="bank-list"><div class="empty">Loading...</div></div>' +
      '<div data-region="bank-pagination" style="display:flex;gap:6px;align-items:center;margin-top:8px;flex-wrap:wrap;"></div>' +
      "</div>" +
      '<div style="width:300px;flex-shrink:0;">' +
      '<section style="background:var(--bg);border:1px solid var(--rule);border-radius:var(--r);padding:16px;">' +
      '<div class="section-label" style="margin-bottom:10px;">Add Question</div>' +
      '<div class="field"><label>Category<select data-ctrl="add-cat"><option value="sfw">SFW</option><option value="nsfw">NSFW</option></select></label></div>' +
      '<div class="field"><label>Question<textarea data-ctrl="add-text" rows="3" style="width:100%;"></textarea></label></div>' +
      '<div style="display:flex;gap:8px;align-items:center;"><button class="btn btn-primary" data-action="add-question">Add</button><span data-status="add" class="save-status"></span></div>' +
      "</section>" +
      '<section style="background:var(--bg-card);border:1px solid var(--rule);border-radius:var(--r);padding:16px;margin-top:12px;">' +
      '<div class="section-label" style="margin-bottom:10px;">Bulk Import</div>' +
      '<div class="field"><label>Category<select data-ctrl="bulk-cat"><option value="sfw">SFW</option><option value="nsfw">NSFW</option></select></label></div>' +
      '<div class="field"><label>Lines (one per line)<textarea data-ctrl="bulk-text" rows="6" style="width:100%;font-size:12px;"></textarea></label></div>' +
      '<div style="display:flex;gap:8px;align-items:center;"><button class="btn btn-primary" data-action="bulk-import">Import</button><span data-status="bulk" class="save-status"></span></div>' +
      "</section></div></div></section>";
  }

  function initBank() {
    let currentPage = 1;
    let currentCat = "";
    let currentSearch = "";

    async function loadBank() {
      const el = region("bank-list");
      el.innerHTML = '<div class="empty">Loading...</div>';
      const params = new URLSearchParams({ game_type: gameType, page: currentPage, per_page: 50 });
      if (currentCat) params.set("category", currentCat);
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
          const badge = q.category === "nsfw"
            ? '<span style="color:#e87070;font-size:11px;font-weight:600;">NSFW</span>'
            : '<span style="color:#70c870;font-size:11px;font-weight:600;">SFW</span>';
          rows += '<tr data-qid="' + q.question_id + '">' +
            '<td style="width:48px;">' + badge + "</td>" +
            '<td class="bank-text-cell" style="padding-right:8px;">' + esc(q.question_text) + "</td>" +
            '<td style="width:120px;white-space:nowrap;">' +
            '<button class="btn" style="padding:2px 6px;font-size:12px;margin-right:4px;" data-action="edit-q" data-qid="' + q.question_id + '">Edit</button>' +
            '<button class="btn" style="padding:2px 6px;font-size:12px;" data-action="del-q" data-qid="' + q.question_id + '">Del</button>' +
            "</td></tr>";
        }
        el.innerHTML = '<table style="width:100%;border-collapse:collapse;" class="data-table">' +
          '<thead><tr><th style="width:48px;">Cat</th><th>Question</th><th style="width:120px;"></th></tr></thead>' +
          "<tbody>" + rows + "</tbody></table>";

        el.querySelectorAll('[data-action="edit-q"]').forEach(btn => {
          btn.addEventListener("click", () => {
            const qid = btn.dataset.qid;
            const row = el.querySelector('tr[data-qid="' + qid + '"]');
            const textCell = row.querySelector(".bank-text-cell");
            const catCell = row.querySelector("td:first-child");
            const origText = textCell.textContent;
            const origCat = catCell.querySelector("span").textContent.toLowerCase().trim();
            textCell.innerHTML = '<textarea class="field-input" style="width:100%;min-height:60px;">' + esc(origText) + "</textarea>";
            catCell.innerHTML = '<select class="field-input" style="width:60px;font-size:12px;">' +
              '<option value="sfw"' + (origCat === "sfw" ? " selected" : "") + ">SFW</option>" +
              '<option value="nsfw"' + (origCat === "nsfw" ? " selected" : "") + ">NSFW</option></select>";
            btn.textContent = "Save";
            btn.dataset.action = "save-q";
          });
        });

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

    // Delegated save/delete handler — attached once; loadBank() only replaces
    // the list's innerHTML, so re-attaching there would stack handlers.
    region("bank-list").addEventListener("click", async (e) => {
      const btn = e.target.closest('[data-action="save-q"],[data-action="del-q"]');
      if (!btn) return;
      const qid = btn.dataset.qid;
      const list = region("bank-list");
      if (btn.dataset.action === "save-q") {
        const row = list.querySelector('tr[data-qid="' + qid + '"]');
        const newText = row.querySelector("textarea") && row.querySelector("textarea").value.trim();
        const newCat = row.querySelector("select") && row.querySelector("select").value;
        if (!newText) return;
        try { await apiPut("/api/games/bank/" + qid, { question_text: newText, category: newCat }); loadBank(); }
        catch (err) { toast("Save failed: " + err.message, "error"); }
      } else if (btn.dataset.action === "del-q") {
        if (!(await confirmDialog("Delete this question?", { danger: true, confirmLabel: "Delete" }))) return;
        try { await apiDelete("/api/games/bank/" + qid); loadBank(); }
        catch (err) { toast("Delete failed: " + err.message, "error"); }
      }
    });

    ctrl("filter-cat").addEventListener("change", () => { currentCat = ctrl("filter-cat").value; currentPage = 1; loadBank(); });
    ctrl("search").addEventListener("keydown", e => {
      if (e.key === "Enter") { currentSearch = ctrl("search").value.trim(); currentPage = 1; loadBank(); }
    });
    container.querySelector('[data-action="search-btn"]').addEventListener("click", () => {
      currentSearch = ctrl("search").value.trim(); currentPage = 1; loadBank();
    });
    container.querySelector('[data-action="add-question"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="add"]');
      const text = ctrl("add-text").value.trim();
      const cat = ctrl("add-cat").value;
      if (!text) { showStatus(st, false, "Question text required"); return; }
      try {
        await apiPost("/api/games/bank", { game_type: gameType, category: cat, question_text: text });
        ctrl("add-text").value = "";
        showStatus(st, true, "Added");
        loadBank();
      } catch (err) { showStatus(st, false, err.message); }
    });
    container.querySelector('[data-action="bulk-import"]').addEventListener("click", async () => {
      const st = container.querySelector('[data-status="bulk"]');
      const raw = ctrl("bulk-text").value;
      const lines = raw.split("\n").map(l => l.trim()).filter(Boolean);
      if (!lines.length) { showStatus(st, false, "No lines to import"); return; }
      const cat = ctrl("bulk-cat").value;
      try {
        const res = await apiPost("/api/games/bank/bulk", { game_type: gameType, category: cat, lines });
        ctrl("bulk-text").value = "";
        showStatus(st, true, "Imported " + res.added);
        loadBank();
      } catch (err) { showStatus(st, false, err.message); }
    });
    loadBank();
  }
}
