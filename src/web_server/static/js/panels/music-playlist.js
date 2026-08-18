// Config — Music Playlist. The watched-channel → rolling-Spotify-playlist
// feature's whole admin surface: the connection chip (the stored grant's
// shape, so a read-only token names itself instead of surfacing as a bare
// 403), the watch/behavior dials, the live window, the unmatched review
// queue, history with removal reasons, and the two maintenance actions.
// Everything talks to /api/music-playlist/* (routes/music_playlist.py).
import { api, esc, fmtAge } from "../api.js";
import {
  apiDelete,
  apiPost,
  apiPut,
  checkbox,
  field,
  guardForm,
  loadChannels,
  memberNames,
  mountAsync,
  mountChannelPicker,
  numInput,
  renderMetaWarning,
  sectionCard,
  showStatus,
} from "../config-helpers.js";
import { renderSortableTable } from "../table.js";
import { confirmDialog, toast } from "../ui.js";

// The three shapes connection_status() reports. Wording matches the spec —
// "Read-only, needs re-consent" is the whole diagnosis, not a code.
const CONNECTION = {
  connected: { label: "Connected", cls: "chip chip-success", btn: "Re-connect" },
  read_only: { label: "Read-only, needs re-consent", cls: "chip chip-warning", btn: "Re-consent" },
  not_connected: { label: "Not connected", cls: "chip chip-danger", btn: "Connect Spotify" },
};

const REASONS = {
  rolled_off: { label: "Rolled off", cls: "badge badge-dim" },
  message_deleted: { label: "Message deleted", cls: "badge badge-warning" },
  admin: { label: "Removed by admin", cls: "badge" },
};

function nowSec() { return Date.now() / 1000; }

// Client-side twin of the route's playlist-id normalization, so pasting a
// link to the *same* playlist doesn't trip the change warning.
function extractPlaylistId(raw) {
  const value = String(raw || "").trim();
  const m = value.match(/(?:open\.spotify\.com\/playlist\/|spotify:playlist:)([A-Za-z0-9]+)/);
  return m ? m[1] : value;
}

function ageCell(ts) {
  return ts ? `${fmtAge(nowSec() - ts)} ago` : "—";
}

