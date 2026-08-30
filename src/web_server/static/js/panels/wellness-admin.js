import { wGet, wPost, wDelete, esc, showStatus, enfLabel } from "../wellness-helpers.js";
import { confirmDialog, toast } from "../ui.js";
import {
  guardForm,
  mountAsync,
} from "../config-helpers.js";
import { renderLoading, renderEmpty } from "../states.js";

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading wellness settings…")}</div>`;

  return mountAsync(container, async () => {
    // Let the rejection reach mountAsync: it draws the error state *and* a
    // working Try again button. Catching it here rendered a dead-end error
    // and made this panel's own errorMsg unreachable. wellness-caps.js
    // documents the same reasoning where it rethrows on first load.
    const [dash, defaults, users, exempt, prov] = await Promise.all([
      wGet("/api/wellness/admin/dashboard"),
      wGet("/api/wellness/admin/defaults"),
      wGet("/api/wellness/admin/users"),
      wGet("/api/wellness/admin/exempt"),
      wGet("/api/wellness/admin/provision"),
    ]);

    // Activation. role_id and channel_id gate the whole feature — opt-in
    // refuses without the role, the pinned active list and milestone posts
    // refuse without the channel — so an unprovisioned guild gets the
    // Activate card front and center, and a provisioned one a summary with
    // change controls. A stored id whose role/channel was since deleted
    // counts as unprovisioned: the feature is just as dead either way.
    const roleOk = Boolean(prov.role_name);
    const channelOk = Boolean(prov.channel_name);
    const provRoleOpts = (sel) => prov.role_options
      .map(r => `<option value="${r.id}"${r.id === sel ? " selected" : ""}>@${esc(r.name)}</option>`).join("");
    const provChannelOpts = (sel) => prov.channel_options
      .map(c => `<option value="${c.id}"${c.id === sel ? " selected" : ""}>#${esc(c.name)}</option>`).join("");

    let activationHTML;
    if (!prov.bot_connected) {
      activationHTML = `
        <div class="section-label">Activation</div>
        <div class="field-hint">The bot isn’t connected right now, so wellness activation can’t be checked or changed. Try again in a moment.</div>`;
    } else if (roleOk && channelOk) {
      activationHTML = `
        <div class="section-label">Activation</div>
        <div class="field-hint">Wellness is active. The role marks opted-in members; the channel carries the pinned active list and milestone celebrations.</div>
        <div class="w-row">
          <div class="w-row-main">Wellness role: <strong>@${esc(prov.role_name)}</strong></div>
          <div class="w-row-actions"><button class="btn btn-sm" data-change="role">Change</button></div>
        </div>
        <form data-provision-role class="form w-inline-form" hidden style="margin-top:8px;">
          <select name="role_id">${provRoleOpts(prov.role_id)}</select>
          <button type="submit" class="btn btn-primary btn-sm">Save Role</button>
        </form>
        <div class="w-row">
          <div class="w-row-main">Wellness channel: <strong>#${esc(prov.channel_name)}</strong></div>
          <div class="w-row-actions"><button class="btn btn-sm" data-change="channel">Change</button></div>
        </div>
        <form data-provision-channel class="form w-inline-form" hidden style="margin-top:8px;">
          <select name="channel_id">${provChannelOpts(prov.channel_id)}</select>
          <button type="submit" class="btn btn-primary btn-sm">Save Channel</button>
        </form>`;
    } else {
      activationHTML = `
        <div class="section-label">Activate Wellness</div>
        <div class="field-hint">Wellness is off until it has a role and a channel. The role marks opted-in members (members can’t join without it); the channel carries the pinned active list and milestone celebrations.</div>
        <form data-activate-form class="form" style="margin-top:8px;">
          <div class="field">
            <label>Wellness role
              <select name="role_id">
                ${roleOk ? "" : `<option value="auto" selected>✨ Create a “${esc(prov.auto_role_name)}” role for me</option>`}
                ${provRoleOpts(roleOk ? prov.role_id : "")}
              </select>
            </label>
          </div>
          <div class="field">
            <label>Wellness channel
              <select name="channel_id">${provChannelOpts(channelOk ? prov.channel_id : "")}</select>
            </label>
          </div>
          <div><button type="submit" class="btn btn-primary">Activate Wellness</button><span data-activate-status></span></div>
        </form>`;
    }

    // Overview cards
    const overviewHTML = `
      <div class="card-grid">
        <div class="card">
          <div class="stat-label">Members Opted In</div>
          <div class="stat-value">${dash.active_count}</div>
        </div>
        <div class="card">
          <div class="stat-label">Exempt Channels</div>
          <div class="stat-value">${dash.exempt_channels.length}</div>
        </div>
        <div class="card">
          <div class="stat-label">Default Enforcement</div>
          <div class="stat-value">${esc(dash.config?.default_enforcement || "—")}</div>
        </div>
      </div>`;

    // Defaults form
    const cfg = defaults.config || {};
    const defaultsHTML = `
      <div class="section-label">Server Defaults</div>
      <form data-defaults-form class="form">
        <div class="field">
          <label>Default Enforcement
            <select name="default_enforcement">
              ${defaults.enforcement_levels.map(e => `<option value="${e}"${e === cfg.default_enforcement ? " selected" : ""}>${enfLabel(e)}</option>`).join("")}
            </select>
          </label>
          <div class="field-hint">The starting level for members who opt in. Each member can change their own afterwards.</div>
        </div>
        <div><button type="submit" class="btn btn-primary">Save</button><span data-defaults-status></span></div>
      </form>`;

    // Users table
    const usersHTML = users.users.length ? `
      <div class="section-label">Members Opted In</div>
      <table class="w-table">
        <thead><tr><th>Member</th><th>Timezone</th><th>Enforcement</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          ${users.users.map(u => `
            <tr data-uid="${u.user_id}">
              <td>${esc(u.name)}</td>
              <td>${esc(u.timezone)}</td>
              <td>${esc(u.enforcement_level)}</td>
              <td>${u.is_paused ? '<span class="chip chip-warning">Paused</span>' : '<span class="chip chip-success">Active</span>'}</td>
              <td>
                ${u.is_paused
                  ? `<button class="btn btn-sm" data-resume-uid="${u.user_id}">Resume</button>`
                  : `<button class="btn btn-sm" data-pause-uid="${u.user_id}">Pause 60 Minutes</button>`}
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>` : `
      <div class="section-label">Members Opted In</div>
      ${renderEmpty("Nobody has opted in yet. Members join by running /wellness setup in Discord.")}`;

    // Exempt channels
    const exemptListHTML = exempt.exempt.length
      ? exempt.exempt.map(ch => `
          <div class="w-row">
            <div class="w-row-main">#${esc(ch.name)}</div>
            <div class="w-row-actions">
              <button class="btn btn-sm btn-danger" data-unexempt="${ch.id}" data-unexempt-name="${esc(ch.name)}">Remove</button>
            </div>
          </div>
        `).join("")
      : renderEmpty("No exempt channels. Messages in every channel count toward members’ wellness caps — add a channel below to leave it out.");

    const channelOptsHTML = exempt.channel_options.length
      ? exempt.channel_options.map(c => `<option value="${c.id}">#${esc(c.name)}</option>`).join("")
      : '<option value="">No channels available</option>';

    const exemptHTML = `
      <div class="section-label">Exempt Channels</div>
      <div class="w-list">${exemptListHTML}</div>
      <form data-exempt-form class="form w-inline-form" style="margin-top:12px;">
        <label style="display:inline-flex;align-items:center;gap:6px;">Channel to exempt
          <select name="channel_id">${channelOptsHTML}</select>
        </label>
        <button type="submit" class="btn btn-primary">Add Exempt Channel</button>
      </form>`;

    container.querySelector(".panel").innerHTML = `
      <header>
        <h2>Wellness Admin</h2>
        <div class="subtitle">Server-wide defaults for the wellness program. Members still control their own caps and blackouts.</div>
      </header>
      ${overviewHTML}
      ${activationHTML}
      ${defaultsHTML}
      ${usersHTML}
      ${exemptHTML}
    `;

    // Activate card (unprovisioned guild): one submit sets both keys.
    const aForm = container.querySelector("[data-activate-form]");
    if (aForm) {
      const aStatus = container.querySelector("[data-activate-status]");
      aForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(aForm);
        const roleChoice = fd.get("role_id");
        const channelChoice = fd.get("channel_id");
        try {
          await wPost("/api/wellness/admin/provision/role",
            roleChoice === "auto" ? { auto_create: true } : { role_id: roleChoice });
          await wPost("/api/wellness/admin/provision/channel", { channel_id: channelChoice });
          toast("Wellness is now active — members can join with /wellness setup.");
          mount(container);
        } catch (err) { showStatus(aStatus, false, `Couldn’t activate — ${err.message}`); }
      });
    }

    // Change role/channel on an already-active guild.
    container.querySelectorAll("[data-change]").forEach(btn => {
      btn.addEventListener("click", () => {
        const form = container.querySelector(`[data-provision-${btn.dataset.change}]`);
        if (form) form.hidden = !form.hidden;
      });
    });
    for (const [kind, endpoint] of [["role", "role_id"], ["channel", "channel_id"]]) {
      const form = container.querySelector(`[data-provision-${kind}]`);
      if (!form) continue;
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const value = new FormData(form).get(endpoint);
        try {
          await wPost(`/api/wellness/admin/provision/${kind}`, { [endpoint]: value });
          toast(`Wellness ${kind} updated.`);
          mount(container);
        } catch (err) { toast(`Couldn’t change the wellness ${kind} — ${err.message}`, "error"); }
      });
    }

    // Defaults form handler
    const dForm = guardForm(container.querySelector("[data-defaults-form]"));
    const dStatus = container.querySelector("[data-defaults-status]");
    dForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(dForm);
      try {
        await wPost("/api/wellness/admin/defaults", {
          default_enforcement: fd.get("default_enforcement"),
        });
        showStatus(dStatus, true);
      } catch (err) { showStatus(dStatus, false, `Couldn’t save — ${err.message}`); }
    });

    // Pause/Resume. Delegated, because a button flips to the opposite action
    // after it fires: a listener bound to the element would keep running the
    // action the button no longer offers. Before this, acting on a row left a
    // disabled "Paused"/"Resumed" label and no way to undo without a reload.
    container.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-pause-uid], [data-resume-uid]");
      if (!btn || btn.disabled) return;
      const pausing = "pauseUid" in btn.dataset;
      const uid = pausing ? btn.dataset.pauseUid : btn.dataset.resumeUid;
      const statusCell = btn.closest("tr").querySelector("td:nth-child(4)");
      btn.disabled = true;
      try {
        if (pausing) {
          await wPost(`/api/wellness/admin/users/${uid}/pause`, { minutes: 60 });
          statusCell.innerHTML = '<span class="chip chip-warning">Paused</span>';
          delete btn.dataset.pauseUid;
          btn.dataset.resumeUid = uid;
          btn.textContent = "Resume";
        } else {
          await wPost(`/api/wellness/admin/users/${uid}/resume`, {});
          statusCell.innerHTML = '<span class="chip chip-success">Active</span>';
          delete btn.dataset.resumeUid;
          btn.dataset.pauseUid = uid;
          btn.textContent = "Pause 60 Minutes";
        }
      } catch (err) {
        toast(`Couldn’t ${pausing ? "pause" : "resume"} that member — ${err.message}`, "error");
      } finally {
        btn.disabled = false;
      }
    });

    // Exempt remove
    container.querySelectorAll("[data-unexempt]").forEach(btn => {
      btn.addEventListener("click", async () => {
        // Every other destructive action on this page confirms first; this one
        // used to fire on the click and immediately start counting messages in
        // that channel against members' caps.
        const name = btn.dataset.unexemptName || "this channel";
        const ok = await confirmDialog(
          `Messages in #${name} will start counting toward members’ wellness caps again.`,
          { title: "Stop exempting this channel?", danger: true, confirmLabel: "Remove" },
        );
        if (!ok) return;
        try {
          await wDelete(`/api/wellness/admin/exempt/${btn.dataset.unexempt}`);
          btn.closest(".w-row").remove();
        } catch (e) { toast(`Couldn’t remove that exempt channel — ${e.message}`, "error"); }
      });
    });

    // Exempt add
    const exForm = container.querySelector("[data-exempt-form]");
    if (exForm) {
      exForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const cid = new FormData(exForm).get("channel_id");
        if (!cid) return;
        try {
          await wPost("/api/wellness/admin/exempt", { channel_id: cid });
          toast("Exempt channel added.");
          mount(container);
        }
        catch (err) { toast(`Couldn’t add that exempt channel — ${err.message}`, "error"); }
      });
    }
  }, { errorMsg: "Couldn’t load the wellness admin settings." });
}
