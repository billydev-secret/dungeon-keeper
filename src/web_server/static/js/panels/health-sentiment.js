import { api, esc } from "../api.js";
import { renderEmpty, renderError } from "../states.js";
import { mountBotToggle, mountReloadable } from "../report-helpers.js";
import { jumpLink } from "../audit-helpers.js";

// This panel used to lead with a composite "emotional temperature": an average
// sentiment score, a positive:negative ratio against a 3:1 target, a spike
// count, a 30-day trend line and a per-channel bar chart. None of it was
// actionable — an average over every message in a busy server barely moves, so
// the number said the same thing every day, and no one could say what they
// would *do* differently at 0.21 versus 0.18. What people actually opened the
// page for was the one table at the bottom: the negative messages, so they
// could go read them. That table is now the whole panel.

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function mount(container) {
  let includeBots = false;
  container.innerHTML =
    '<div class="panel"><div class="panel-loading">Loading flagged messages…</div></div>';

  async function load() {
    const botParam = includeBots ? { include_bots: "true" } : {};
    // The feed's rejection is deliberately NOT caught here. It used to fall
    // back to an empty message list, which was survivable when the feed was a
    // footnote under the scorecards — but now that it *is* the panel, a failed
    // fetch would render as "nothing flagged", which is a lie in the one
    // direction that matters. Letting it reject hands mountReloadable a real
    // error with a retry.
    const [d, feed] = await Promise.all([
      api("/api/health/sentiment", includeBots ? botParam : undefined),
      api("/api/health/sentiment-feed", { polarity: "negative", ...botParam }),
    ]);
    const panel = container.querySelector(".panel");

    const header = `
      <header>
        <h2>Flagged Messages</h2>
        <div class="subtitle">Recent messages the scorer rated strongly negative</div>
      </header>`;

    if (!d.scored_count) {
      panel.innerHTML = header + renderEmpty(
        "No messages have been scored yet. Scoring runs on new messages, so this "
        + "fills in after a few days of conversation.",
      );
      return;
    }

    const msgs = feed.messages || [];

    const about = `
      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          Every message is scored from &minus;1 to +1 by an automated sentiment
          model. Anything at &minus;0.5 or below is listed here, newest first, so
          you can go and read it in context. <strong>A flag is not a verdict</strong>
          — the model has no idea who is joking, who is venting about their day,
          or who is quoting someone else, and all three score negative. Most of
          what lands here needs nothing doing. It is a reading queue, not a
          moderation queue: rule-breaking is <a href="/#/rules-watch">Rules Watch</a>'s
          job. To search by author, channel, date or a narrower score range, use
          <a href="/#/message-search">Message Search</a>.
        </div>
      </details>`;

    // Deliberately a count of the queue, not a score out of it — "how much is
    // there to read" is answerable and useful; "how happy is the server" was
    // the thing nobody could act on.
    const context = `
      <div class="home-dim" style="margin-bottom:12px;">
        <strong>${feed.negative_24h}</strong> flagged in the last 24 hours ·
        showing the ${msgs.length} most recent ·
        ${d.scored_count.toLocaleString()} messages scored in total
      </div>`;

    if (!msgs.length) {
      panel.innerHTML = header + about + context + renderEmpty(
        "Nothing is currently flagged. Messages appear here when the scorer rates "
        + "them −0.5 or lower.",
      );
      return;
    }

    // Grouped by channel because that is how you'd act on it — you go and read
    // one room, rather than hopping between five of them in timestamp order.
    const byChannel = new Map();
    for (const m of msgs) {
      const name = m.channel_name ? "#" + m.channel_name : "(unknown channel)";
      if (!byChannel.has(name)) byChannel.set(name, []);
      byChannel.get(name).push(m);
    }
    const groups = [...byChannel.entries()].sort((a, b) => b[1].length - a[1].length);

    function msgRow(m) {
      const score = m.sentiment.toFixed(2);
      const content = esc((m.content || "").slice(0, 160));
      return `<tr>
        <td style="color:var(--red-text);font-weight:600;white-space:nowrap;">${score}</td>
        <td class="sf-panel-author">${esc(m.author_name || m.author_id || "")}</td>
        <td class="sf-panel-content" title="${esc(m.content || "")}">${content}</td>
        <td style="white-space:nowrap;color:var(--ink-dim);font-size:12px;">${fmtTime(m.ts)}</td>
        <td style="white-space:nowrap;"><a href="${esc(jumpLink(m.channel_id, m.message_id))}"
          target="_blank" rel="noopener noreferrer">Jump</a></td>
      </tr>`;
    }

    const groupHTML = groups.map(([name, rows]) => `
      <div class="home-card" style="margin-top:14px;">
        <div class="home-card-label">${esc(name)} <span class="home-card-sub">(${rows.length})</span></div>
        <div class="data-table-scroll"><table class="data-table">
          <thead><tr><th>Score</th><th>Author</th><th>Message</th><th>Time</th><th></th></tr></thead>
          <tbody>${rows.map(msgRow).join("")}</tbody>
        </table></div>
      </div>`).join("");

    panel.innerHTML = header + about + context + groupHTML;
  }

  // Bots are excluded from every metric by default; this is the per-report
  // opt-in. Re-injected after each render because load() rewrites the panel.
  function decorate() {
    mountBotToggle(container, includeBots, (v) => {
      includeBots = v;
      reload();
    });
  }

  // Every pass is guarded, not just the first — see mountReloadable.
  const reload = mountReloadable(container, {
    load, decorate, renderError, describe: "flagged messages",
  });

  // No charts and no timers any more, so there is nothing to tear down; the
  // dev unmount tripwire only counts setInterval/ResizeObserver registrations.
}
