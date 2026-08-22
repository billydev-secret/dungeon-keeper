import { api } from "./api.js";
import { apiPost, esc, loadChannels, mountChannelPicker } from "./config-helpers.js";
import { toast } from "./ui.js";

// Renders one panel's "post it into a channel" control inside that feature's
// own config page.
//
// These all lived together on a Channel Panels page until 2026-07-28. One page
// listing seven unrelated panels meant an admin configuring the perk shop had
// to leave the Sinks page to place the shop panel, which is the jumble
// CLAUDE.md says to reorganize rather than append to. The page is gone; each
// control now sits with the settings that govern it.
//
// What did NOT move is the registry behind it: bot_modules/services/
// panel_registry.py still declares every panel and POST /api/panels/{key}/post
// still posts it. Seven bespoke endpoints would be seven chances to get the
// channel check or the permission check subtly different — this keeps one
// tested posting path and one place a new panel is declared, and changes only
// where the control is drawn.

// One /api/panels fetch per page load, shared by pages hosting several posters
// (Economy → Settings renders three). A failed fetch isn't cached, so a
// transient error doesn't poison the rest of the session.
let _specsPromise = null;

function loadPanelSpecs() {
  if (!_specsPromise) {
    _specsPromise = api("/api/panels").then((d) => d.panels || []);
    _specsPromise.catch(() => { _specsPromise = null; });
  }
  return _specsPromise;
}

// Called by app.js's applyMeData — on boot, and on every guild switch. The
// payload isn't guild-independent: a spec's grant_role choices are resolved
// from the active guild's config, and switchGuild re-mounts panels without
// reloading the page, so the memo has to go with the old guild.
export function _resetPanelSpecCache() { _specsPromise = null; }

function isAdmin() {
  return !!window.__dk_user?.perms?.has?.("admin");
}

/**
 * Draw the post control for one registry panel into `slotEl`.
 *
 * @param {Element} slotEl   Where to render. Emptied first.
 * @param {string}  key      Registry key, e.g. "economy-panel".
 * @param {object}  opts
 * @param {string}  [opts.heading]     Section label. Defaults to the spec's label.
 * @param {string}  [opts.buttonLabel] Button text. Defaults to "Post".
 * @param {Function} [opts.getOptions] Returns the spec's option values from
 *   controls the host page already owns, instead of this helper drawing its
 *   own. Grant Audit uses it: the page's Grant Role / Minimum Level selectors
 *   are the same two options the spec declares, and rendering a second pair
 *   would let the card audit something other than the table beneath it.
 */
