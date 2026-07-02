import { api, fmtTs } from "./api.js";

let backdrop = null;
let modal = null;
let loading = false;
let _prevFocus = null;

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

const TYPE_LABELS = {
  ticket: "Ticket",
  jail: "Jail",
  policy_ticket: "Policy Ticket",
};

function buildDom() {
  if (backdrop) return;

  backdrop = document.createElement("div");
  backdrop.className = "transcript-backdrop";
  backdrop.style.display = "none";
  backdrop.addEventListener("click", close);

  modal = document.createElement("div");
  modal.className = "transcript-modal";
  modal.style.display = "none";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-labelledby", "transcript-title");
  modal.setAttribute("tabindex", "-1");
  modal.innerHTML = `
    <div class="transcript-header">
      <h3 data-title id="transcript-title"></h3>
      <button class="transcript-close" data-close aria-label="Close transcript">&times;</button>
    </div>
    <div class="transcript-meta-bar" data-meta></div>
    <div class="transcript-body" data-body>
      <div class="transcript-loading">Loading transcript...</div>
    </div>
  `;
  modal.querySelector("[data-close]").addEventListener("click", close);

  document.body.appendChild(backdrop);
  document.body.appendChild(modal);
}

function onKeyDown(e) {
  if (e.key === "Escape") { close(); return; }
  if (e.key !== "Tab" || !modal) return;
  const items = Array.from(
    modal.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    ),
  ).filter(el => el.offsetParent !== null && !el.disabled);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function open() {
  buildDom();
  _prevFocus = document.activeElement;
  backdrop.style.display = "";
  modal.style.display = "";
  document.addEventListener("keydown", onKeyDown);
  const closeBtn = modal.querySelector("[data-close]");
  (closeBtn || modal).focus();
}

function close() {
  if (!backdrop) return;
  backdrop.style.display = "none";
  modal.style.display = "none";
  document.removeEventListener("keydown", onKeyDown);
  loading = false;
  if (_prevFocus && typeof _prevFocus.focus === "function") {
    _prevFocus.focus();
    _prevFocus = null;
  }
}

function renderMessages(messages) {
  return messages.map((m) => {
    let html = `<div class="transcript-msg">
      <div class="transcript-msg-header">
        <span class="transcript-msg-author">${esc(m.author_name || String(m.author_id))}</span>
        <span class="transcript-msg-time">${fmtTs(m.timestamp)}</span>
      </div>`;

    if (m.content) {
      html += `<div class="transcript-msg-content">${esc(m.content)}</div>`;
    }

    if (m.embeds && m.embeds.length) {
      for (const embed of m.embeds) {
        html += `<div class="transcript-msg-embed">`;
        if (embed.title) html += `<div class="transcript-msg-embed-title">${esc(embed.title)}</div>`;
        if (embed.description) html += `<div>${esc(embed.description)}</div>`;
        html += `</div>`;
      }
    }

    if (m.attachments && m.attachments.length) {
      for (const att of m.attachments) {
        html += `<div class="transcript-msg-attachment">
          <a href="${esc(att.url)}" target="_blank" rel="noopener">${esc(att.filename || "attachment")}</a>
        </div>`;
      }
    }

    html += `</div>`;
    return html;
  }).join("");
}

export async function showTranscript(recordType, recordId) {
  if (loading) return;
  loading = true;

  buildDom();
  const titleEl = modal.querySelector("[data-title]");
  const metaEl = modal.querySelector("[data-meta]");
  const bodyEl = modal.querySelector("[data-body]");

  const label = TYPE_LABELS[recordType] || recordType;
  titleEl.textContent = `${label} #${recordId} — Transcript`;
  metaEl.innerHTML = "";
  bodyEl.innerHTML = `<div class="transcript-loading">Loading transcript...</div>`;

  open();

  try {
    const data = await api("/api/moderation/transcript", {
      record_type: recordType,
      record_id: recordId,
    });

    if (!data.transcript) {
      bodyEl.innerHTML = `<div class="transcript-empty">No transcript available for this record.</div>`;
      loading = false;
      return;
    }

    const t = data.transcript;

    // Build metadata bar
    const meta = [];
    if (t.channel_name) meta.push(`<span>Channel: <strong>${esc(t.channel_name)}</strong></span>`);
    if (t.message_count != null) meta.push(`<span>${t.message_count} messages</span>`);
    if (t.created_at) meta.push(`<span>Created: ${fmtTs(t.created_at)}</span>`);
    if (t.reason) meta.push(`<span>Reason: ${esc(t.reason)}</span>`);
    if (t.close_reason) meta.push(`<span>Close reason: ${esc(t.close_reason)}</span>`);
    if (t.duration_served) meta.push(`<span>Duration: ${esc(t.duration_served)}</span>`);
    metaEl.innerHTML = meta.join("");

    // Update title with channel name if available
    if (t.channel_name) {
      titleEl.textContent = `${label} #${recordId} — #${t.channel_name}`;
    }

    // Render messages
    if (t.messages && t.messages.length) {
      bodyEl.innerHTML = renderMessages(t.messages);
    } else {
      bodyEl.innerHTML = `<div class="transcript-empty">Transcript is empty (no messages recorded).</div>`;
    }
  } catch (err) {
    bodyEl.innerHTML = `<div class="transcript-empty">Error loading transcript: ${esc(err.message)}</div>`;
  }

  loading = false;
}
