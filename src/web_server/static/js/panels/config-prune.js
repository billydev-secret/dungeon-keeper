import {
  loadConfig, loadRoles, loadMembers, apiPut, apiDelete, showStatus,
  guardForm, renderMetaWarning, mountRolePicker, mountPicker,
} from "../config-helpers.js";
import { esc, apiPost } from "../api.js";
import { toast, confirmDialog } from "../ui.js";

function fmtTs(ts) {
  if (!ts) return "Never";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading inactivity settings…</div></div>`;

  (async () => {
    const [config, roles, members] = await Promise.all([
      loadConfig(),
      loadRoles(),
      loadMembers(),
    ]);
    const p = config.prune;

    // Member options for the shared searchable picker: members who have left
    // sort last, and say so in their label.
    const memberOpts = members
      .map((m) => ({
        id: String(m.id),
        label: m.display_name && m.display_name !== m.name
          ? `${m.display_name} (${m.name})`
          : m.name,
        left: !!m.left_server,
      }))
      .sort((a, b) => a.left - b.left || a.label.localeCompare(b.label))
      .map((o) => (o.left ? { ...o, label: `${o.label} (left the server)` } : o));

    // Local mutable state
    let exemptions = (p.exemptions || []).slice();

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Auto-Remove Role (Inactive)</h2>
          <div class="subtitle">Take a role back from members who stop posting. Pairs with the Inactive Role Members report.</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">What Gets Removed</div>
            <div class="field">
              <label>Role to Take Away</label>
              <span data-picker="role_id"></span>
              <div class="field-hint">Members lose this role once they've been quiet for the number of days below. Choose "(none)" and nothing is ever removed.</div>
            </div>
            <div class="field">
              <label for="cp-days">Days of Silence Before Removal</label>
              <input type="number" name="inactivity_days" id="cp-days" min="1" max="365" step="1" value="${p.inactivity_days || ""}" placeholder="30" style="max-width:140px;" />
              <div class="field-hint">Counted from a member's last message or voice activity. Between 1 and 365 days; leave blank to turn removal off.</div>
            </div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <div class="section-label">Members Who Are Never Removed</div>
        <div class="card">
          <div class="field-hint" style="margin-bottom:10px;">
            Anyone on this list keeps the role no matter how long they're away. Adding and
            removing here saves straight away — there's no Save button for this list.
          </div>
          <div class="field" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;">
            <div style="flex:1 1 240px;min-width:0;">
              <label>Member</label>
              <span data-picker="exempt_member"></span>
            </div>
            <button type="button" class="btn btn-primary" data-add-exempt>Add Member</button>
          </div>
          <div data-exempt-list></div>
        </div>

        <div class="section-label">
          <span>Who Would Be Removed Right Now</span>
          <button type="button" class="btn btn-sm" data-preview-btn>Check Now</button>
          <span data-preview-status style="color:var(--ink-dim); font-size:12px; text-transform:none; letter-spacing:0; font-weight:400;"></span>
        </div>
        <div class="card">
          <div class="field-hint" style="margin-bottom:10px;">
            A dry run against the settings above — nobody is touched until the scheduled sweep
            runs. Unsaved changes on this page are included, so you can test a threshold before
            committing to it.
          </div>
          <div data-preview></div>
        </div>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const exemptListEl = container.querySelector("[data-exempt-list]");
    const previewBtn = container.querySelector("[data-preview-btn]");
    const previewStatusEl = container.querySelector("[data-preview-status]");
    const previewEl = container.querySelector("[data-preview]");

    // Searchable pickers from the shared widget replace the hand-rolled member
    // search this panel used to carry (W-C4).
    const rolePicker = mountRolePicker(
      form.querySelector('[data-picker="role_id"]'), roles,
      String(p.role_id || "0"), { label: "Role to Take Away" },
    );
    const exemptPicker = mountPicker(
      container.querySelector('[data-picker="exempt_member"]'), memberOpts, "0",
      {
        emptyValue: "0",
        emptyLabel: "(pick a member)",
        placeholder: "Search members…",
        label: "Member to never remove",
        filter: (o) => !excludedIds().has(String(o.id)),
      },
    );

    guardForm(form);

    function excludedIds() {
      return new Set(exemptions.map((e) => String(e.id)));
    }

    function renderExemptions() {
      if (!exemptions.length) {
        exemptListEl.innerHTML = `<div class="empty" style="padding:8px 0;">Nobody is exempt — every holder of the role can be removed.</div>`;
        return;
      }
      exemptListEl.innerHTML = `<div class="exempt-chips">${exemptions
        .map(
          (e) =>
            `<span class="exempt-chip"><span>${esc(e.name)}</span><button type="button" data-remove-exempt="${esc(e.id)}" aria-label="Stop exempting ${esc(e.name)}" title="Stop exempting ${esc(e.name)}">×</button></span>`
        )
        .join("")}</div>`;
      exemptListEl.querySelectorAll("[data-remove-exempt]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const uid = btn.dataset.removeExempt;
          const entry = exemptions.find((e) => String(e.id) === String(uid));
          const ok = await confirmDialog(
            `Stop exempting ${entry ? entry.name : "this member"}? They will lose the role like everyone else once they go quiet for too long.`,
            { title: "Remove Exemption", danger: true, confirmLabel: "Remove Exemption" },
          );
          if (!ok) return;
          try {
            await apiDelete(`/api/config/prune/exemptions/${uid}`);
            exemptions = exemptions.filter((e) => String(e.id) !== String(uid));
            renderExemptions();
          } catch (err) {
            toast(err.message, "error");
          }
        });
      });
    }

    async function addExemption(id, label) {
      try {
        // Member ids stay strings on the wire.
        await apiPut(`/api/config/prune/exemptions/${id}`, {});
        // Strip the trailing " (username)" / " (left the server)" for display.
        const displayName = label.replace(/\s*\([^)]*\)\s*$/, "").trim() || label;
        exemptions.push({ id: String(id), name: displayName });
        exemptions.sort((a, b) => a.name.localeCompare(b.name));
        renderExemptions();
      } catch (err) {
        toast(err.message, "error");
      }
    }

    container.querySelector("[data-add-exempt]").addEventListener("click", async () => {
      const id = exemptPicker.getValue();
      if (!id || id === "0") {
        toast("Pick a member first.", "error");
        return;
      }
      const opt = memberOpts.find((o) => o.id === String(id));
      await addExemption(id, opt ? opt.label : String(id));
      exemptPicker.setValue("0");
      // Re-apply the filter so the member just added drops out of suggestions.
      exemptPicker.setFilter((o) => !excludedIds().has(String(o.id)));
    });

    renderExemptions();

    function readDays(statusEl) {
      const raw = String(new FormData(form).get("inactivity_days") ?? "").trim();
      if (raw === "") return 0; // blank means "not configured"
      const v = parseInt(raw, 10);
      if (!Number.isFinite(v) || v < 1 || v > 365) {
        showStatus(statusEl, false, "Days of Silence Before Removal must be a whole number between 1 and 365.");
        form.querySelector('[name="inactivity_days"]').focus();
        return null;
      }
      return v;
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const days = readDays(status);
      if (days === null) return;
      try {
        await apiPut("/api/config/prune", {
          role_id: rolePicker.getValue() || "0",
          inactivity_days: days,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    async function runPreview() {
      const roleId = rolePicker.getValue() || "0";
      const days = readDays(previewStatusEl);
      if (days === null) return;
      if (roleId === "0" || days <= 0) {
        previewEl.innerHTML = `<div class="empty">Pick a role and a number of days above, then check again.</div>`;
        previewStatusEl.textContent = "";
        return;
      }
      previewStatusEl.textContent = "Checking…";
      previewEl.innerHTML = "";
      try {
        const data = await apiPost("/api/config/prune/preview", {
          role_id: roleId,
          inactivity_days: days,
          exempt_user_ids: exemptions.map((e) => String(e.id)),
        });
        previewStatusEl.textContent = `${data.candidates.length} of ${data.role_member_count || 0} members would lose the role (${data.considered_count || 0} checked after exemptions).`;
        if (!data.candidates.length) {
          previewEl.innerHTML = `<div class="empty">Nobody would lose the role with these settings.</div>`;
          return;
        }
        previewEl.innerHTML = `
          <table class="prune-preview-table">
            <thead>
              <tr><th>Member</th><th style="text-align:right;">Days Quiet</th><th>Last Seen</th><th></th></tr>
            </thead>
            <tbody>
              ${data.candidates
                .map(
                  (c) => `
                    <tr>
                      <td>${esc(c.name)}</td>
                      <td style="text-align:right; font-variant-numeric: tabular-nums;">${esc(String(c.days_inactive))}</td>
                      <td style="color:var(--ink-dim);">${esc(fmtTs(c.last_activity_ts))}</td>
                      <td><button type="button" class="btn btn-sm" data-exempt-from-preview="${esc(c.id)}" data-exempt-name="${esc(c.name)}">Never Remove</button></td>
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
            exemptPicker.setFilter((o) => !excludedIds().has(String(o.id)));
            btn.closest("tr").remove();
          });
        });
      } catch (err) {
        previewStatusEl.textContent = "";
        previewEl.innerHTML = `<div class="error">Couldn't run the check: ${esc(err.message)}</div>`;
      }
    }

    previewBtn.addEventListener("click", runPreview);
  })();
}