export async function mountPanelPoster(slotEl, key, opts = {}) {
  const { heading = null, buttonLabel = "Post", getOptions = null } = opts;

  // Posting is admin-only server-side, but three host pages (Grant Audit,
  // Tickets, Guess Who) are moderator-visible. Show the control locked rather
  // than hidden, matching how the nav renders admin-only pages for moderators
  // — a moderator who can see the section knows it exists and who to ask.
  if (!isAdmin()) {
    slotEl.innerHTML = `
      <div class="panel-post" data-panel-post="${esc(key)}" data-locked>
        <div class="section-label">${esc(heading || "Post to Discord")}</div>
        <div class="field-hint">Posting this panel is an admin-only action.</div>
        <div class="panel-post__actions">
          <button class="btn" type="button" disabled>${esc(buttonLabel)}</button>
        </div>
      </div>`;
    return null;
  }

  slotEl.innerHTML = `<div class="panel-post"><div class="field-hint">Loading…</div></div>`;

  let spec, channels;
  try {
    const [specs, chans] = await Promise.all([loadPanelSpecs(), loadChannels()]);
    spec = specs.find((p) => p.key === key);
    channels = chans;
  } catch (err) {
    slotEl.innerHTML = `<div class="panel-post"><div class="error">Couldn't load the
      post control — ${esc(err.message)}. Reload the page to try again.</div></div>`;
    return null;
  }
  if (!spec) {
    // A key removed from the registry without its host page being updated.
    slotEl.innerHTML = `<div class="panel-post"><div class="error">No panel registered
      as "${esc(key)}".</div></div>`;
    return null;
  }

  // Panels that own their destination (Voice Control's control channel, Guess
  // Who's game channel) get no picker: their buttons drive a flow the cog only
  // watches in that one channel, so posting elsewhere would strand them.
  const pickerHtml = spec.targets_own_channel
    ? `<div class="field-hint">Goes to the channel configured above — there is nothing to pick.</div>`
    : `<div data-channel-slot></div>`;
  const optionsHtml = getOptions ? "" : renderOptions(spec.options);

  slotEl.innerHTML = `
    <div class="panel-post" data-panel-post="${esc(spec.key)}">
      <div class="section-label">${esc(heading || spec.label)}</div>
      <div class="field-hint">${esc(spec.description)}</div>
      ${optionsHtml}
      <div class="panel-post__actions">
        ${pickerHtml}
        <button class="btn" type="button" data-post>${esc(buttonLabel)}</button>
        <span data-status class="field-hint"></span>
      </div>
    </div>`;

  const root = slotEl.querySelector(".panel-post");
  const slot = root.querySelector("[data-channel-slot]");
  const picker = slot
    ? mountChannelPicker(slot, channels, "0", { label: `${spec.label} channel` })
    : null;
  const status = root.querySelector("[data-status]");

  root.querySelector("[data-post]").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const channelId = picker ? picker.getValue() : null;
    if (!spec.targets_own_channel && (!channelId || channelId === "0")) {
      toast("Pick a channel first.", "error");
      return;
    }
    let options = {};
    if (getOptions) {
      // A host page refuses by throwing — its controls aren't ready or aren't
      // filled in. Say so and stop; letting it escape an async click handler
      // would surface as a console error and no explanation.
      try {
        options = getOptions() || {};
      } catch (err) {
        toast(err.message, "error");
        return;
      }
    } else {
      root.querySelectorAll("[data-opt]").forEach((el) => {
        options[el.dataset.opt] = el.value;
      });
    }
    btn.disabled = true;
    status.textContent = "Posting…";
    try {
      const res = await apiPost(`/api/panels/${encodeURIComponent(spec.key)}/post`, {
        // Snowflakes stay strings end to end; the picker never parses them.
        channel_id: spec.targets_own_channel ? null : channelId,
        options,
      });
      status.innerHTML = res.message_url
        ? `<a href="${esc(res.message_url)}" target="_blank" rel="noopener">Posted — open in Discord</a>`
        : "Posted.";
      // A survivable sticky collision: the post went through, but two panels
      // are now fighting over one bottom slot and the admin should know.
      if (res.warning) {
        status.innerHTML += `<div class="field-hint">${esc(res.warning)}</div>`;
        toast(res.warning, "info");
      }
    } catch (err) {
      status.textContent = "";
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  });

  return { spec };
}

// Only the grant-audit card declares options, and it supplies them via
// getOptions from the controls already on its page — so this renders nothing
// today. It stays because the registry can declare options on any spec, and a
// host page that doesn't already own the controls needs them drawn.
function renderOptions(options) {
  if (!options || !options.length) return "";
  return `<div class="panel-post__options">${options.map((o) => {
    const control = o.kind === "grant_role"
      ? `<select data-opt="${esc(o.name)}">${(o.choices || []).map((c) =>
          `<option value="${esc(c.value)}"${c.value === o.default ? " selected" : ""}>${esc(c.label)}</option>`
        ).join("")}</select>`
      : `<input type="number" data-opt="${esc(o.name)}" value="${esc(String(o.default))}"
           ${o.minimum != null ? `min="${esc(String(o.minimum))}"` : ""}>`;
    const empty = o.kind === "grant_role" && !(o.choices || []).length
      ? `<div class="field-hint">No grant roles configured yet.</div>` : "";
    return `<label class="panel-opt">
        <span>${esc(o.label)}</span>${control}
        ${o.hint ? `<span class="field-hint">${esc(o.hint)}</span>` : ""}${empty}
      </label>`;
  }).join("")}</div>`;
}
