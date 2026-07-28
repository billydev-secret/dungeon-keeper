import {
  loadChannels,
  apiPost,
  mountChannelPicker,
  esc,
} from "../config-helpers.js";
import { api } from "../api.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { toast } from "../ui.js";

// Posts the bot's channel panels — the sticky messages members interact with
// (economy guide, leaderboard, perk shop, voice control, Guess Who prompt,
// ticket button). Each of these used to be its own slash command whose whole
// job was "put this panel in that channel"; they collapsed into one route on
// 2026-07-28 (POST /api/panels/{key}/post).
//
// Two panels own their destination — Voice Control posts into its configured
// control channel, Guess Who into the configured Guess channel — because their
// buttons drive a flow the cog only looks for in that one place. The API
// reports which via targets_own_channel, so their row shows no picker.

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading channel panels…")}</div>`;

  (async () => {
    let data, channels;
    try {
      [data, channels] = await Promise.all([api("/api/panels"), loadChannels()]);
    } catch (err) {
      container.querySelector(".panel").innerHTML = renderError(
        `Couldn't load the panel list — ${err.message}. Reload the page to try again.`
      );
      return;
    }

    const panels = data.panels || [];
    if (!panels.length) {
      container.querySelector(".panel").innerHTML = renderEmpty("No postable panels.");
      return;
    }

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Channel Panels</h2>
          <div class="subtitle">Post the bot's interactive panels into a channel</div>
        </header>

        <div class="field-hint" style="margin-bottom:1rem">Re-posting a panel into the
          channel it already occupies refreshes it in place rather than moving it to the
          bottom — so you can safely re-post after a re-brand. Posting into a different
          channel moves it. The ticket panel is the exception: each post creates a new
          one, and older panels keep working until you delete them.</div>

        <div class="panel-list">
          ${panels.map(renderRow).join("")}
        </div>
      </div>
    `;

    for (const p of panels) {
      const row = container.querySelector(`[data-panel="${CSS.escape(p.key)}"]`);
      if (!row) continue;
      const slot = row.querySelector("[data-channel-slot]");
      const picker = slot ? mountChannelPicker(slot, channels, "0") : null;

      row.querySelector("[data-post]").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        const status = row.querySelector("[data-status]");
        const channelId = picker ? picker.getValue() : null;
        if (!p.targets_own_channel && (!channelId || channelId === "0")) {
          toast("Pick a channel first.", "error");
          return;
        }
        btn.disabled = true;
        status.textContent = "Posting…";
        try {
          const res = await apiPost(`/api/panels/${encodeURIComponent(p.key)}/post`, {
            channel_id: p.targets_own_channel ? null : channelId,
          });
          status.innerHTML = res.message_url
            ? `<a href="${esc(res.message_url)}" target="_blank" rel="noopener">Posted — open in Discord</a>`
            : "Posted.";
        } catch (err) {
          status.textContent = "";
          toast(err.message, "error");
        } finally {
          btn.disabled = false;
        }
      });
    }
  })();
}

function renderRow(p) {
  const picker = p.targets_own_channel
    ? `<div class="field-hint">Posts into the channel set on its own settings page.</div>`
    : `<div data-channel-slot></div>`;
  const related = p.related_page
    ? ` <a href="#/${esc(p.related_page)}">Settings</a>`
    : "";
  return `
    <div class="panel-row" data-panel="${esc(p.key)}">
      <div class="panel-row__main">
        <div class="section-label">${esc(p.label)}</div>
        <div class="field-hint">${esc(p.description)}${related}</div>
      </div>
      <div class="panel-row__actions">
        ${picker}
        <button class="btn btn-primary" data-post>Post</button>
        <span data-status class="field-hint"></span>
      </div>
    </div>
  `;
}
