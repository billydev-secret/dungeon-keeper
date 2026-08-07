import { esc, api } from "../api.js";
import { confirmDialog } from "../ui.js";
import {
  loadChannels,
  loadRoles,
  apiPost,
  apiPut,
  apiDelete,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  mountRolePicker,
  mountMemberPicker,
  channelName,
  mountAsync,
  trackCard,
  rerenderUnlessDirty,
  loadMembers,
} from "../config-helpers.js";

// Chip kinds, in the order the "add condition" menu offers them. Labels are
// the admin-facing names; the wire format uses `kind`.
const KINDS = [
  { kind: "contains_text", label: "Contains text" },
  { kind: "mentions_role", label: "Mentions role" },
  { kind: "from_user", label: "From user" },
  { kind: "author_has_role", label: "Author has role" },
];
const KIND_LABEL = Object.fromEntries(KINDS.map((k) => [k.kind, k.label]));

function chipRow(cond) {
  const isText = cond.kind === "contains_text";
  return `
    <div class="card" data-chip data-kind="${esc(cond.kind)}"
         style="padding:10px 12px; display:flex; flex-direction:column; gap:8px;">
      <div style="display:flex; align-items:center; gap:10px;">
        <span class="section-label" style="margin:0; flex:1;">
          ${esc(KIND_LABEL[cond.kind] || cond.kind)}</span>
        <button type="button" class="tag-remove" data-chip-remove
                aria-label="Remove condition" title="Remove condition"
                style="font-size:17px; padding:2px 8px;">&times;</button>
      </div>
      ${isText
        ? `<div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
             <input type="text" class="field-input" data-chip-value maxlength="200"
                    value="${esc(cond.value)}" placeholder="your turn"
                    style="flex:1; min-width:12em;" />
             <label style="display:flex; gap:6px; align-items:center; white-space:nowrap;
                           font-size:12px; color:var(--ink-dim); cursor:pointer;">
               <input type="checkbox" data-chip-regex${cond.regex ? " checked" : ""} /> regex
             </label>
           </div>`
        : `<span data-chip-picker></span>`}
    </div>`;
}

function ruleCard(rule, channels, idx) {
  const uid = `ma-${idx}`;
  const name = rule.channel_id
    ? channelName(channels, rule.channel_id)
    : "New rule";
  const chips = (rule.conditions || []).map((c) => chipRow(c)).join("");

  return `
    <form class="card" data-rule data-id="${esc(rule.id || "")}"
          data-channel="${esc(rule.channel_id || "")}"
          style="display:flex; flex-direction:column; gap:10px;">
      <div class="section-label">${esc(name)}</div>
      <div class="field">
        <label>Channel</label>
        <span data-picker="channel"></span>
        <div class="field-hint">Only messages in this channel can award — threads under it count too.</div>
      </div>
      <div class="field">
        <label for="${uid}-amount">Amount</label>
        <input type="number" name="amount" id="${uid}-amount" min="0" step="1"
               value="${rule.amount ?? 0}" style="max-width:140px;" />
        <div class="field-hint">Coins paid to the person mentioned. 0 parks the
          rule without deleting it.</div>
      </div>
      <div class="field">
        <label>Conditions</label>
        <div data-chips style="display:flex; flex-direction:column; gap:8px;">${chips}</div>
        <div style="display:flex; gap:8px; align-items:center; margin-top:8px; flex-wrap:wrap;">
          <select data-chip-kind style="flex:1; min-width:10em; max-width:15em; padding:6px 26px 6px 9px;">
            ${KINDS.map((k) => `<option value="${k.kind}">${esc(k.label)}</option>`).join("")}
          </select>
          <button type="button" class="btn" data-chip-add>Add condition</button>
        </div>
        <div class="field-hint">Every condition must match for the award to fire
          — and the message must @-mention <strong>exactly one</strong> person,
          who gets paid. Tagging several people pays nobody; you can never award
          yourself. <strong>With no author condition, anyone in the channel can
          hand out currency</strong> — right for a game where the previous player
          announces the next, wide open otherwise. Note: a role ping like
          <code>@Hot Seat</code> is <em>not text</em> — use "Mentions role" for
          it, not "Contains text".</div>
      </div>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <button type="submit" class="btn btn-primary">Save</button>
        ${rule.id ? `<button type="button" class="btn btn-danger" data-remove="${esc(rule.id)}">Remove</button>` : ""}
        <span data-status></span>
      </div>
    </form>`;
}

