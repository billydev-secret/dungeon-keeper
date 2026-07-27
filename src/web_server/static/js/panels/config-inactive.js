import {
  loadConfig, loadMembers, apiPut, apiDelete, showStatus, guardForm, mountPicker,
} from "../config-helpers.js";
import { esc, apiPost } from "../api.js";
import { toast, confirmDialog } from "../ui.js";

function fmtTs(ts) {
  if (!ts) return "Never";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, members] = await Promise.all([loadConfig(), loadMembers()]);
    const cfg = config.inactive;

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

    let exemptions = (cfg.exemptions || []).slice();

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Inactive Sweep</h2>
          <div class="subtitle">Moves members who have gone quiet into the holding area, so your active roster stays honest</div>
        </header>
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Who Counts as Inactive</div>
            <div class="field">
              <label for="ci-threshold">Inactivity Threshold (days)</label>
              <input type="number" name="threshold_days" id="ci-threshold" required
                min="1" max="3650" step="1" value="${cfg.threshold_days}" style="max-width:140px;" />
              <div class="field-hint">A member who has not posted for this many days
                becomes eligible to be swept. Shorter thresholds catch more people —
                30 days is the usual starting point.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Sweeping</div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="auto_sweep" ${cfg.auto_sweep ? "checked" : ""} />
                Sweep inactive members automatically
              </label>
              <div class="field-hint">When checked, the sweep runs by itself every six
                hours and moves eligible members without anyone pressing a button. It
                does nothing until an inactive channel has been set from the
                <code>/inactive</code> panel in Discord. Unchecked, sweeps only happen
                when a moderator starts one.</div>
            </div>
            <div class="field">
              <label for="ci-cap">Members Moved Per Run</label>
              <input type="number" name="sweep_cap" id="ci-cap" required
                min="1" max="200" step="1" value="${cfg.sweep_cap}" style="max-width:140px;" />
              <div class="field-hint">The most members a single sweep will move,
                whether it was started by hand or ran automatically. A low cap keeps a
                first sweep from surprising half the server at once.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <div class="section-label">Never Sweep These Members</div>
        <div class="card">
          <div class="field-hint" style="margin-bottom:10px;">
            Exempt members are skipped by every sweep, and <code>/inactive mark</code>
            refuses them too — so an exemption can't be undone by accident from Discord.
            Bots, the owner, admins and moderators are already protected and don't need
            listing here.
          </div>
          <div class="field" style="display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap;">
            <div data-picker="exempt_member" style="min-width:240px;"></div>
            <button type="button" class="btn" data-add-exempt>Add Exemption</button>
          </div>
          <div data-exempt-list></div>
        </div>

        <div class="section-label">
          <span>Who Would Be Swept Right Now</span>
          <button type="button" class="btn btn-sm" data-preview-btn>Check Now</button>
          <span data-preview-status style="color:var(--ink-dim); font-size:12px; text-transform:none; letter-spacing:0; font-weight:400;"></span>
        </div>
        <div class="card">
          <div class="field-hint" style="margin-bottom:10px;">
            A dry run — nobody is moved and no roles change until a sweep actually runs.
            The threshold typed above is used even if you haven't saved it, so you can
            try a number before committing to it.
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

    const exemptPicker = mountPicker(
      container.querySelector('[data-picker="exempt_member"]'), memberOpts, "0",
      {
        emptyValue: "0",
        emptyLabel: "(pick a member)",
        placeholder: "Search members…",
        label: "Member to never sweep",
        filter: (o) => !excludedIds().has(String(o.id)),
      },
    );

    guardForm(form);

    function excludedIds() {
      return new Set(exemptions.map((e) => String(e.id)));
    }

    function renderExemptions() {
      if (!exemptions.length) {
        exemptListEl.innerHTML = `<div class="empty" style="padding:8px 0;">Nobody is exempt — every member who goes quiet can be swept.</div>`;
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
            `Stop exempting ${entry ? entry.name : "this member"}? They will be swept like everyone else once they go quiet for too long.`,
            { title: "Remove Exemption", danger: true, confirmLabel: "Remove Exemption" },
          );
          if (!ok) return;
          try {
            await apiDelete(`/api/config/inactive/exemptions/${uid}`);
            exemptions = exemptions.filter((e) => String(e.id) !== String(uid));
            renderExemptions();
            exemptPicker.setFilter((o) => !excludedIds().has(String(o.id)));
          } catch (err) {
            toast(err.message, "error");
          }
        });
      });
    }

    async function addExemption(id, label) {
      try {
        // Member ids stay strings on the wire.
        await apiPut(`/api/config/inactive/exemptions/${id}`, {});
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
      exemptPicker.setFilter((o) => !excludedIds().has(String(o.id)));
    });

    renderExemptions();

    function readThreshold(statusEl) {
      const v = parseInt(String(new FormData(form).get("threshold_days") ?? "").trim(), 10);
      if (!Number.isFinite(v) || v < 1 || v > 3650) {
        showStatus(statusEl, false, "Inactivity Threshold must be a number of days from 1 to 3650");
        form.querySelector("[name=threshold_days]").focus();
        return null;
      }
      return v;
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const threshold = readThreshold(status);
      if (threshold === null) return;
      const cap = parseInt(new FormData(form).get("sweep_cap"), 10);
      if (!Number.isFinite(cap) || cap < 1 || cap > 200) {
        showStatus(status, false, "Members Moved Per Run must be a number from 1 to 200");
        form.querySelector("[name=sweep_cap]").focus();
        return;
      }
      try {
        await apiPut("/api/config/inactive", {
          threshold_days: threshold,
          auto_sweep: form.querySelector('input[name="auto_sweep"]').checked,
          sweep_cap: cap,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    function lastSeenCell(row) {
      // A member with no tracked messages was aged from their join date alone —
      // say so rather than showing a date that reads like a last post.
      if (!row.has_tracked_messages) {
        return `<span style="color:var(--ink-dim);">Joined ${esc(fmtTs(row.last_seen_ts))} — no tracked messages</span>`;
      }
      return `<span style="color:var(--ink-dim);">${esc(fmtTs(row.last_seen_ts))}</span>`;
    }

    function rolesCell(row, idx, prefix) {
      if (!row.removed_role_count) {
        const kept = row.kept_managed_role_names.length
          ? " (only integration-managed roles, which are never stripped)"
          : "";
        return `<span style="color:var(--ink-dim);">No roles${esc(kept)}</span>`;
      }
      const label = row.removed_role_count === 1 ? "1 role" : `${row.removed_role_count} roles`;
      return `<button type="button" class="roles-toggle" data-toggle-roles="${prefix}-${idx}"
        aria-expanded="false" aria-controls="${prefix}-roles-${idx}">${esc(label)} ▸</button>`;
    }

    function detailRow(row, idx, prefix, colspan) {
      if (!row.removed_role_count) return "";
      const kept = row.kept_managed_role_names.length
        ? `<div style="margin-top:4px; color:var(--ink-dim);">Kept (managed by an integration): ${row.kept_managed_role_names.map(esc).join(", ")}</div>`
        : "";
      return `<tr class="sweep-roles-detail" id="${prefix}-roles-${idx}" hidden>
        <td colspan="${colspan}">
          <div>Would lose: ${row.removed_role_names.map(esc).join(", ")}</div>
          ${kept}
        </td></tr>`;
    }

    function wireRoleToggles(root) {
      root.querySelectorAll("[data-toggle-roles]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const detail = root.querySelector(`#${btn.getAttribute("aria-controls")}`);
          if (!detail) return;
          const open = detail.hasAttribute("hidden");
          if (open) detail.removeAttribute("hidden");
          else detail.setAttribute("hidden", "");
          btn.setAttribute("aria-expanded", open ? "true" : "false");
          btn.textContent = btn.textContent.replace(open ? "▸" : "▾", open ? "▾" : "▸");
        });
      });
    }

    async function runPreview() {
      const threshold = readThreshold(previewStatusEl);
      if (threshold === null) return;
      previewStatusEl.textContent = "Checking…";
      previewEl.innerHTML = "";
      let data;
      try {
        data = await apiPost("/api/config/inactive/preview", { threshold_days: threshold });
      } catch (err) {
        previewStatusEl.textContent = "";
        previewEl.innerHTML = `<div class="error">Couldn't run the check: ${esc(err.message)}</div>`;
        return;
      }

      const notes = [];
      if (!data.inactive_channel_configured) {
        notes.push(`<div class="error" style="margin-bottom:10px;">No inactive channel is set up yet,
          so the automatic sweep does nothing even when it is switched on — nobody below would
          actually move. Run <code>/inactive panel</code> in Discord first.</div>`);
      }
      if (data.eligible_count) {
        const capNote = data.eligible_count > data.sweep_cap
          ? `A single run moves at most <strong>${esc(String(data.sweep_cap))}</strong> of these
             ${esc(String(data.eligible_count))}, most idle first — the rest wait for the next run.`
          : `All ${esc(String(data.eligible_count))} fit inside the per-run cap of
             <strong>${esc(String(data.sweep_cap))}</strong>, so one run would move every one of them.`;
        notes.push(`<div class="field-hint" style="margin-bottom:10px;">${capNote}</div>`);
      }

      previewStatusEl.textContent =
        `${data.eligible_count} member(s) idle past ${data.threshold_days} days` +
        (data.blocked_count ? ` · ${data.blocked_count} the bot can't strip` : "");

      if (!data.eligible_count && !data.blocked_count) {
        previewEl.innerHTML = `${notes.join("")}<div class="empty">Nobody is inactive past ${esc(String(data.threshold_days))} days.</div>`;
        return;
      }

      // The action column pushes this past a phone's width, so the table gets
      // its own horizontal scroller rather than pushing the page sideways.
      const table = (rows, prefix, withAction) => `
        <div class="preview-table-scroll">
        <table class="prune-preview-table">
          <thead>
            <tr><th>Member</th><th style="text-align:right;">Days Idle</th><th>Last Seen</th>
              <th>Would Lose</th>${withAction ? "<th></th>" : ""}</tr>
          </thead>
          <tbody>
            ${rows
              .map(
                (r, i) => `
                  <tr>
                    <td>${esc(r.display_name)}</td>
                    <td style="text-align:right; font-variant-numeric: tabular-nums;">${esc(String(r.days_idle))}</td>
                    <td>${lastSeenCell(r)}</td>
                    <td>${rolesCell(r, i, prefix)}</td>
                    ${withAction ? `<td><button type="button" class="btn btn-sm" data-exempt-from-preview="${esc(r.user_id)}" data-exempt-name="${esc(r.display_name)}">Never Sweep</button></td>` : ""}
                  </tr>
                  ${detailRow(r, i, prefix, withAction ? 5 : 4)}`
              )
              .join("")}
          </tbody>
        </table>
        </div>`;

      const blockedBlock = data.blocked_count
        ? `<div class="section-label" style="margin-top:16px;">Would Fail — Bot Outranked</div>
           <div class="field-hint" style="margin-bottom:10px;">
             These members qualify, but their highest role sits at or above the bot's, so it
             cannot remove their roles. A sweep would pick them up and then quietly fail on each
             one. Move the bot's role above theirs, or exempt them to stop it trying.
           </div>
           ${table(data.blocked, "blocked", true)}`
        : "";

      previewEl.innerHTML =
        notes.join("") +
        (data.eligible_count
          ? table(data.members, "sweep", true)
          : `<div class="empty">Nobody would be swept — but see below.</div>`) +
        blockedBlock;

      wireRoleToggles(previewEl);
      previewEl.querySelectorAll("[data-exempt-from-preview]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          await addExemption(btn.dataset.exemptFromPreview, btn.dataset.exemptName);
          exemptPicker.setFilter((o) => !excludedIds().has(String(o.id)));
          const row = btn.closest("tr");
          const detail = row.nextElementSibling;
          if (detail && detail.classList.contains("sweep-roles-detail")) detail.remove();
          row.remove();
        });
      });
    }

    previewBtn.addEventListener("click", runPreview);
  })();
}
