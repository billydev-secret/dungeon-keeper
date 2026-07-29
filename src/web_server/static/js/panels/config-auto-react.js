import { esc } from "../api.js";
import { confirmDialog } from "../ui.js";
import {
  loadConfig,
  loadChannels,
  apiPut,
  apiDelete,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelPicker,
  channelName,
} from "../config-helpers.js";

// Supplied by the server, derived from the rake constants — so this stays
// right if the rake changes, instead of accepting prices the service declines.
let MIN_RUNG = 2;

function ruleCard(rule, channels, idx) {
  const uid = `ar-${idx}`;
  const name = rule.channel_id
    ? channelName(channels, rule.channel_id)
    : "New rule";
  const emojiValue = esc(rule.emojis.join(", "));
  const rungRows = rule.emojis
    .map(
      (emoji) => `
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <span style="min-width:2.5em; font-size:1.2em;">${esc(emoji)}</span>
        <input type="number" data-rung="${esc(emoji)}" min="0" step="1"
               value="${rule.rungs?.[emoji] ?? 0}" style="max-width:110px;"
               aria-label="Tip amount for ${esc(emoji)}" />
        <span class="field-hint" style="margin:0;">coins (0 = free)</span>
      </div>`,
    )
    .join("");

  return `
    <form class="card" data-rule data-channel="${esc(rule.channel_id || "")}">
      <div class="section-label">${esc(name)}</div>
      <div class="field">
        <label>Channel</label>
        <span data-picker="channel"></span>
      </div>
      <div class="field">
        <label for="${uid}-emojis">Emoji</label>
        <input type="text" name="emojis" id="${uid}-emojis" value="${emojiValue}"
               placeholder="🔥, 💎, 👑" />
        <div class="field-hint">Comma-separated. Unicode emoji, or custom emoji in
          full <code>&lt;:name:id&gt;</code> form. The bot adds all of them to every
          image posted in this channel.</div>
      </div>
      <div class="field">
        <label style="display:flex; gap:6px; align-items:center;">
          <input type="checkbox" name="enabled"${rule.enabled ? " checked" : ""} />
          Enabled
        </label>
      </div>
      <div class="field">
        <label style="display:flex; gap:6px; align-items:center;">
          <input type="checkbox" name="tips_enabled"${rule.tips_enabled ? " checked" : ""} />
          These emoji are tip buttons
        </label>
        <div class="field-hint">Turns the emoji above into payments: tapping one pays
          the poster from the tapper's own wallet, minus a 10% cut that is destroyed.
          The channel must be marked NSFW in Discord, and the bot only adds emoji to
          images it detects as explicit — so nothing else in the channel becomes
          payable. Leave this off to use Auto React as plain decoration.</div>
      </div>
      <div class="field" data-rungs-wrap${rule.tips_enabled ? "" : ' hidden=""'}>
        <label>Price Per Emoji</label>
        ${rungRows || `<div class="field-hint">Add emoji above and save to set prices.</div>`}
        <div class="field-hint">Which emoji someone taps is how much they give — this
          is the only thing telling them the price, since Discord doesn't ask for
          confirmation before a reaction. Minimum ${MIN_RUNG}. Save after changing the
          emoji list to price any new ones.</div>
      </div>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <button type="submit" class="btn btn-primary">Save</button>
        ${rule.channel_id ? `<button type="button" class="btn btn-danger" data-remove="${esc(rule.channel_id)}">Remove</button>` : ""}
        <span data-status></span>
      </div>
    </form>`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const rules = config.auto_react || [];
    MIN_RUNG = config.nsfw_classifier?.min_rung ?? MIN_RUNG;

    const render = () => {
      const cards = [...rules, { channel_id: "", emojis: [], enabled: true, tips_enabled: false, rungs: {} }]
        .map((r, i) => ruleCard(r, channels, i))
        .join("");

      container.innerHTML = `
        <div class="panel">
          <header>
            <h2>Auto React</h2>
            <div class="subtitle">Emoji the bot adds to image posts — and optionally, tip buttons</div>
          </header>
          ${renderMetaWarning()}
          <div class="form form-cards">${cards}</div>
        </div>
      `;

      container.querySelectorAll("[data-rule]").forEach((form) => {
        const status = form.querySelector("[data-status]");
        const picker = mountChannelPicker(
          form.querySelector('[data-picker="channel"]'),
          channels,
          form.dataset.channel || "0",
          { label: "Channel" },
        );
        const tipsBox = form.querySelector('[name="tips_enabled"]');
        const rungsWrap = form.querySelector("[data-rungs-wrap]");
        tipsBox.addEventListener("change", () => {
          rungsWrap.hidden = !tipsBox.checked;
        });

        guardForm(form);

        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const channelId = picker.getValue();
          if (!channelId || channelId === "0") {
            showStatus(status, false, "Pick a channel.");
            return;
          }
          const emojis = form
            .querySelector('[name="emojis"]')
            .value.split(",")
            .map((s) => s.trim())
            .filter(Boolean);
          if (!emojis.length) {
            showStatus(status, false, "Add at least one emoji.");
            return;
          }

          const rungs = {};
          let bad = null;
          form.querySelectorAll("[data-rung]").forEach((el) => {
            const emoji = el.dataset.rung;
            if (!emojis.includes(emoji)) return; // dropped from the list
            const amount = Number(el.value) || 0;
            if (amount > 0 && amount < MIN_RUNG) bad = emoji;
            rungs[emoji] = amount;
          });
          if (bad) {
            showStatus(
              status,
              false,
              `${bad} must be at least ${MIN_RUNG} coins — a 1-coin tip leaves the poster nothing after the cut.`,
            );
            return;
          }

          try {
            await apiPut(`/api/config/auto-react/${channelId}`, {
              emojis,
              enabled: form.querySelector('[name="enabled"]').checked,
              tips_enabled: tipsBox.checked,
              rungs,
            });
            showStatus(status, true);
            const fresh = await loadConfig();
            rules.length = 0;
            rules.push(...(fresh.auto_react || []));
            render();
          } catch (err) {
            showStatus(status, false, err.message);
          }
        });

        const removeBtn = form.querySelector("[data-remove]");
        if (removeBtn) {
          removeBtn.addEventListener("click", async () => {
            const name = channelName(channels, removeBtn.dataset.remove);
            const ok = await confirmDialog(
              `Stop auto-reacting in ${name}? If tipping is on, the emoji stop being tip buttons immediately.`,
              { title: "Remove Rule", danger: true, confirmLabel: "Remove" },
            );
            if (!ok) return;
            try {
              await apiDelete(
                `/api/config/auto-react/${removeBtn.dataset.remove}`,
              );
              const fresh = await loadConfig();
              rules.length = 0;
              rules.push(...(fresh.auto_react || []));
              render();
            } catch (err) {
              showStatus(status, false, err.message);
            }
          });
        }
      });
    };

    render();
  })();
}
