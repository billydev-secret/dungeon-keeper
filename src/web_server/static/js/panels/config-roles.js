import {
  loadConfig, loadChannels, loadRoles, roleName, apiPut, apiDelete, showStatus,
  guardForm, renderMetaWarning, mountRolePicker, mountChannelPicker, mountPicker,
  toRoleOptions, esc,
} from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading role grants…</div></div>`;

  (async () => {
    const [config, channels, roles] = await Promise.all([loadConfig(), loadChannels(), loadRoles()]);
    render(container, config.roles, channels, roles);
  })();
}

function permLabel(perm, roles) {
  const prefix = perm.entity_type === "role" ? "Role" : "Member";
  const name = perm.entity_type === "role" ? roleName(roles, perm.entity_id) : perm.entity_id;
  return `${prefix}: ${name}`;
}

function render(container, grants, channels, roles) {
  const names = Object.keys(grants);

  function renderRole(name) {
    const g = grants[name];
    const perms = g.permissions || [];
    const permListHTML = perms.length
      ? `<div class="tag-list">${perms.map((p, i) => `
          <span class="tag">
            ${esc(permLabel(p, roles))}
            <button type="button" class="tag-remove perm-remove" data-grant="${esc(name)}" data-idx="${i}" title="Remove ${esc(permLabel(p, roles))}">&times;</button>
          </span>`).join("")}</div>`
      : '<em style="color:var(--ink-mute);">Moderators only — nobody else can hand this role out.</em>';

    return `
      <div class="role-card card" style="margin-bottom:16px;" data-grant="${esc(name)}">
        <div class="section-label">${esc(g.label || name)}</div>
        <form class="form" data-save-form="${esc(name)}">
          <div class="field">
            <label for="cr-label-${esc(name)}">Display Name</label>
            <input type="text" name="label" id="cr-label-${esc(name)}" required value="${esc(g.label || name)}" />
            <div class="field-hint">What this grant is called in commands and log messages. Its internal key is <code>${esc(name)}</code> and cannot be changed.</div>
          </div>
          <div class="field">
            <label>Role Handed Out</label>
            <span data-picker="role_id" data-grant="${esc(name)}"></span>
            <div class="field-hint">The Discord role members actually receive. Choose "(none)" and this grant does nothing.</div>
          </div>
          <div class="field">
            <label>Log Channel</label>
            <span data-picker="log_channel_id" data-grant="${esc(name)}"></span>
            <div class="field-hint">A private record of every time this role is given or taken away. "(disabled)" keeps no record.</div>
          </div>
          <div class="field">
            <label>Announcement Channel</label>
            <span data-picker="announce_channel_id" data-grant="${esc(name)}"></span>
            <div class="field-hint">Where the public congratulations message below is posted. "(disabled)" announces nothing.</div>
          </div>
          <div class="field">
            <label for="cr-msg-${esc(name)}">Announcement Message</label>
            <textarea name="grant_message" id="cr-msg-${esc(name)}">${esc(g.grant_message || "")}</textarea>
            <div class="field-hint">Posted in the announcement channel when someone receives the role. Placeholders: {member} pings them, {role} prints the role name.</div>
          </div>
          <div class="field">
            <label>Role Required First</label>
            <span data-picker="required_role_id" data-grant="${esc(name)}"></span>
            <div class="field-hint">A member cannot receive this grant unless they already have this role — useful for gating NSFW or veteran perks. "(none)" means anyone is eligible.</div>
          </div>
          <div class="field">
            <label>Who Can Hand This Out</label>
            <div data-perm-list="${esc(name)}" style="margin-bottom:8px;">${permListHTML}</div>
            <div style="display:flex; gap:6px; flex-wrap:wrap; align-items:center;">
              <label class="visually-hidden" for="cr-perm-type-${esc(name)}" style="position:absolute; left:-9999px;">Grant permission to a role or a member</label>
              <select data-perm-type="${esc(name)}" id="cr-perm-type-${esc(name)}" style="width:auto;">
                <option value="role">A role</option>
                <option value="user">One member</option>
              </select>
              <span data-perm-role-slot="${esc(name)}"></span>
              <label class="visually-hidden" for="cr-perm-user-${esc(name)}" style="position:absolute; left:-9999px;">Member ID</label>
              <input type="text" data-perm-user="${esc(name)}" id="cr-perm-user-${esc(name)}" inputmode="numeric" placeholder="Member ID (17–20 digits)" style="width:180px; display:none;" />
              <button type="button" class="btn btn-sm" data-perm-add="${esc(name)}">Add</button>
            </div>
            <div class="field-hint">Anyone listed here can hand out this role without being a moderator. Leave it empty to keep it moderator-only. A member ID is the long number from Discord's "Copy User ID".</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <button type="submit" class="btn btn-primary">Save</button>
            <button type="button" class="btn btn-danger" data-remove-role="${esc(name)}">Delete Grant</button>
            <span data-status></span>
          </div>
        </form>
      </div>`;
  }

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Role Grants</h2>
        <div class="subtitle">Roles your team can hand out on request — access roles, age-verified roles, veteran perks, and the like</div>
      </header>
      ${renderMetaWarning()}
      <div data-grants>${names.length
        ? names.map(renderRole).join("")
        : '<div class="empty">No role grants yet. Create your first one below.</div>'}</div>

      <div class="section-label">Add a Role Grant</div>
      <form class="form card" data-add-role-form>
        <div class="field">
          <label for="cr-new-key">Internal Key</label>
          <input type="text" name="grant_name" id="cr-new-key" required placeholder="denizen" pattern="[a-z0-9_]+" />
          <div class="field-hint">A short lowercase name used in commands — letters, numbers, and underscores only. It cannot be changed later.</div>
        </div>
        <div class="field">
          <label for="cr-new-label">Display Name</label>
          <input type="text" name="label" id="cr-new-label" required placeholder="Denizen" />
          <div class="field-hint">What members and moderators see. This one you can change any time.</div>
        </div>
        <div class="field">
          <label>Role Handed Out</label>
          <span data-picker="role_id" data-grant="__new__"></span>
          <div class="field-hint">The Discord role members actually receive.</div>
        </div>
        <div class="field">
          <label>Log Channel</label>
          <span data-picker="log_channel_id" data-grant="__new__"></span>
          <div class="field-hint">A private record of every time this role is given or taken away. "(disabled)" keeps no record.</div>
        </div>
        <div class="field">
          <label>Announcement Channel</label>
          <span data-picker="announce_channel_id" data-grant="__new__"></span>
          <div class="field-hint">Where the public congratulations message is posted. "(disabled)" announces nothing.</div>
        </div>
        <div class="field">
          <label for="cr-new-msg">Announcement Message</label>
          <textarea name="grant_message" id="cr-new-msg" placeholder="Welcome {member} to {role}!"></textarea>
          <div class="field-hint">Placeholders: {member} pings the member, {role} prints the role name.</div>
        </div>
        <div class="field">
          <label>Role Required First</label>
          <span data-picker="required_role_id" data-grant="__new__"></span>
          <div class="field-hint">A member cannot receive this grant unless they already have this role. "(none)" means anyone is eligible.</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Add Grant</button>
          <span data-add-status></span>
        </div>
      </form>
    </div>`;

  // ── Searchable pickers replace the old plain <select>s (W-C4). ────────
  const pickers = {}; // pickers[grantName][fieldName]
  function mountRow(grantName, source) {
    const defs = [
      ["role_id", mountRolePicker, roles, "Role Handed Out"],
      ["log_channel_id", mountChannelPicker, channels, "Log Channel"],
      ["announce_channel_id", mountChannelPicker, channels, "Announcement Channel"],
      ["required_role_id", mountRolePicker, roles, "Role Required First"],
    ];
    pickers[grantName] = {};
    for (const [fieldName, mountFn, options, label] of defs) {
      const slot = container.querySelector(
        `[data-picker="${fieldName}"][data-grant="${CSS.escape(grantName)}"]`,
      );
      if (!slot) continue;
      pickers[grantName][fieldName] = mountFn(
        slot, options, String(source[fieldName] || "0"), { label },
      );
    }
  }
  for (const name of names) mountRow(name, grants[name]);
  mountRow("__new__", {});

  // Permission type toggle (role picker vs member ID input)
  const permRolePickers = {};
  for (const name of names) {
    const typeSelect = container.querySelector(`[data-perm-type="${CSS.escape(name)}"]`);
    const roleSlot = container.querySelector(`[data-perm-role-slot="${CSS.escape(name)}"]`);
    const userEl = container.querySelector(`[data-perm-user="${CSS.escape(name)}"]`);
    permRolePickers[name] = mountPicker(roleSlot, toRoleOptions(roles), "0", {
      emptyValue: "0", emptyLabel: "(pick a role)", label: "Role allowed to hand this out",
    });
    const rolePickerEl = permRolePickers[name].el;
    rolePickerEl.style.minWidth = "200px";
    typeSelect.addEventListener("change", () => {
      const isRole = typeSelect.value === "role";
      rolePickerEl.style.display = isRole ? "" : "none";
      userEl.style.display = isRole ? "none" : "";
    });
  }

  // Local add permission
  for (const name of names) {
    const g = grants[name];
    if (!g._perms) g._perms = (g.permissions || []).slice();

    container.querySelector(`[data-perm-add="${CSS.escape(name)}"]`).addEventListener("click", () => {
      const typeSelect = container.querySelector(`[data-perm-type="${CSS.escape(name)}"]`);
      const entityType = typeSelect.value;
      let entityId;
      if (entityType === "role") {
        entityId = permRolePickers[name].getValue();
      } else {
        entityId = container.querySelector(`[data-perm-user="${CSS.escape(name)}"]`).value.trim();
      }
      if (!entityId || entityId === "0") {
        toast(entityType === "role" ? "Pick a role to add first." : "Enter a member ID first.", "error");
        return;
      }
      if (entityType === "user" && !/^\d{15,25}$/.test(entityId)) {
        toast("That doesn't look like a member ID — it should be 17 to 20 digits.", "error");
        return;
      }
      if (g._perms.some((p) => p.entity_type === entityType && p.entity_id === entityId)) return;
      // Ids stay strings end-to-end.
      g._perms.push({ entity_type: entityType, entity_id: String(entityId) });
      refreshPermList(name);
    });
  }

  // Local remove permission
  container.addEventListener("click", (e) => {
    const btn = e.target.closest(".perm-remove");
    if (!btn) return;
    const name = btn.dataset.grant;
    const idx = parseInt(btn.dataset.idx, 10);
    const g = grants[name];
    if (!g._perms) g._perms = (g.permissions || []).slice();
    g._perms.splice(idx, 1);
    refreshPermList(name);
  });

  function refreshPermList(name) {
    const g = grants[name];
    const perms = g._perms || [];
    const listEl = container.querySelector(`[data-perm-list="${CSS.escape(name)}"]`);
    if (!perms.length) {
      listEl.innerHTML = '<em style="color:var(--ink-mute);">Moderators only — nobody else can hand this role out.</em>';
      return;
    }
    const tagsHTML = perms.map((p, i) => `
      <span class="tag">
        ${esc(permLabel(p, roles))}
        <button type="button" class="tag-remove perm-remove" data-grant="${esc(name)}" data-idx="${i}" title="Remove ${esc(permLabel(p, roles))}">&times;</button>
      </span>`).join("");
    listEl.innerHTML = `<div class="tag-list">${tagsHTML}</div>`;
  }

  // Save handlers
  for (const name of names) {
    const form = container.querySelector(`[data-save-form="${CSS.escape(name)}"]`);
    const status = form.querySelector("[data-status]");
    guardForm(form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const g = grants[name];
      const label = String(fd.get("label") || "").trim();
      if (!label) {
        showStatus(status, false, "Display Name cannot be empty.");
        form.querySelector('[name="label"]').focus();
        return;
      }
      try {
        await apiPut(`/api/config/roles/${name}`, {
          label,
          role_id: pickers[name].role_id.getValue() || "0",
          log_channel_id: pickers[name].log_channel_id.getValue() || "0",
          announce_channel_id: pickers[name].announce_channel_id.getValue() || "0",
          grant_message: fd.get("grant_message"),
          required_role_id: pickers[name].required_role_id.getValue() || "0",
          permissions: g._perms || g.permissions || [],
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  // Remove role handlers
  container.querySelectorAll("[data-remove-role]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.removeRole;
      const label = (grants[key] && grants[key].label) || key;
      const ok = await confirmDialog(
        `Delete the "${label}" role grant? Your team will no longer be able to hand this role out, `
        + "and its settings are gone for good. Members who already have the Discord role keep it.",
        { title: "Delete Role Grant", danger: true, confirmLabel: "Delete Grant" },
      );
      if (!ok) return;
      try {
        await apiDelete(`/api/config/roles/${key}`);
        const fresh = await loadConfig();
        render(container, fresh.roles, channels, roles);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  // Add new role handler
  const addForm = container.querySelector("[data-add-role-form]");
  const addStatus = container.querySelector("[data-add-status]");
  guardForm(addForm);
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(addForm);
    const grantName = String(fd.get("grant_name") || "").trim().toLowerCase().replace(/\s+/g, "_");
    if (!grantName) {
      showStatus(addStatus, false, "Internal Key cannot be empty.");
      addForm.querySelector('[name="grant_name"]').focus();
      return;
    }
    if (!/^[a-z0-9_]+$/.test(grantName)) {
      showStatus(addStatus, false, "Internal Key may only contain lowercase letters, numbers, and underscores.");
      addForm.querySelector('[name="grant_name"]').focus();
      return;
    }
    if (Object.prototype.hasOwnProperty.call(grants, grantName)) {
      showStatus(addStatus, false, `A grant with the key "${grantName}" already exists.`);
      addForm.querySelector('[name="grant_name"]').focus();
      return;
    }
    if (!String(fd.get("label") || "").trim()) {
      showStatus(addStatus, false, "Display Name cannot be empty.");
      addForm.querySelector('[name="label"]').focus();
      return;
    }
    try {
      await apiPut(`/api/config/roles/${grantName}`, {
        label: fd.get("label"),
        role_id: pickers.__new__.role_id.getValue() || "0",
        log_channel_id: pickers.__new__.log_channel_id.getValue() || "0",
        announce_channel_id: pickers.__new__.announce_channel_id.getValue() || "0",
        grant_message: fd.get("grant_message"),
        required_role_id: pickers.__new__.required_role_id.getValue() || "0",
      });
      const fresh = await loadConfig();
      render(container, fresh.roles, channels, roles);
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });
}