const BLANK = { id: "", channel_id: "", amount: 0, conditions: [] };

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading rules…</div></div>`;

  return mountAsync(container, async () => {
    const [rules, channels, roles, members] = await Promise.all([
      api("/api/mention-awards/rules"),
      loadChannels(),
      loadRoles(),
      loadMembers(),
    ]);

    // Mount the right value widget into one chip row; returns a getValue().
    const mountChipWidget = (row, cond) => {
      if (cond.kind === "contains_text") {
        return {
          getValue: () => row.querySelector("[data-chip-value]").value.trim(),
          getRegex: () => row.querySelector("[data-chip-regex]").checked,
        };
      }
      const slot = row.querySelector("[data-chip-picker]");
      const picker = cond.kind === "from_user"
        ? mountMemberPicker(slot, members, cond.value || "0", { label: "Member" })
        : mountRolePicker(slot, roles, cond.value || "0", { label: "Role" });
      return { getValue: () => picker.getValue(), getRegex: () => false };
    };

    const refresh = async () => {
      const fresh = await api("/api/mention-awards/rules");
      rules.length = 0;
      rules.push(...fresh);
    };

    const render = () => {
      const all = [...rules, BLANK];
      const cards = all.map((r, i) => ruleCard(r, channels, i)).join("");

      container.innerHTML = `
        <div class="panel">
          <header>
            <h2>Mention Awards</h2>
            <div class="subtitle">Pay whoever gets @-mentioned when a message matches your conditions</div>
          </header>
          ${renderMetaWarning()}
          <p class="field-hint" style="margin:0 0 12px;">For games the bot doesn't
            run. Build each rule from condition chips — contains text (or regex),
            mentions a role, from a specific user, author holds a role. When a
            message matches them all and tags someone, the person tagged is paid.
            Each announcement pays once, so editing a message can't pay twice.</p>
          <div class="form form-cards">${cards}</div>
        </div>
      `;

      const cardsRoot = container.querySelector(".form-cards");

      container.querySelectorAll("[data-rule]").forEach((form, formIdx) => {
        const status = form.querySelector("[data-status]");
        const rule = all[formIdx];
        const chPicker = mountChannelPicker(
          form.querySelector('[data-picker="channel"]'),
          channels,
          form.dataset.channel || "0",
          { label: "Channel" },
        );

        // Live chip state: widgets keyed by row element, so Save can collect
        // without re-rendering (which would wipe sibling cards' edits).
        const widgets = new Map();
        const chipsRoot = form.querySelector("[data-chips]");
        chipsRoot.querySelectorAll("[data-chip]").forEach((row, i) => {
          widgets.set(row, mountChipWidget(row, rule.conditions[i]));
        });

        const wireRemove = (row) => {
          row.querySelector("[data-chip-remove]").addEventListener("click", () => {
            widgets.delete(row);
            row.remove();
          });
        };
        chipsRoot.querySelectorAll("[data-chip]").forEach(wireRemove);

        form.querySelector("[data-chip-add]").addEventListener("click", () => {
          const kind = form.querySelector("[data-chip-kind]").value;
          const cond = { kind, value: "", regex: false };
          const tpl = document.createElement("template");
          tpl.innerHTML = chipRow(cond).trim();
          const row = tpl.content.firstElementChild;
          chipsRoot.appendChild(row);
          widgets.set(row, mountChipWidget(row, cond));
          wireRemove(row);
        });

        guardForm(form);
        trackCard(form);

        const collectChips = () =>
          [...chipsRoot.querySelectorAll("[data-chip]")].map((row) => ({
            kind: row.dataset.kind,
            value: widgets.get(row).getValue(),
            regex: widgets.get(row).getRegex(),
          }));

        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const channelId = chPicker.getValue();
          if (!channelId || channelId === "0") {
            showStatus(status, false, "Pick a channel.");
            return;
          }
          const conditions = collectChips();
          if (!conditions.length) {
            showStatus(status, false, "Add at least one condition.");
            return;
          }
          const body = {
            channel_id: channelId,
            amount: Number(form.querySelector('[name="amount"]').value) || 0,
            conditions,
          };

          try {
            const id = form.dataset.id;
            if (id) {
              await apiPut(`/api/mention-awards/rules/${id}`, body);
            } else {
              // Record the new id immediately: if the rebuild below is
              // skipped (a sibling card holds unsaved edits), a second Save
              // on this card must PUT, not POST a duplicate rule.
              const created = await apiPost("/api/mention-awards/rules", body);
              form.dataset.id = String(created.id);
            }
            showStatus(status, true);
            await refresh();
            // Don't rebuild while a sibling card holds unsaved edits — the
            // save just cleared the dirty flag, so the guard couldn't warn.
            rerenderUnlessDirty(cardsRoot, form, render);
          } catch (err) {
            showStatus(status, false, err.message);
          }
        });

        const removeBtn = form.querySelector("[data-remove]");
        if (removeBtn) {
          removeBtn.addEventListener("click", async () => {
            const where = channelName(channels, form.dataset.channel);
            const ok = await confirmDialog(
              `Remove this award rule in ${where}? Its conditions stop paying immediately. Awards already paid are not reversed.`,
              { title: "Remove Rule", danger: true, confirmLabel: "Remove" },
            );
            if (!ok) return;
            try {
              await apiDelete(`/api/mention-awards/rules/${removeBtn.dataset.remove}`);
              await refresh();
              // Remove just this card: a full re-render would wipe unsaved
              // edits in sibling cards — the loss rerenderUnlessDirty exists
              // to prevent on the save path.
              form.remove();
            } catch (err) {
              showStatus(status, false, err.message);
            }
          });
        }
      });
    };

    render();
  }, { errorMsg: "Couldn’t load the mention award rules." });
}
