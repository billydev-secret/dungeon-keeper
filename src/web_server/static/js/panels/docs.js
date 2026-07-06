import { api, apiPost, apiPut, apiDelete, esc, fmtTs } from "../api.js";
import { loadChannels, channelName, mountChannelPicker, showStatus } from "../config-helpers.js";
import { renderLoading, renderEmpty } from "../states.js";

// ── lightweight markdown → HTML, just for the editor preview ──────────
// Discord does the real rendering; this only needs to look close enough that
// the author can see structure. Escapes first, so raw HTML never executes.
function mdInline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/__([^_]+)__/g, "<b>$1</b>")
    .replace(/~~([^~]+)~~/g, "<s>$1</s>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$1" onclick="return false">$1</a>');
}

function mdToHtml(text) {
  const lines = (text || "").split("\n");
  const out = [];
  let i = 0;
  let listOpen = null;
  const closeList = () => { if (listOpen) { out.push(`</${listOpen}>`); listOpen = null; } };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*(```|~~~)(.*)$/);
    if (fence) {
      closeList();
      const marker = fence[1];
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) { buf.push(lines[i]); i++; }
      i++;
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      out.push(`<div class="dp-h dp-h${level}">${mdInline(heading[2])}</div>`);
      i++;
      continue;
    }
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      closeList();
      out.push(`<blockquote>${mdInline(quote[1])}</blockquote>`);
      i++;
      continue;
    }
    const ul = line.match(/^\s*[-*]\s+(.+)$/);
    const ol = line.match(/^\s*\d+\.\s+(.+)$/);
    if (ul || ol) {
      const want = ul ? "ul" : "ol";
      if (listOpen && listOpen !== want) closeList();
      if (!listOpen) { listOpen = want; out.push(`<${want}>`); }
      out.push(`<li>${mdInline((ul || ol)[1])}</li>`);
      i++;
      continue;
    }
    if (line.trim() === "") { closeList(); out.push("<br>"); i++; continue; }
    closeList();
    out.push(`<div>${mdInline(line)}</div>`);
    i++;
  }
  closeList();
  return out.join("");
}

function renderList(docs, activeKey) {
  if (!docs.length) return renderEmpty("No docs yet. Create one to get started.");
  return docs.map((d) => {
    const cls = d.doc_key === activeKey ? " active" : "";
    const posted = d.placements.length
      ? `${d.placements.length} channel${d.placements.length === 1 ? "" : "s"}`
      : "not posted";
    return `
      <div class="ticket-item med${cls}" data-doc-key="${esc(d.doc_key)}">
        <div class="pri"></div>
        <div class="body">
          <div class="subj">${esc(d.title || d.doc_key)}</div>
          <div class="row"><span>${esc(d.doc_key)}</span><span>${esc(posted)}</span></div>
        </div>
      </div>`;
  }).join("");
}

function renderPreview(embeds, accent) {
  if (!embeds || !embeds.length) return '<div class="empty" style="padding:16px">Nothing to preview.</div>';
  const bar = accent && /^#?[0-9a-fA-F]{6}$/.test(accent) ? (accent[0] === "#" ? accent : "#" + accent) : "var(--accent, #E6B84C)";
  return embeds.map((e) => `
    <div class="dp-embed" style="border-left-color:${esc(bar)}">
      ${e.title ? `<div class="dp-title">${esc(e.title)}</div>` : ""}
      <div class="dp-desc">${mdToHtml(e.description)}</div>
    </div>`).join("");
}

function syncSummary(sync) {
  if (!sync || !sync.length) return "";
  const bad = sync.filter((s) => s.status !== "ok");
  if (bad.length) {
    return "⚠️ " + bad.map((s) => `#${channelName([], s.channel_id) || s.channel_id}: ${s.detail || s.status}`).join(" · ");
  }
  const total = sync.reduce((a, s) => a + s.created + s.edited + s.deleted, 0);
  return `Synced ${sync.length} channel${sync.length === 1 ? "" : "s"} (${total} message${total === 1 ? "" : "s"}).`;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Docs</h2>
        <div class="subtitle">Write rules, FAQs, staff bios once in markdown; post &amp; keep them synced across channels via <code>/docs</code>.</div>
      </header>
      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>Documents</h3>
            <button class="act-btn" data-new-btn>New</button>
          </div>
          <div class="ticket-list" data-list>${renderLoading("Loading…")}</div>
        </div>
        <div class="ticket-detail" data-editor>
          <div class="empty" style="padding:24px">Select a document, or create one.</div>
        </div>
      </section>
    </div>`;

  const listEl = container.querySelector("[data-list]");
  const editorEl = container.querySelector("[data-editor]");
  const newBtn = container.querySelector("[data-new-btn]");

  const state = { docs: [], activeKey: null, doc: null, channels: [], saving: false, previewTimer: null };

  loadChannels().then((chs) => { state.channels = chs || []; });

  function renderDocList() {
    listEl.innerHTML = renderList(state.docs, state.activeKey);
  }

  async function refreshList() {
    try {
      const data = await api("/api/docs");
      state.docs = data.docs || [];
      renderDocList();
    } catch (err) {
      listEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
    }
  }

  async function selectDoc(key) {
    state.activeKey = key;
    renderDocList();
    editorEl.innerHTML = renderLoading("Loading…");
    try {
      state.doc = await api(`/api/docs/${encodeURIComponent(key)}`);
      renderEditor();
    } catch (err) {
      editorEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
    }
  }

  function currentInputs() {
    return {
      title: editorEl.querySelector("[data-title]")?.value ?? "",
      accent: editorEl.querySelector("[data-accent]")?.value ?? "",
      body_md: editorEl.querySelector("[data-body]")?.value ?? "",
    };
  }

  async function refreshPreview() {
    const previewEl = editorEl.querySelector("[data-preview]");
    if (!previewEl) return;
    const { title, body_md, accent } = currentInputs();
    try {
      const data = await apiPost("/api/docs/preview", { title, body_md });
      previewEl.innerHTML = renderPreview(data.embeds, accent);
    } catch (_) { /* preview is best-effort */ }
  }

  function schedulePreview() {
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(refreshPreview, 300);
  }

  function renderPlacements(doc) {
    if (!doc.placements.length) return '<div class="field-hint" style="padding:4px 0">Not posted anywhere yet.</div>';
    return doc.placements.map((p) => `
      <div class="doc-place-row">
        <span class="doc-place-ch">#${esc(channelName(state.channels, p.channel_id) || p.channel_id)}</span>
        <span class="doc-place-count">${p.message_count} msg${p.message_count === 1 ? "" : "s"}</span>
        <button class="doc-x" data-remove-ch="${esc(p.channel_id)}" title="Remove from channel">✕</button>
      </div>`).join("");
  }

  function renderEditor() {
    const doc = state.doc;
    if (!doc) return;
    editorEl.innerHTML = `
      <div class="doc-ed">
        <div class="doc-ed-head">
          <input class="doc-ed-title" data-title type="text" maxlength="200"
                 placeholder="Document title" value="${esc(doc.title)}" />
          <span class="doc-key-badge">${esc(doc.doc_key)}</span>
          <input class="doc-ed-accent" data-accent type="text" maxlength="7"
                 placeholder="#hex" value="${esc(doc.accent)}" title="Accent colour (blank = server branding)" />
        </div>
        <div class="doc-ed-grid">
          <div class="doc-ed-col">
            <label class="doc-ed-lbl">Markdown source
              <span class="field-hint" style="font-weight:400">A line of <code>---</code> starts a new message. A leading <code>#</code> heading becomes the embed title.</span>
            </label>
            <textarea class="doc-ed-area" data-body spellcheck="true" placeholder="# Server Rules&#10;&#10;1. Be kind.&#10;&#10;---&#10;&#10;## FAQ&#10;...">${esc(doc.body_md)}</textarea>
            <div class="doc-ed-actions">
              <button class="act-btn" data-save>Save &amp; sync</button>
              <span class="save-status" data-status></span>
              <span class="act-spacer" style="flex:1"></span>
              <button class="doc-danger" data-delete>Delete</button>
            </div>
          </div>
          <div class="doc-ed-col">
            <label class="doc-ed-lbl">Preview</label>
            <div class="doc-preview" data-preview></div>
          </div>
        </div>
        <div class="doc-placements">
          <div class="doc-place-head">
            <h4>Posted in</h4>
            <button class="act-btn ghost" data-sync>Re-sync all</button>
          </div>
          <div data-place-list>${renderPlacements(doc)}</div>
          <div class="doc-add-row">
            <span data-ch-picker></span>
            <button class="act-btn" data-add-place>Post to channel</button>
          </div>
        </div>
      </div>`;

    const picker = mountChannelPicker(
      editorEl.querySelector("[data-ch-picker]"), state.channels, "0",
      { placeholder: "Pick a channel…" });
    editorEl._picker = picker;

    editorEl.querySelector("[data-body]").addEventListener("input", schedulePreview);
    editorEl.querySelector("[data-title]").addEventListener("input", schedulePreview);
    editorEl.querySelector("[data-accent]").addEventListener("input", schedulePreview);
    refreshPreview();
  }

  // ── list + new-doc interactions ────────────────────────────────────

  listEl.addEventListener("click", (e) => {
    const row = e.target.closest(".ticket-item");
    if (!row) return;
    selectDoc(row.dataset.docKey);
  });

  newBtn.addEventListener("click", async () => {
    const key = prompt("Key for the new doc (letters, digits, dashes) — e.g. rules, mod-faq:");
    if (key == null) return;
    const slug = key.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    if (!slug) { alert("Key must contain letters or digits."); return; }
    try {
      const doc = await apiPost("/api/docs", { doc_key: slug, title: key.trim(), body_md: "", accent: "" });
      await refreshList();
      selectDoc(doc.doc_key);
    } catch (err) {
      alert(err.message);
    }
  });

  // ── editor interactions (delegated) ────────────────────────────────

  editorEl.addEventListener("click", async (e) => {
    const doc = state.doc;
    if (!doc) return;
    const statusEl = editorEl.querySelector("[data-status]");

    if (e.target.closest("[data-save]")) {
      if (state.saving) return;
      state.saving = true;
      const btn = e.target.closest("[data-save]");
      btn.disabled = true;
      const { title, accent, body_md } = currentInputs();
      try {
        const res = await apiPut(`/api/docs/${encodeURIComponent(doc.doc_key)}`, { title, accent, body_md });
        state.doc = res.doc;
        const summary = syncSummary(res.sync);
        showStatus(statusEl, true, summary ? `Saved — ${summary}` : "Saved");
        editorEl.querySelector("[data-place-list]").innerHTML = renderPlacements(res.doc);
        await refreshList();
      } catch (err) {
        showStatus(statusEl, false, err.message);
      } finally {
        state.saving = false;
        btn.disabled = false;
      }
      return;
    }

    if (e.target.closest("[data-delete]")) {
      if (!confirm(`Delete "${doc.title || doc.doc_key}"? This removes its posted messages too.`)) return;
      try {
        await apiDelete(`/api/docs/${encodeURIComponent(doc.doc_key)}`);
        state.doc = null;
        state.activeKey = null;
        editorEl.innerHTML = '<div class="empty" style="padding:24px">Document deleted.</div>';
        await refreshList();
      } catch (err) {
        alert(err.message);
      }
      return;
    }

    if (e.target.closest("[data-sync]")) {
      const btn = e.target.closest("[data-sync]");
      btn.disabled = true;
      try {
        const res = await apiPost(`/api/docs/${encodeURIComponent(doc.doc_key)}/sync`, {});
        showStatus(statusEl, true, syncSummary(res.sync) || "Nothing posted yet.");
      } catch (err) {
        showStatus(statusEl, false, err.message);
      } finally { btn.disabled = false; }
      return;
    }

    if (e.target.closest("[data-add-place]")) {
      const channelId = editorEl._picker ? editorEl._picker.getValue() : "0";
      if (!channelId || channelId === "0") { showStatus(statusEl, false, "Pick a channel first."); return; }
      const btn = e.target.closest("[data-add-place]");
      btn.disabled = true;
      try {
        const res = await apiPost(`/api/docs/${encodeURIComponent(doc.doc_key)}/placements`, { channel_id: channelId });
        showStatus(statusEl, res.ok, syncSummary([res.sync]) || (res.ok ? "Posted." : res.sync.detail));
        await selectDoc(doc.doc_key);
        await refreshList();
      } catch (err) {
        showStatus(statusEl, false, err.message);
        btn.disabled = false;
      }
      return;
    }

    const removeCh = e.target.closest("[data-remove-ch]");
    if (removeCh) {
      const chId = removeCh.dataset.removeCh;
      if (!confirm("Remove this doc from that channel? Its messages there will be deleted.")) return;
      try {
        await apiDelete(`/api/docs/${encodeURIComponent(doc.doc_key)}/placements/${chId}`);
        await selectDoc(doc.doc_key);
        await refreshList();
      } catch (err) {
        showStatus(statusEl, false, err.message);
      }
    }
  });

  refreshList();

  return { unmount() { clearTimeout(state.previewTimer); } };
}
