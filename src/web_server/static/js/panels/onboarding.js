import { esc, mountAsync, showStatus, apiPost } from "../config-helpers.js";
import { api } from "../api.js";
import { renderEmpty } from "../states.js";
import { confirmDialog, toast } from "../ui.js";

// Discord's built-in onboarding ("Channels & Roles"). This page does one job:
// put Dungeon Keeper's opt-in ping roles somewhere members actually pick roles
// up. Editing onboarding REPLACES the whole prompt list, and an admin edits the
// same config by hand in Server Settings — so nothing here writes without an
// explicit confirm, and the server re-reads and re-plans before every write.

const STATE_LABEL = {
  offered: { text: "In onboarding", cls: "badge-success" },
  ready: { text: "Ready to add", cls: "" },
  uncreated: { text: "Will be created", cls: "" },
  off: { text: "Turned off", cls: "badge-muted" },
};

function roleRow(role) {
  const meta = STATE_LABEL[role.state] || { text: role.state, cls: "" };
  const selectable = role.state === "ready" || role.state === "uncreated";
  const why = role.state === "off"
    ? "Set to (none) on this feature's own page — nothing will be created."
    : role.state === "offered"
      ? "Members can already pick this up in onboarding."
      : role.blurb;
  return `
    <tr>
      <td style="width:32px">
        <input type="checkbox" data-key="${esc(role.key)}"
               aria-label="Offer ${esc(role.name)} in onboarding"
               ${selectable ? "" : "disabled"}>
      </td>
      <td><strong>${esc(role.emoji)} ${esc(role.name)}</strong>
          <div class="field-hint">${esc(why)}</div></td>
      <td><span class="badge ${meta.cls}">${esc(meta.text)}</span></td>
    </tr>`;
}

function promptCard(prompt) {
  const opts = prompt.options.length
    ? prompt.options
        .map((o) => `<li>${esc(o.emoji)} ${esc(o.title)}` +
          (o.role_ids.length ? "" : ' <span class="field-hint">(no roles)</span>') +
          "</li>")
        .join("")
    : '<li class="field-hint">No choices yet</li>';
  return `
    <div class="card" style="margin-bottom:10px">
      <strong>${esc(prompt.title)}</strong>
      <span class="field-hint">${esc(prompt.type === "dropdown" ? "dropdown" : "multiple choice")}</span>
      <ul style="margin:6px 0 0 18px">${opts}</ul>
    </div>`;
}

export function mount(container) {
  mountAsync(container, async (root) => {
    const data = await api("/api/onboarding");
    render(root, data);
  }, { errorMsg: "Couldn't read this server's onboarding." });
}

function render(root, data) {
  const prompts = data.prompts || [];
  const roles = data.roles || [];
  const addable = roles.filter((r) => r.state === "ready" || r.state === "uncreated");

  const destination = prompts.length
    ? `<label>Add them to
         <select data-dest>
           ${prompts.map((p) => `<option value="${esc(p.id)}">${esc(p.title)}</option>`).join("")}
           <option value="">— a new question —</option>
         </select>
       </label>
       <label data-newtitle-wrap hidden>New question title
         <input type="text" data-newtitle maxlength="100" placeholder="Get pinged for…">
       </label>`
    : `<label>New question title
         <input type="text" data-newtitle maxlength="100" value="Get pinged for…">
       </label>`;

  root.innerHTML = `
    <div class="panel">
      <header><h2>Discord Onboarding</h2></header>
      <p class="field-hint">
        This is Discord's own <strong>Channels &amp; Roles</strong> screen — the one
        new members walk through, and the one anybody can reopen from the server
        menu. Roles offered here are the ones members actually end up with.
      </p>
      ${data.can_edit ? "" : `<p class="error">The bot needs <strong>Manage Server</strong>
        and <strong>Manage Roles</strong> to change onboarding. You can look, but not save.</p>`}

      <h3>Opt-in roles Dungeon Keeper manages</h3>
      <table class="table">
        <thead><tr><th></th><th>Role</th><th>Status</th></tr></thead>
        <tbody>${roles.map(roleRow).join("")}</tbody>
      </table>
      ${addable.length ? `
        <div class="form-row" style="margin-top:10px">${destination}</div>
        <button type="button" class="btn btn-primary" data-add
                ${data.can_edit ? "" : "disabled"}>Add to onboarding</button>
        <span data-status></span>
      ` : `<p class="field-hint">Every managed role is either already offered or switched off.</p>`}

      <h3 style="margin-top:18px">What onboarding asks today</h3>
      ${prompts.length ? prompts.map(promptCard).join("") : renderEmpty("This server has no onboarding questions yet.")}
    </div>`;

  const dest = root.querySelector("[data-dest]");
  const newWrap = root.querySelector("[data-newtitle-wrap]");
  if (dest && newWrap) {
    dest.addEventListener("change", () => {
      newWrap.hidden = dest.value !== "";
    });
  }

  root.querySelector("[data-add]")?.addEventListener("click", async () => {
    const keys = [...root.querySelectorAll("input[data-key]:checked")]
      .map((el) => el.dataset.key);
    if (!keys.length) {
      showStatus(root.querySelector("[data-status]"), false, "Pick at least one role.");
      return;
    }
    const promptId = dest ? dest.value : "";
    const newTitle = promptId ? "" : (root.querySelector("[data-newtitle]")?.value || "").trim();
    if (!promptId && !newTitle) {
      showStatus(root.querySelector("[data-status]"), false, "Name the new question.");
      return;
    }
    const where = promptId
      ? `the “${dest.selectedOptions[0].textContent}” question`
      : `a new question called “${newTitle}”`;
    const ok = await confirmDialog(
      `Add ${keys.length} role${keys.length === 1 ? "" : "s"} to ${where}. `
      + "This edits the screen new members see. Nothing else about your "
      + "onboarding changes.",
      { title: "Change onboarding?", confirmLabel: "Add them" },
    );
    if (!ok) return;

    try {
      const res = await apiPost("/api/onboarding/add-roles", {
        keys, prompt_id: promptId, new_prompt_title: newTitle,
      });
      if (res.unavailable?.length) {
        toast(`Couldn't create: ${res.unavailable.join(", ")}`, "error");
      }
      if (!res.written) {
        toast("Nothing to add — those roles are already offered.", "info");
      } else {
        toast(`Added ${res.added.join(", ")} to onboarding.`);
      }
      render(root, await api("/api/onboarding"));
    } catch (err) {
      showStatus(root.querySelector("[data-status]"), false, err.message || String(err));
    }
  });
}
