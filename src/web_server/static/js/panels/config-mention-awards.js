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
  channelName,
  mountAsync,
  trackCard,
  rerenderUnlessDirty,
} from "../config-helpers.js";

// One card per rule, plus a blank card at the end to add another. A rule with
// no id has never been saved.
function ruleCard(rule, channels, idx) {
  const uid = `ma-${idx}`;
  const name = rule.channel_id
    ? channelName(channels, rule.channel_id)
    : "New rule";

  return `
    <form class="card" data-rule data-id="${esc(rule.id || "")}"
          data-channel="${esc(rule.channel_id || "")}"
          data-role="${esc(rule.announcer_role_id || "0")}">
      <div class="section-label">${esc(name)}</div>
      <div class="field">
        <label>Channel</label>
        <span data-picker="channel"></span>
        <div class="field-hint">Only messages in this channel can award.</div>
      </div>
      <div class="field">
        <label for="${uid}-phrase">Trigger phrase</label>
        <input type="text" name="phrase" id="${uid}-phrase" maxlength="200"
               value="${esc(rule.phrase || "")}" placeholder="your turn" />
        <div class="field-hint">Case-insensitive. The award fires when a message
          contains this anywhere <em>and</em> @-mentions exactly one person — that
          person gets paid. A message tagging several people is ignored, and you
          can never award yourself.</div>
      </div>
      <div class="field">
        <label for="${uid}-amount">Amount</label>
        <input type="number" name="amount" id="${uid}-amount" min="0" step="1"
               value="${rule.amount ?? 0}" style="max-width:140px;" />
        <div class="field-hint">Coins paid to the person mentioned. 0 parks the
          rule without deleting it.</div>
      </div>
      <div class="field">
        <label>Who can award</label>
        <span data-picker="role"></span>
        <div class="field-hint">Leave unset and <strong>anyone</strong> in the
          channel can hand out currency by typing the phrase. Set a role when the
          game has fixed hosts. Leave it open when the game passes a baton — the
          previous player announces the next one.</div>
      </div>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <button type="submit" class="btn btn-primary">Save</button>
        ${rule.id ? `<button type="button" class="btn btn-danger" data-remove="${esc(rule.id)}">Remove</button>` : ""}
        <span data-status></span>
      </div>
    </form>`;
}

const BLANK = { id: "", channel_id: "", phrase: "", amount: 0, announcer_role_id: "0" };

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading rules…</div></div>`;

  return mountAsync(container, async () => {
    const [rules, channels, roles] = await Promise.all([
      api("/api/mention-awards/rules"),
      loadChannels(),
      loadRoles(),
    ]);

    const render = () => {
      const cards = [...rules, BLANK]
        .map((r, i) => ruleCard(r, channels, i))
        .join("");

      container.innerHTML = `
        <div class="panel">
          <header>
            <h2>Mention Awards</h2>
            <div class="subtitle">Pay whoever gets @-mentioned alongside a trigger phrase</div>
          </header>
          ${renderMetaWarning()}
          <p class="field-hint" style="margin:0 0 12px;">For games the bot doesn't
            run. When a member posts the phrase and tags someone — "@Hot Seat your
            turn @someone!" — the person tagged is paid automatically. Each
            announcement pays once, so editing a message can't pay twice.</p>
          <div class="form form-cards">${cards}</div>
        </div>
      `;

      const cardsRoot = container.querySelector(".form-cards");

      container.querySelectorAll("[data-rule]").forEach((form) => {
        const status = form.querySelector("[data-status]");
        const chPicker = mountChannelPicker(
          form.querySelector('[data-picker="channel"]'),
          channels,
          form.dataset.channel || "0",
          { label: "Channel" },
        );
        const rolePicker = mountRolePicker(
          form.querySelector('[data-picker="role"]'),
          roles,
          form.dataset.role || "0",
          { label: "Who can award" },
        );

        guardForm(form);
        trackCard(form);

        const refresh = async () => {
          const fresh = await api("/api/mention-awards/rules");
          rules.length = 0;
          rules.push(...fresh);
        };

        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const channelId = chPicker.getValue();
          if (!channelId || channelId === "0") {
            showStatus(status, false, "Pick a channel.");
            return;
          }
          const phrase = form.querySelector('[name="phrase"]').value.trim();
          if (!phrase) {
            showStatus(status, false, "Add a trigger phrase.");
            return;
          }
          const amount = Number(form.querySelector('[name="amount"]').value) || 0;

          const body = {
            channel_id: channelId,
            phrase,
            amount,
            announcer_role_id: rolePicker.getValue() || "0",
          };

          try {
            const id = form.dataset.id;
            if (id) {
              await apiPut(`/api/mention-awards/rules/${id}`, body);
            } else {
              await apiPost("/api/mention-awards/rules", body);
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
              `Remove this award rule in ${where}? The phrase stops paying immediately. Awards already paid are not reversed.`,
              { title: "Remove Rule", danger: true, confirmLabel: "Remove" },
            );
            if (!ok) return;
            try {
              await apiDelete(`/api/mention-awards/rules/${removeBtn.dataset.remove}`);
              await refresh();
              render();
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
