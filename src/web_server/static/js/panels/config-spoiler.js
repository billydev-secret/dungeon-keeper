import { api } from "../api.js";
import {
  loadConfig,
  loadChannels,
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountChannelMultiPicker,
  mountChannelPicker,
} from "../config-helpers.js";

const MODE_HINTS = {
  off: "Nothing happens. No images are downloaded and no checks run.",
  log: "Reports what it would remove to the log channel, but removes nothing. Run here first to see how accurate it is on your server before trusting it.",
  enforce:
    "Removes explicit images, DMs the image back to the poster, and posts a brief notice.",
};

function labelText(label) {
  return label.replace(/_/g, " ").toLowerCase();
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    const s = config.spoiler;
    const n = config.nsfw_classifier;

    const labelRows = n.available_labels
      .map((label) => {
        const on = n.labels.includes(label) ? "checked" : "";
        return `<label style="display:flex; gap:6px; align-items:center; min-width:180px;"><input type="checkbox" data-label value="${label}" ${on}> ${labelText(label)}</label>`;
      })
      .join("");

    const modeOptions = Object.keys(MODE_HINTS)
      .map(
        (m) =>
          `<option value="${m}" ${n.sfw_mode === m ? "selected" : ""}>${m}</option>`,
      )
      .join("");

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Image Guard</h2>
          <div class="subtitle">What the bot does about explicit images, and where</div>
        </header>
        ${renderMetaWarning()}
        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Spoiler-Required Channels</div>
            <div class="field">
              <label>Channels</label>
              <span data-picker="spoiler_required_channels"></span>
              <div class="field-hint">In these channels, an image the bot detects as
                explicit is removed unless it was posted with Discord's spoiler blur.
                Ordinary pictures posted without a spoiler tag are left alone. An image
                the bot can't read is removed anyway, so a failed check is never a way
                through. Leave empty to switch this off. Members holding a bypass role
                (set on Global Settings) are never caught by this.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Nudity in SFW Channels</div>
            <div class="field">
              <label>Mode</label>
              <select data-field="sfw_mode">${modeOptions}</select>
              <div class="field-hint" data-mode-hint></div>
            </div>
            <div class="field">
              <label>Log Channel</label>
              <span data-picker="sfw_log_channel"></span>
              <div class="field-hint">Where each call is reported for review, with the
                detected label and confidence — this is how you spot false positives.</div>
            </div>
            <div class="field">
              <label>Exempt Channels</label>
              <span data-picker="sfw_exempt_channels"></span>
              <div class="field-hint">Never checked. Age-gated (NSFW-marked) channels
                and anything the bot itself posts are already exempt automatically.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Detection Tuning</div>
            <div class="field">
              <label>Confidence Threshold</label>
              <input type="number" data-field="threshold" min="0.05" max="1" step="0.05" value="${n.threshold}">
              <div class="field-hint">How sure the bot must be before treating an image
                as explicit. Used for spoiler checks and for tip eligibility. Lower
                catches more and misjudges more.</div>
            </div>
            <div class="field">
              <label>SFW Removal Threshold</label>
              <input type="number" data-field="sfw_threshold" min="0.05" max="1" step="0.05" value="${n.sfw_threshold}">
              <div class="field-hint">A separate, stricter bar for actually deleting
                someone's image in a SFW channel — being wrong there costs a member
                their photo, so it should demand more certainty than the setting above.</div>
            </div>
            <div class="field">
              <label>What Counts As Explicit</label>
              <div style="display:flex; flex-wrap:wrap; gap:8px 16px;">${labelRows}</div>
              <div class="field-hint">Exposed nudity only by default. Adding the
                "covered" entries makes lingerie and swimwear count too.</div>
            </div>
          </div>

          <div class="card">
            <div class="section-label">Recent Activity</div>
            <div data-metrics><div class="empty">Loading…</div></div>
            <div class="field-hint">Images checked in age-gated channels over the last
              30 days. Only those channels are recorded — checks elsewhere leave no trace.</div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const modeSelect = form.querySelector('[data-field="sfw_mode"]');
    const modeHint = form.querySelector("[data-mode-hint]");

    const spoilerPicker = mountChannelMultiPicker(
      form.querySelector('[data-picker="spoiler_required_channels"]'),
      channels,
      s.spoiler_required_channels,
      { label: "Spoiler-Required Channels" },
    );
    const exemptPicker = mountChannelMultiPicker(
      form.querySelector('[data-picker="sfw_exempt_channels"]'),
      channels,
      n.sfw_exempt_channels,
      { label: "Exempt Channels" },
    );
    const logPicker = mountChannelPicker(
      form.querySelector('[data-picker="sfw_log_channel"]'),
      channels,
      n.sfw_log_channel_id,
      { label: "Log Channel" },
    );

    const syncModeHint = () => {
      modeHint.textContent = MODE_HINTS[modeSelect.value] || "";
    };
    syncModeHint();
    modeSelect.addEventListener("change", syncModeHint);

    guardForm(form);

    (async () => {
      const box = container.querySelector("[data-metrics]");
      try {
        const m = await api("/api/nsfw-classifier/metrics", { days: 30 });
        if (!m.classified) {
          box.innerHTML = `<div class="empty">Nothing checked yet.</div>`;
          return;
        }
        const top = m.labels
          .slice(0, 5)
          .map((l) => `<li>${labelText(l.label)} — ${l.count}</li>`)
          .join("");
        box.innerHTML = `
          <ul style="margin:0; padding-left:18px;">
            <li><strong>${m.classified}</strong> images checked</li>
            <li><strong>${m.explicit}</strong> judged explicit · ${m.not_explicit} not</li>
            <li>average <strong>${m.avg_inference_ms}ms</strong> per image</li>
          </ul>
          ${top ? `<div class="section-label" style="margin-top:10px;">Most common detections</div><ul style="margin:0; padding-left:18px;">${top}</ul>` : ""}
        `;
      } catch {
        box.innerHTML = `<div class="empty">Couldn't load activity.</div>`;
      }
    })();

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const labels = [...form.querySelectorAll("[data-label]:checked")].map(
        (el) => el.value,
      );
      if (!labels.length) {
        showStatus(status, false, "Pick at least one label that counts as explicit.");
        return;
      }
      try {
        await apiPut("/api/config/spoiler", {
          spoiler_required_channels: spoilerPicker.getValues(),
        });
        await apiPut("/api/config/nsfw-classifier", {
          threshold: Number(form.querySelector('[data-field="threshold"]').value),
          sfw_threshold: Number(
            form.querySelector('[data-field="sfw_threshold"]').value,
          ),
          labels,
          sfw_mode: modeSelect.value,
          sfw_log_channel_id: logPicker.getValue() || "0",
          sfw_exempt_channels: exemptPicker.getValues(),
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  })();
}
