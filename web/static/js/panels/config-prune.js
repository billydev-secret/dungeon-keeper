import { loadConfig, loadRoles, roleSelect, apiPut, apiDelete, showStatus } from "../config-helpers.js";
import { api, esc } from "../api.js";

function fmtTs(ts) {
  if (!ts) return "never";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/**
 * Searchable member picker. Typing filters the member list; clicking an item
 * calls `onPick(id, label)`. Excluded ids (already in the exempt list) are
 * hidden from suggestions.
 */
function memberSearch(members, onPick, getExcludedIds) {
  const wrap = document.createElement("div");
  wrap.className = "filter-select";
  wrap.style.maxWidth = "360px";

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "Search members to exempt…";
  input.className = "filter-select-input";
  wrap.appendChild(input);

  const list = document.createElement("div");
  list.className = "filter-select-list";
  wrap.appendChild(list);

  function render(filter) {
    const lc = filter.toLowerCase().trim();
    const excluded = getExcludedIds();
    const matches = members.filter((m) => {
      if (excluded.has(m.id)) return false;
      if (!lc) return true;
      return m.label.toLowerCase().includes(lc);
    });
    const show = matches.slice(0, 80);
    if (!show.length) {
      list.innerHTML = `<div class="filter-select-item"><em style="color:var(--ink-dim)">No matches</em></div>`;
      return;
    }
    list.innerHTML = show
      .map((m) => `<div class="filter-select-item" data-id="${esc(m.id)}">${esc(m.label)}</div>`)
      .join("");
  }

  input.addEventListener("focus", () => {
    render(input.value);
    list.style.display = "block";
  });
  input.addEventListener("input", () => {
    render(input.value);
    list.style.display = "block";
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { list.style.display = "none"; input.blur(); }
  });
  input.addEventListener("blur", () => {
    setTimeout(() => { list.style.display = "none"; }, 150);
  });

  list.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".filter-select-item");
    if (!item || !item.dataset.id) return;
    const id = item.dataset.id;
    const label = item.textContent;
    input.value = "";
    list.style.display = "none";
    onPick(id, label);
  });

  return { el: wrap };
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading config…</div></div>`;

  (async () => {
    const [config, roles, members] = await Promise.all([
      loadConfig(),
      loadRoles(),
      api("/api/meta/members"),
    ]);
    const p = config.prune;

    // Build member options for the picker
    const memberOpts = members.map((m) => ({
      id: m.id,
      label: m.display_name !== m.name ? `${m.display_name} (${m.name})` : m.name,
      left: !!m.left_server,
    })).sort((a, b) => a.left - b.left || a.label.localeCompare(b.label));
    memberOpts.forEach((o) => { if (o.left) o.label += " (left)"; });

    // Local mutable state
    let exemptions = (p.exemptions || []).slice();

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Inactivity Prune</h2>
          <div class="subtitle">Automatically remove a role from inactive members</div>
        </header>
        <form class="form" data-form>
          <div class="field">
            <label>Role to Prune</label>
            <select name="role_id">${roleSelect(roles, p.role_id)}</select>
            <div class="field-hint">Set to (none) to disable pruning</div>
          </div>
          <div class="field">
            <label>Inactivity Threshold (days)</label>
            <input type="number" name="inactivity_days" min="1" max="365" value="${p.inactivity_days || ""}" placeholder="30" />
          </div>
          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>

        <div class="section-label">Exemptions</div>
        <div class="field-hint" style="margin-bottom:10px;">
          Members on this list are never pruned, even if they exceed the inactivity threshold.
        </div>
        <div data-exempt-picker style="margin-bottom:12px;"></div>
        <div data-exempt-list></div>

        <div class="section-label">
          <span>Preview</span>
          <button type="button" class="btn btn-sm" data-preview-btn>Refresh preview</button>
          <span data-preview-status style="color:var(--ink-dim); font-size:12px; text-transform:none; letter-spacing:0; font-weight:400;"></span>
        </div>
        <div class="field-hint" style="margin-bottom:10px;">
          Shows who would be removed <em>right now</em> using the settings above (including unsaved exemption changes).
        </div>
        <div data-preview></div>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const exemptListEl = container.querySelector("[data-exempt-list]");
    const exemptPickerEl = container.querySelector("[data-exempt-picker]");
    const previewBtn = container.querySelector("[data-preview-btn]");
    const previewStatusEl = container.querySelector("[data-preview-status]");
    const previewEl = container.querySelector("[data-preview]");

    function excludedIds() {
      return new Set(exemptions.map((e) => String(e.id)));
    }

    function renderExemptions() {
      if (!exemptions.length) {
        exemptListEl.innerHTML = `<div class="empty" style="padding:8px 0;">No exemptions.</div>`;
        return;
      }
      exemptListEl.innerHTML = `<div class="exempt-chips">${exemptions
        .map(
          (e) =>
            `<span class="exempt-chip"><span>${esc(e.name)}</span><button type="button" data-remove-exempt="${esc(e.id)}" title="Remove exemption">×</button></span>`
        )
        .join("")}</div>`;
      exemptListEl.querySelectorAll("[data-remove-exempt]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const uid = btn.dataset.removeExempt;
          try {
            await apiDelete(`/api/config/prune/exemptions/${uid}`);
            exemptions = exemptions.filter((e) => String(e.id) !== String(uid));
            renderExemptions();
          } catch (err) {
            alert(err.message);
          }
        });
      });
    }

    async function addExemption(id, label) {
      try {
        await apiPut(`/api/config/prune/exemptions/${id}`, {});
        // Strip " (username)" suffix if present for display
        const displayName = label.replace(/\s*\([^)]*\)\s*$/, "") || label;
        exemptions.push({ id: String(id), name: displayName });
        exemptions.sort((a, b) => a.name.localeCompare(b.name));
        renderExemptions();
      } catch (err) {
        alert(err.message);
      }
    }

    const picker = memberSearch(memberOpts, addExemption, excludedIds);
    exemptPickerEl.appendChild(picker.el);
    renderExemptions();

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/prune", {
          role_id: fd.get("role_id"),
          inactivity_days: parseInt(fd.get("inactivity_days")) || 0,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    async function runPreview() {
      const fd = new FormData(form);
      const roleId = fd.get("role_id");
      const days = parseInt(fd.get("inactivity_days")) || 0;
      if (!roleId || roleId === "0" || days <= 0) {
        previewEl.innerHTML = `<div class="empty">Set a role and threshold to preview.</div>`;
        previewStatusEl.textContent = "";
        return;
      }
      previewStatusEl.textContent = "Computing…";
      previewEl.innerHTML = "";
      try {
        const res = await fetch("/api/config/prune/preview", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            role_id: roleId,
            inactivity_days: days,
            exempt_user_ids: exemptions.map((e) => String(e.id)),
          }),
        });
        if (!res.ok) {
          let detail = res.statusText;
          try { const b = await res.json(); if (b.detail) detail = b.detail; } catch (_) {}
          throw new Error(`${res.status}: ${detail}`);
        }
        const data = await res.json();
        previewStatusEl.textContent = `${data.candidates.length} of ${data.role_member_count || 0} would be removed (${data.considered_count || 0} considered after exemptions).`;
        if (!data.candidates.length) {
          previewEl.innerHTML = `<div class="empty">No members would be removed.</div>`;
          return;
        }
        previewEl.innerHTML = `
          <table class="prune-preview-table">
            <thead>
              <tr><th>Member</th><th style="text-align:right;">Days inactive</th><th>Last activity</th><th></th></tr>
            </thead>
            <tbody>
              ${data.candidates
                .map(
                  (c) => `
                    <tr>
                      <td>${esc(c.name)}</td>
                      <td style="text-align:right; font-variant-numeric: tabular-nums;">${esc(String(c.days_inactive))}</td>
                      <td style="color:var(--ink-dim);">${esc(fmtTs(c.last_activity_ts))}</td>
                      <td><button type="button" class="btn btn-sm" data-exempt-from-preview="${esc(c.id)}" data-exempt-name="${esc(c.name)}">Exempt</button></td>
                    </tr>`
                )
                .join("")}
            </tbody>
          </table>`;
        previewEl.querySelectorAll("[data-exempt-from-preview]").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const uid = btn.dataset.exemptFromPreview;
            const name = btn.dataset.exemptName;
            await addExemption(uid, name);
            btn.closest("tr").remove();
          });
        });
      } catch (err) {
        previewStatusEl.textContent = "";
        previewEl.innerHTML = `<div class="error">${esc(err.message)}</div>`;
      }
    }

    previewBtn.addEventListener("click", runPreview);
  })();
}
