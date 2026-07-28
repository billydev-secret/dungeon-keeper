import {
  loadChannels,
  loadBots,
  apiPost,
  mountChannelPicker,
  mountMemberPicker,
  esc,
} from "../config-helpers.js";
import { api } from "../api.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { toast } from "../ui.js";

// Watch another bot's results channel and bank its games as payouts. Replaced
// /games track watch|status|disable|enable|sample on 2026-07-28.
//
// The commands needed a "which bot?" argument for disable/enable and refused
// outright when a server watched more than one; a row per watch makes that
// ambiguity impossible, so the toggle lives on the row it affects.

export function mount(container) {
  container.innerHTML = `<div class="panel">${renderLoading("Loading external tracking…")}</div>`;

  (async () => {
    let data, channels, bots;
    try {
      [data, channels, bots] = await Promise.all([
        api("/api/games-external"),
        loadChannels(),
        loadBots(),
      ]);
    } catch (err) {
      container.querySelector(".panel").innerHTML = renderError(
        `Couldn't load external tracking — ${err.message}. Reload the page to try again.`
      );
      return;
    }

    function render() {
      container.innerHTML = `
        <div class="panel">
          <header>
            <h2>External Game Tracking</h2>
            <div class="subtitle">Bank another bot's game results as payouts</div>
          </header>

          <div class="field-hint" style="margin-bottom:1rem">Point this at a channel
            where another bot posts game results. Dungeon Keeper reads those messages,
            parses them with the format you pick, and pays the winners. Watch a channel,
            let a game or two run, then use <strong>Sample</strong> to confirm the parser
            recognises the output before trusting the payouts.</div>

          <section class="form">
            <div class="section-label">Watched channels</div>
            ${data.watches.length ? renderRows(data.watches) : renderEmpty(
              "Nothing tracked yet. Add a watch below."
            )}
          </section>

          <section class="form" style="margin-top:1.5rem;padding-top:1.25rem;border-top:1px solid var(--border,#333)">
            <div class="section-label">Add a watch</div>
            <div class="field">
              <label>Channel</label>
              <div data-new-channel></div>
              <div class="field-hint">Where the other bot posts its results.</div>
            </div>
            <div class="field">
              <label>Bot</label>
              <div data-new-bot></div>
              <div class="field-hint">The game bot itself — the account that posts the
                results, not the person who started the game.</div>
            </div>
            <div class="field">
              <label for="ge-kind">Format</label>
              <select id="ge-kind" data-new-kind>
                ${data.kinds.map((k) => `<option value="${esc(k.value)}">${esc(k.label)}</option>`).join("")}
              </select>
              <div class="field-hint">Picks the parser and the payout rules.</div>
            </div>
            <button class="btn btn-primary" data-add>Watch this channel</button>
            <span data-add-status class="field-hint"></span>
          </section>

          <div data-sample-out></div>
        </div>
      `;

      const chSlot = container.querySelector("[data-new-channel]");
      const botSlot = container.querySelector("[data-new-bot]");
      const chPicker = mountChannelPicker(chSlot, channels, "0");
      const botPicker = mountMemberPicker(botSlot, bots, "0");

      container.querySelector("[data-add]").addEventListener("click", async () => {
        const status = container.querySelector("[data-add-status]");
        const channelId = chPicker.getValue();
        const botId = botPicker.getValue();
        if (!channelId || channelId === "0") return toast("Pick a channel.", "error");
        if (!botId || botId === "0") return toast("Pick a bot.", "error");
        status.textContent = "Saving…";
        try {
          const res = await apiPost("/api/games-external/watches", {
            channel_id: channelId,
            bot_id: botId,
            kind: container.querySelector("[data-new-kind]").value,
          });
          notifyLive(res);
          await reload();
        } catch (err) {
          status.textContent = "";
          toast(err.message, "error");
        }
      });

      container.querySelectorAll("[data-toggle]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            const res = await apiPost("/api/games-external/watches/toggle", {
              bot_id: btn.dataset.bot,
              channel_id: btn.dataset.channel,
              enabled: btn.dataset.toggle === "on",
            });
            notifyLive(res);
            await reload();
          } catch (err) {
            toast(err.message, "error");
            btn.disabled = false;
          }
        });
      });

      container.querySelectorAll("[data-sample]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const out = container.querySelector("[data-sample-out]");
          out.innerHTML = renderLoading("Fetching banked messages…");
          try {
            const res = await api("/api/games-external/sample", {
              channel_id: btn.dataset.channel,
              bot_id: btn.dataset.bot,
              count: 40,
            });
            out.innerHTML = renderSample(res.messages);
          } catch (err) {
            out.innerHTML = renderError(`Couldn't fetch a sample — ${err.message}`);
          }
        });
      });
    }

    function notifyLive(res) {
      // The row is saved either way; "live" only reports whether the running
      // bot picked it up. Saying so beats a silent no-op until restart.
      if (res && res.live === false) {
        toast("Saved, but the bot is offline — it takes effect on next restart.", "warn");
      }
    }

    async function reload() {
      data = await api("/api/games-external");
      render();
    }

    function channelLabel(id) {
      const ch = channels.find((c) => String(c.id) === String(id));
      return ch ? `#${ch.name}` : `channel ${id}`;
    }

    function botLabel(id) {
      const m = bots.find((x) => String(x.id) === String(id));
      return m ? m.display_name || m.name : `bot ${id}`;
    }

    function renderRows(watches) {
      return `<div class="panel-list">${watches.map((w) => `
        <div class="panel-row">
          <div class="panel-row__main">
            <div class="section-label">${esc(botLabel(w.bot_user_id))} in ${esc(channelLabel(w.channel_id))}</div>
            <div class="field-hint">
              ${esc(w.kind_label)} ·
              ${w.enabled
                ? `<span class="badge badge-success">Collecting</span>`
                : `<span class="badge">Paused</span>`} ·
              <strong>${w.banked}</strong> message${w.banked === 1 ? "" : "s"} banked
            </div>
          </div>
          <div class="panel-row__actions">
            <button class="btn btn-secondary" data-sample
                    data-channel="${esc(w.channel_id)}" data-bot="${esc(w.bot_user_id)}">Sample</button>
            <button class="btn ${w.enabled ? "btn-danger" : "btn-primary"}"
                    data-toggle="${w.enabled ? "off" : "on"}"
                    data-channel="${esc(w.channel_id)}" data-bot="${esc(w.bot_user_id)}">
              ${w.enabled ? "Pause" : "Resume"}
            </button>
          </div>
        </div>`).join("")}</div>`;
    }

    render();
  })();
}

function renderSample(messages) {
  if (!messages.length) {
    return `<section class="form" style="margin-top:1.5rem">
      <div class="section-label">Sample</div>
      ${renderEmpty("Nothing banked for that channel yet. Let a game finish, then try again.")}
    </section>`;
  }
  return `<section class="form" style="margin-top:1.5rem">
    <div class="section-label">Sample — ${messages.length} most recent</div>
    <div class="field-hint" style="margin-bottom:.5rem">Raw content and embeds exactly as
      the other bot posted them, so you can check the parser matches.</div>
    <div class="ge-sample">${messages.map((m) => `
      <div class="ge-sample__row">
        <div class="dim">${esc(m.created_at || "")} · <code>${esc(m.message_id)}</code></div>
        ${m.content ? `<pre>${esc(m.content)}</pre>` : `<div class="dim">(no text content)</div>`}
        ${m.embeds_json && m.embeds_json !== "[]"
          ? `<pre>${esc(m.embeds_json)}</pre>` : ""}
      </div>`).join("")}</div>
  </section>`;
}