// Source URLs are member-posted text. Only link http(s); label by host so the
// cell reads "open.spotify.com" / "youtube.com" instead of a full URL.
function sourceCell(url) {
  if (!url) return "—";
  if (!/^https?:\/\//i.test(url)) return esc(url);
  let host;
  try {
    host = new URL(url).hostname.replace(/^www\./, "");
  } catch (_) {
    return esc(url);
  }
  return `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(host)}</a>`;
}

function reasonCell(reason) {
  const r = REASONS[reason] || { label: reason || "—", cls: "badge badge-dim" };
  return `<span class="${esc(r.cls)}">${esc(r.label)}</span>`;
}

// Maintenance responses are small dicts ({ok, scanned, added, …}); show the
// counts without hard-coding which keys a given action reports.
function summarize(result) {
  const parts = Object.entries(result || {})
    .filter(([k, v]) => k !== "ok" && (typeof v === "number" || typeof v === "string"))
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`);
  return parts.length ? parts.join(", ") : "Done";
}

function errorBox(host, err) {
  host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
}

// Names for the member ids a payload references, departed members included
// (memberNames reaches past the bounded roster page for exact ids).
async function nameLookup(rows, key = "added_by") {
  try {
    return await memberNames(rows.map((r) => r[key]));
  } catch (_) {
    return (id) => String(id); // ids still render; names arrive on refresh
  }
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Music Playlist…</div></div>`;
  return mountAsync(container, async () => {
    const [status, channels] = await Promise.all([
      api("/api/music-playlist/status"),
      loadChannels(),
    ]);
    render(container, status, channels);
  }, { errorMsg: "Couldn’t load the Music Playlist settings." });
}

function render(container, status, channels) {
  const s = status.settings || {};
  // Tracks the last-saved playlist id across saves in this panel session,
  // for the change warning below.
  let savedPlaylistId = s.playlist_id || "";

  container.textContent = "";
  const panel = document.createElement("div");
  panel.className = "panel";

  const hdr = document.createElement("header");
  const h2 = document.createElement("h2");
  h2.textContent = "Music Playlist";
  const sub = document.createElement("div");
  sub.className = "subtitle";
  sub.textContent =
    "One watched channel becomes a rolling Spotify playlist: music links " +
    "posted there are resolved to tracks, deduped, and kept as the last " +
    "N songs.";
  hdr.append(h2, sub);
  panel.appendChild(hdr);

  const warning = renderMetaWarning();
  if (warning) {
    const w = document.createElement("div");
    w.innerHTML = warning;
    panel.appendChild(w.firstElementChild);
  }

  // ── Connection ───────────────────────────────────────────────────────
  const conn = CONNECTION[status.connection] || CONNECTION.not_connected;
  const cardConn = sectionCard(panel, "Connection");
  const connRow = document.createElement("div");
  connRow.style.cssText = "display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:6px 0;";
  connRow.innerHTML =
    `<span class="${esc(conn.cls)}">${esc(conn.label)}</span>` +
    // A real navigation, not a fetch: /spotify/authorize redirects to
    // Spotify's consent page. New tab so the panel survives the round trip.
    `<a class="btn btn-primary" href="/spotify/authorize" target="_blank" rel="noopener">${esc(conn.btn)}</a>`;
  cardConn.appendChild(connRow);
  const connHint = document.createElement("div");
  connHint.className = "field-hint";
  connHint.textContent =
    "The playlist is written with the bot owner's Spotify account. " +
    "Read-only means the stored grant predates the write scopes — click " +
    "through the consent page once more, then reload this page. Nothing " +
    "below can write to Spotify until the chip says Connected.";
  cardConn.appendChild(connHint);

  // ── Watch + Behavior (one form, one Save) ────────────────────────────
  const form = document.createElement("form");
  form.className = "form form-cards";
  panel.appendChild(form);

  const cardWatch = sectionCard(form, "Watch");
  const chanSlot = document.createElement("span");
  cardWatch.appendChild(field(
    "Watched Channel",
    chanSlot,
    "The one channel whose music links feed the playlist. \"(disabled)\" " +
      "stops watching entirely.",
  ));
  const chanPicker = mountChannelPicker(
    chanSlot, channels, String(s.channel_id || "0"),
    { emptyValue: "0", emptyLabel: "(disabled)", label: "Watched Channel" },
  );
  const playlistInput = document.createElement("input");
  playlistInput.type = "text";
  playlistInput.name = "playlist_id";
  playlistInput.value = s.playlist_id || "";
  playlistInput.placeholder = "Playlist link, spotify:playlist: URI, or bare id";
  playlistInput.style.maxWidth = "420px";
  cardWatch.appendChild(field(
    "Target Playlist",
    playlistInput,
    "The Spotify playlist the window is written to. Paste an " +
      "open.spotify.com/playlist link — it's stored as the bare id. Leave " +
      "empty to clear. Changing it does not move anything: songs already " +
      "added stay in the old playlist, and already-processed messages " +
      "never follow the new one.",
  ));
  const watchToggles = document.createElement("div");
  watchToggles.style.cssText = "display:flex; flex-wrap:wrap; gap:8px 16px;";
  watchToggles.append(checkbox("enabled", s.enabled === true, "Watch the channel"));
  cardWatch.appendChild(field(
    "Enable",
    watchToggles,
    "Unchecked pauses the watcher without losing any settings. Links " +
      "posted while paused are only picked up by a later Re-scan.",
  ));

  const cardBehavior = sectionCard(form, "Behavior");
  cardBehavior.appendChild(field(
    "Window Size",
    numInput("window_size", s.window_size ?? 30, 1, "1", 200),
    "How many songs the rolling playlist holds. Song N+1 pushes the " +
      "oldest one out. Between 1 and 200; 30 is the default.",
  ));
  const behaviorToggles = document.createElement("div");
  behaviorToggles.style.cssText = "display:flex; flex-wrap:wrap; gap:8px 16px;";
  behaviorToggles.append(
    checkbox("remove_on_delete", s.remove_on_delete !== false, "Remove when the message is deleted"),
  );
  cardBehavior.appendChild(field(
    "Re-scan Depth",
    numInput("rescan_depth", s.rescan_depth ?? 200, 1, "1", 2000),
    "How far back Re-scan reads the channel. This is the only way to " +
      "recover songs the bot could not add at the time (a read-only " +
      "connection, or a Spotify outage), so raise it if writes were blocked " +
      "for longer than this many messages. Between 1 and 2000; 200 is the " +
      "default.",
  ));
  cardBehavior.appendChild(field(
    "Options",
    behaviorToggles,
    "Remove-on-delete drops a track when its source message is deleted, " +
      "unless another live message still references it. Album and playlist " +
      "links always contribute their single most popular track.",
  ));

  const saveRow = document.createElement("div");
  saveRow.style.cssText = "display:flex; gap:8px; align-items:center;";
  const saveBtn = document.createElement("button");
  saveBtn.type = "submit";
  saveBtn.className = "btn btn-primary";
  saveBtn.textContent = "Save";
  const saveStatus = document.createElement("span");
  saveRow.append(saveBtn, saveStatus);
  form.appendChild(saveRow);

  guardForm(form);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const windowSize = parseInt(String(fd.get("window_size") ?? "").trim(), 10);
    if (!Number.isFinite(windowSize) || windowSize < 1 || windowSize > 200) {
      showStatus(saveStatus, false, "Window Size must be a whole number between 1 and 200.");
      form.querySelector('[name="window_size"]').focus();
      return;
    }
    const rescanDepth = parseInt(String(fd.get("rescan_depth") ?? "").trim(), 10);
    if (!Number.isFinite(rescanDepth) || rescanDepth < 1 || rescanDepth > 2000) {
      showStatus(saveStatus, false, "Re-scan Depth must be a whole number between 1 and 2000.");
      form.querySelector('[name="rescan_depth"]').focus();
      return;
    }
    // Repointing the dial strands the back catalogue: the processed-message
    // ledger is playlist-agnostic, so already-processed messages never
    // follow, and nothing already added moves. Deliberate (2026-08-18) —
    // warned about here rather than re-keying the ledger.
    if (
      savedPlaylistId &&
      extractPlaylistId(playlistInput.value) !== savedPlaylistId
    ) {
      const ok = await confirmDialog(
        "Songs already in the current playlist stay there, and messages " +
          "the bot has already processed will never be re-added to the new " +
          "one — a Re-scan only picks up messages that never landed. " +
          "Change the target playlist?",
        { title: "Change target playlist", confirmLabel: "Change playlist" },
      );
      if (!ok) return;
    }
    try {
      const saved = await apiPut("/api/music-playlist/settings", {
        enabled: fd.has("enabled"),
        channel_id: chanPicker.getValue() || "0", // string — snowflake rule
        playlist_id: playlistInput.value.trim(),
        window_size: windowSize,
        remove_on_delete: fd.has("remove_on_delete"),
        rescan_depth: rescanDepth,
      });
      savedPlaylistId = (saved && saved.settings
        ? saved.settings.playlist_id
        : extractPlaylistId(playlistInput.value)) || "";
      showStatus(saveStatus, true);
      refreshWindow(); // a changed playlist or window size redraws the table
    } catch (err) {
      showStatus(saveStatus, false, err.message);
    }
  });

  // ── Window ───────────────────────────────────────────────────────────
  const cardWindow = sectionCard(panel, "Live Window");
  const windowLabel = cardWindow.querySelector(".section-label");
  const windowHint = document.createElement("div");
  windowHint.className = "field-hint";
  windowHint.style.marginBottom = "8px";
  windowHint.textContent =
    "The songs currently on the playlist, newest first. Removing a row " +
    "also removes the track from Spotify.";
  cardWindow.appendChild(windowHint);
  const windowHost = document.createElement("div");
  windowHost.style.overflowX = "auto";
  cardWindow.appendChild(windowHost);

  let windowRows = [];
  async function refreshWindow() {
    let payload;
    try {
      payload = await api("/api/music-playlist/window");
    } catch (err) {
      errorBox(windowHost, err);
      return;
    }
    windowRows = payload.tracks || [];
    windowLabel.textContent = `Live Window (${windowRows.length} of ${payload.window_size})`;
    const nameOf = await nameLookup(windowRows);
    for (const r of windowRows) r.added_by_name = nameOf(r.added_by);
    renderSortableTable(windowHost, {
      columns: [
        { key: "title", label: "Title" },
        { key: "artist", label: "Artist" },
        { key: "added_by_name", label: "Added by" },
        { key: "added_at", label: "Added", format: ageCell },
        { key: "source_url", label: "Source", html: true, format: sourceCell },
        // Row id is server-issued and numeric; the delegated handler below
        // survives table.js's re-render on every sort.
        { key: "id", label: "", html: true,
          format: (v) => `<button type="button" class="btn btn-ghost btn-sm" data-remove-track="${Number(v)}">Remove</button>` },
      ],
      data: windowRows,
      defaultSort: "added_at",
      defaultAsc: false,
      emptyMsg: "The window is empty — nothing has been added yet.",
    });
  }

  windowHost.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-remove-track]");
    if (!btn) return;
    const id = btn.dataset.removeTrack;
    const row = windowRows.find((r) => String(r.id) === id);
    const title = row ? `“${row.title}”` : "this track";
    const ok = await confirmDialog(
      `Remove ${title} from the playlist? It stays in History as an admin removal.`,
      { title: "Remove track", danger: true, confirmLabel: "Remove" },
    );
    if (!ok) return;
    btn.disabled = true;
    try {
      const res = await apiDelete(`/api/music-playlist/window/${id}`);
      if (res.error) {
        toast(`Removed from the window, but Spotify refused: ${res.error} — run Reconcile once the connection is fixed.`, "error");
      } else {
        toast("Removed", "success");
      }
      refreshWindow();
      refreshHistory();
    } catch (err) {
      toast(err.message, "error");
      btn.disabled = false;
    }
  });

  // ── Review queue ─────────────────────────────────────────────────────
  const cardQueue = sectionCard(panel, "Review Queue");
  const queueLabel = cardQueue.querySelector(".section-label");
  const queueHint = document.createElement("div");
  queueHint.className = "field-hint";
  queueHint.style.marginBottom = "8px";
  queueHint.textContent =
    "Links the pipeline had nothing to add for — no metadata, no Spotify " +
    "candidates, or an unreadable album/playlist. A decision here is " +
    "remembered: the same link never comes back for review. While the " +
    "connection is read-only, approvals fail until re-consent and can " +
    "simply be retried after.";
  cardQueue.appendChild(queueHint);
  const queueHost = document.createElement("div");
  queueHost.style.overflowX = "auto";
  cardQueue.appendChild(queueHost);

  async function refreshQueue() {
    let payload;
    try {
      payload = await api("/api/music-playlist/unmatched");
    } catch (err) {
      errorBox(queueHost, err);
      return;
    }
    const rows = payload.items || [];
    queueLabel.textContent = rows.length ? `Review Queue (${rows.length})` : "Review Queue";
    const nameOf = await nameLookup(rows);
    for (const r of rows) {
      r.added_by_name = nameOf(r.added_by);
      r.candidate = r.candidate_name
        ? `${r.candidate_name} — ${r.candidate_artist || "?"}`
        : "(no candidate)";
    }
    renderSortableTable(queueHost, {
      columns: [
        { key: "extracted_title", label: "Posted",
          format: (v, row) => v || row.source_url || "—" },
        { key: "candidate", label: "Best Candidate" },
        { key: "confidence", label: "Score",
          format: (v) => (v == null ? "—" : Number(v).toFixed(2)) },
        { key: "reason", label: "Reason" },
        { key: "added_by_name", label: "Posted by" },
        { key: "created_at", label: "Age", format: ageCell },
        { key: "id", label: "", html: true,
          format: (v) =>
            `<button type="button" class="btn btn-primary btn-sm" data-approve="${Number(v)}">Approve</button> ` +
            `<button type="button" class="btn btn-ghost btn-sm" data-reject="${Number(v)}">Reject</button>` },
      ],
      data: rows,
      defaultSort: "created_at",
      defaultAsc: false,
      emptyMsg: "Nothing waiting for review.",
    });
  }

  queueHost.addEventListener("click", async (e) => {
    const approve = e.target.closest("[data-approve]");
    const reject = e.target.closest("[data-reject]");
    const btn = approve || reject;
    if (!btn) return;
    const id = approve ? btn.dataset.approve : btn.dataset.reject;
    if (reject) {
      const ok = await confirmDialog(
        "Reject this candidate? The link is dropped without adding anything.",
        { title: "Reject candidate", danger: true, confirmLabel: "Reject" },
      );
      if (!ok) return;
    }
    btn.disabled = true;
    try {
      if (approve) {
        const res = await apiPost(`/api/music-playlist/unmatched/${id}/approve`, {});
        toast(res.was_duplicate ? "Already in the window — marked approved." : "Added to the playlist.", "success");
      } else {
        await apiPost(`/api/music-playlist/unmatched/${id}/reject`, {});
        toast("Rejected", "success");
      }
      refreshQueue();
      refreshWindow();
    } catch (err) {
      toast(err.message, "error");
      btn.disabled = false;
    }
  });

  // ── History ──────────────────────────────────────────────────────────
  const cardHistory = sectionCard(panel, "History");
  const historyHint = document.createElement("div");
  historyHint.className = "field-hint";
  historyHint.style.marginBottom = "8px";
  historyHint.textContent =
    "Tracks that left the window and why — rolled off the end, source " +
    "message deleted, or removed by an admin. Newest removals first.";
  cardHistory.appendChild(historyHint);
  const historyHost = document.createElement("div");
  historyHost.style.overflowX = "auto";
  cardHistory.appendChild(historyHost);

  async function refreshHistory() {
    let payload;
    try {
      payload = await api("/api/music-playlist/history");
    } catch (err) {
      errorBox(historyHost, err);
      return;
    }
    const rows = payload.history || [];
    const nameOf = await nameLookup(rows);
    for (const r of rows) r.added_by_name = nameOf(r.added_by);
    renderSortableTable(historyHost, {
      columns: [
        { key: "title", label: "Title" },
        { key: "artist", label: "Artist" },
        { key: "removal_reason", label: "Reason", html: true, format: reasonCell },
        { key: "removed_at", label: "Removed", format: ageCell },
        { key: "added_by_name", label: "Posted by" },
      ],
      data: rows,
      defaultSort: "removed_at",
      defaultAsc: false,
      emptyMsg: "No track has left the window yet.",
      maxRows: 100,
    });
  }

  // ── Maintenance ──────────────────────────────────────────────────────
  const cardMaint = sectionCard(panel, "Maintenance");
  const maintRow = document.createElement("div");
  maintRow.style.cssText = "display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; margin:6px 0;";
  const rescanBtn = document.createElement("button");
  rescanBtn.type = "button";
  rescanBtn.className = "btn";
  rescanBtn.textContent = "Re-scan Channel";
  const reconcileBtn = document.createElement("button");
  reconcileBtn.type = "button";
  reconcileBtn.className = "btn";
  reconcileBtn.textContent = "Reconcile with Spotify";
  const maintStatus = document.createElement("span");
  maintRow.append(rescanBtn, reconcileBtn, maintStatus);
  cardMaint.appendChild(maintRow);
  const maintHint = document.createElement("div");
  maintHint.className = "field-hint";
  maintHint.textContent =
    "Re-scan runs the pipeline over the watched channel's recent messages " +
    "(already-processed ones are skipped, so it's safe to repeat) — it is " +
    "also how you recover songs the bot couldn't add at the time. " +
    "Reconcile squares the actual Spotify playlist with the window shown " +
    "here, and will ask before deleting anything it doesn't recognise. " +
    "Both need the bot online.";
  cardMaint.appendChild(maintHint);

  async function runMaintenance(path, body) {
    rescanBtn.disabled = reconcileBtn.disabled = true;
    try {
      const res = await apiPost(path, body || {});
      // Reconcile withholds a bulk delete until it's confirmed: "on Spotify
      // but not in the window" also describes every song on a playlist the
      // bot didn't fill, and the removal is a real, irreversible write.
      const pending = res && res.needs_confirmation;
      if (pending) {
        const n = Number(pending.would_remove) || 0;
        const ok = window.confirm(
          `Reconcile wants to delete ${n} track(s) from the Spotify playlist ` +
          `that aren't in this window.\n\nIf this playlist has songs the bot ` +
          `didn't add, they will be permanently removed. Check the playlist ` +
          `id is the one the bot manages.\n\nDelete them?`,
        );
        if (!ok) {
          showStatus(maintStatus, true, `Left ${n} unrecognised track(s) alone.`);
          return;
        }
        rescanBtn.disabled = reconcileBtn.disabled = false;
        await runMaintenance(
          "/api/music-playlist/reconcile?confirm_removals=true", {},
        );
        return;
      }
      showStatus(maintStatus, true, summarize(res));
      refreshWindow();
      refreshQueue();
      refreshHistory();
    } catch (err) {
      showStatus(maintStatus, false, err.message);
    } finally {
      rescanBtn.disabled = reconcileBtn.disabled = false;
    }
  }
  rescanBtn.addEventListener("click", () => runMaintenance("/api/music-playlist/rescan"));
  reconcileBtn.addEventListener("click", () => runMaintenance("/api/music-playlist/reconcile"));

  container.appendChild(panel);

  refreshWindow();
  refreshQueue();
  refreshHistory();
}
