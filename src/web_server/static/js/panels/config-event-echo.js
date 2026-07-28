import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  buildField,
  mountChannelPicker,
  onPickerChange,
  guardForm,
  renderMetaWarning,
} from "../config-helpers.js";
import { renderLoading } from "../states.js";

// One knob, deliberately. The destination channel *is* the on/off switch —
// "(off)" clears the key and nothing posts. A separate enable checkbox would
// be a second thing to get out of step with the first (CLAUDE.md: collapse
// controls; never ship a toggle that isn't enforced).
export function mount(container) {
  container.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "panel";
  loading.innerHTML = renderLoading("Loading Event Echo…");
  container.appendChild(loading);

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const s = config.event_echo || {};

    container.replaceChildren();
    const panel = document.createElement("div");
    panel.className = "panel";
    container.appendChild(panel);

    const header = document.createElement("header");
    const h2 = document.createElement("h2");
    h2.textContent = "Event Echo";
    const sub = document.createElement("div");
    sub.className = "subtitle";
    sub.textContent =
      "Mirror a starting game into main chat, with a link to jump straight to it";
    header.appendChild(h2);
    header.appendChild(sub);
    panel.appendChild(header);

    const warning = renderMetaWarning();
    if (warning) {
      const w = document.createElement("div");
      w.innerHTML = warning;
      panel.appendChild(w.firstElementChild);
    }

    const offBanner = document.createElement("div");
    offBanner.className = "field-hint";
    offBanner.setAttribute("role", "status");
    offBanner.style.cssText =
      "border:1px solid var(--rule); border-radius:6px; padding:10px; margin-bottom:14px; line-height:1.5;";
    offBanner.textContent =
      "Event Echo is off — pick a channel below and save to switch it on.";
    panel.appendChild(offBanner);

    const form = document.createElement("form");
    form.className = "form form-cards";
    panel.appendChild(form);

    const card = document.createElement("div");
    card.className = "card";
    const label = document.createElement("div");
    label.className = "section-label";
    label.textContent = "Echo Destination";
    card.appendChild(label);
    form.appendChild(card);

    const chanSlot = document.createElement("span");
    card.appendChild(buildField(
      "Echo channel",
      chanSlot,
      "Where echoes are posted — normally your main chat. Choose \"(off)\" to stop echoing entirely.",
    ));
    // Snowflakes stay strings throughout; "0" is the codebase-wide unset id
    // (mountChannelPicker's own default) which Billy-bot also writes when it
    // clears a channel setting.
    const chanPicker = mountChannelPicker(
      chanSlot, channels, String(s.channel_id || "0"),
      { emptyLabel: "(off)", label: "Echo channel" },
    );

    function syncBanner() {
      const v = chanPicker.getValue();
      offBanner.style.display = v && v !== "0" ? "none" : "";
    }
    syncBanner();
    onPickerChange(chanPicker, syncBanner);

    // What actually gets echoed, spelled out — the rules live in code and an
    // admin has no other way to find out why a given game didn't appear.
    const rules = document.createElement("div");
    rules.className = "field-hint";
    rules.style.cssText = "margin-top:12px; line-height:1.6;";
    rules.innerHTML = `
      <strong>What gets echoed</strong>
      <ul style="margin:6px 0 0 1.1rem; padding:0;">
        <li>A party game opening for players (<code>/games play …</code>), including
            games the scheduler launches on its own.</li>
        <li>A Cards Against Humanity game starting in the tracked Gamebot channel.</li>
        <li>A Discord server event when it goes live.</li>
      </ul>
      <p style="margin:8px 0 0;">
        Only the <em>start</em> of something joinable — never results, so a link is
        always worth clicking. Echoes are <strong>silent</strong>: no role is pinged
        and nobody is notified.
      </p>
      <p style="margin:6px 0 0;">
        Rate limits: the same kind of game at most <strong>once an hour</strong>, and
        nothing at all within <strong>10 minutes</strong> of the previous echo. A game
        that misses its window is skipped, not queued.
      </p>
    `;
    card.appendChild(rules);

    const saveRow = document.createElement("div");
    saveRow.style.cssText = "display:flex; gap:8px; align-items:center; flex-wrap:wrap;";
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn btn-primary";
    saveBtn.textContent = "Save Settings";
    const saveStatus = document.createElement("span");
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    form.appendChild(saveRow);

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await apiPut("/api/config/event-echo", {
          channel_id: chanPicker.getValue() || "0",
        });
        showStatus(saveStatus, true);
        syncBanner();
      } catch (err) {
        showStatus(saveStatus, false, err.message);
      }
    });
  })();
}
