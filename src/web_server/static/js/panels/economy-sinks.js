import { api, apiPut, apiPost, apiDelete, request, esc } from "../api.js";
import { showStatus } from "../config-helpers.js";

// The perk-shop prices (the currency sinks). Moved here off the Settings page so
// everything a member can spend on lives in one place. Faucet rates stay on the
// Income Sources page. Each entry: [key, label, {min, hint}].
const PRICE_FIELDS = [
  ["price_role_color", "Role color", {}],
  ["price_role_name", "Role name", {}],
  ["price_role_icon", "Role icon (custom upload)", {
    hint: "Flat price when a member uploads their own icon. Curated catalog icons below are priced individually.",
  }],
  ["price_role_gradient", "Role gradient", {}],
  ["price_text_room", "Text room", { hint: "Used by a later stage." }],
  ["price_voice_room", "Voice room", { hint: "Used by a later stage." }],
  ["price_gift_color", "Gift color", {}],
];

function numField(key, label, { hint } = {}, pricing) {
  const hintHtml = hint ? `<div class="field-hint">${esc(hint)}</div>` : "";
  const suggested = pricing && pricing.hints ? pricing.hints[key] : null;
  const median = pricing ? Math.round(pricing.median || 0) : 0;
  const suggest = suggested != null
    ? `<div class="field-hint">suggested ≈ ${suggested} (from median weekly income ${median})</div>`
    : "";
  return `
    <div class="field">
      <label>${esc(label)}</label>
      <input type="number" name="${key}" min="0" step="1" style="max-width:140px;" />
      ${hintHtml}
      ${suggest}
    </div>`;
}

function iconRow(icon) {
  const bust = Date.now();
  const usedBadge = icon.in_use
    ? `<span class="badge" title="Members are renting this icon">in use</span>`
    : "";
  const enabledAttr = icon.enabled ? " checked" : "";
  return `
    <div class="card" data-icon-id="${icon.id}"
         style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px;">
      <img src="/api/economy/icon-catalog/${icon.id}/image?t=${bust}" alt=""
           width="48" height="48"
           style="width:48px;height:48px;border-radius:8px;object-fit:contain;
                  background:repeating-conic-gradient(#808080 0% 25%, #a0a0a0 0% 50%) 50% / 12px 12px" />
      <div class="field" style="margin:0;">
        <label>Name</label>
        <input type="text" data-name maxlength="64" value="${esc(icon.name)}" style="max-width:200px;" />
      </div>
      <div class="field" style="margin:0;">
        <label>Price / week</label>
        <input type="number" data-price min="0" step="1" value="${icon.price}" style="max-width:120px;" />
      </div>
      <label style="display:flex;gap:6px;align-items:center;">
        <input type="checkbox" data-enabled${enabledAttr} /> Enabled
      </label>
      ${usedBadge}
      <div style="display:flex;gap:8px;margin-left:auto;">
        <button type="button" class="btn btn-primary" data-save>Save</button>
        <button type="button" class="btn btn-danger" data-delete>Delete</button>
      </div>
      <span data-row-status></span>
    </div>`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading sinks…</div></div>`;

  (async () => {
    const [cfg, metrics, icons] = await Promise.all([
      api("/api/economy/config"),
      api("/api/economy/metrics").catch(() => null),
      api("/api/economy/icon-catalog").catch(() => []),
    ]);
    const pricing = metrics && metrics.hints && Object.keys(metrics.hints).length
      ? { hints: metrics.hints, median: metrics.median_income }
      : null;
    render(container, cfg, pricing, icons);
  })();
}

function render(container, cfg, pricing, icons) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Sinks</h2>
        <div class="subtitle">Everything members spend currency on — perk-shop prices and
          the rentable icon catalog. Faucet rates live on
          <a href="#/economy-income-sources">Income Sources</a>.</div>
      </header>

      <form class="form card" data-price-form>
        <div class="section-label">Perk prices</div>
        <div class="field-row" style="flex-wrap:wrap;">
          ${PRICE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
        </div>
        <div style="display:flex; gap:8px; align-items:center; margin-top:16px;">
          <button type="submit" class="btn btn-primary">Save prices</button>
          <span data-price-status></span>
        </div>
      </form>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Rentable icon catalog</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Curated role icons members rent from <code>/bank shop</code>, each with its own
          weekly price. Renting bills the icon's price; a price change applies at the
          renter's next weekly renewal. An icon that members are currently renting can't be
          deleted — disable it instead, and current renters keep it. Images are downscaled to
          a small PNG (Discord caps role icons at 256&nbsp;KB); requires the server to have
          the Role Icons feature.
        </div>

        <div data-catalog></div>
        <div data-catalog-empty class="field-hint" style="display:none;">
          No catalog icons yet — add one below.
        </div>

        <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--border,#333);">
          <div class="section-label">Add an icon</div>
          <div class="field-row" style="flex-wrap:wrap;align-items:flex-end;">
            <div class="field">
              <label>Name</label>
              <input type="text" data-add-name maxlength="64" placeholder="e.g. Golden crown" style="max-width:200px;" />
            </div>
            <div class="field">
              <label>Price / week</label>
              <input type="number" data-add-price min="0" step="1" value="75" style="max-width:120px;" />
            </div>
            <div class="field">
              <label>Image (PNG/WEBP)</label>
              <input type="file" data-add-file accept="image/png,image/webp,image/jpeg,image/gif" />
            </div>
            <button type="button" class="btn btn-primary" data-add>Add icon</button>
            <span data-add-status></span>
          </div>
        </div>
      </section>
    </div>`;

  wirePrices(container, cfg);
  wireCatalog(container, icons);
}

function wirePrices(container, cfg) {
  const form = container.querySelector("[data-price-form]");
  const status = form.querySelector("[data-price-status]");
  for (const [key] of PRICE_FIELDS) {
    form.querySelector(`[name=${key}]`).value = cfg[key];
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {};
    for (const [key] of PRICE_FIELDS) {
      payload[key] = parseInt(form.querySelector(`[name=${key}]`).value, 10);
    }
    try {
      await apiPut("/api/economy/config", payload);
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

function wireCatalog(container, icons) {
  const listEl = container.querySelector("[data-catalog]");
  const emptyEl = container.querySelector("[data-catalog-empty]");

  function renderList(rows) {
    listEl.innerHTML = rows.map(iconRow).join("");
    emptyEl.style.display = rows.length ? "none" : "block";
  }
  renderList(icons);

  // Row actions (save / delete) via delegation so re-rendered rows stay wired.
  listEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest("[data-icon-id]");
    const id = row.getAttribute("data-icon-id");
    const rowStatus = row.querySelector("[data-row-status]");

    if (btn.hasAttribute("data-save")) {
      btn.disabled = true;
      try {
        await request("PATCH", `/api/economy/icon-catalog/${id}`, {
          body: {
            name: row.querySelector("[data-name]").value.trim(),
            price: parseInt(row.querySelector("[data-price]").value, 10),
            enabled: row.querySelector("[data-enabled]").checked,
          },
        });
        showStatus(rowStatus, true);
      } catch (err) {
        showStatus(rowStatus, false, err.message);
      } finally {
        btn.disabled = false;
      }
    } else if (btn.hasAttribute("data-delete")) {
      btn.disabled = true;
      try {
        await apiDelete(`/api/economy/icon-catalog/${id}`);
        const fresh = await api("/api/economy/icon-catalog");
        renderList(fresh);
      } catch (err) {
        // 409 = in use: surface the reason, keep the row.
        showStatus(rowStatus, false, err.message);
        btn.disabled = false;
      }
    }
  });

  // Add form.
  const addBtn = container.querySelector("[data-add]");
  const addStatus = container.querySelector("[data-add-status]");
  addBtn.addEventListener("click", async () => {
    const name = container.querySelector("[data-add-name]");
    const price = container.querySelector("[data-add-price]");
    const file = container.querySelector("[data-add-file]");
    if (!name.value.trim()) { showStatus(addStatus, false, "Name required"); return; }
    if (!file.files.length) { showStatus(addStatus, false, "Pick an image"); return; }
    const fd = new FormData();
    fd.append("name", name.value.trim());
    fd.append("price", parseInt(price.value, 10) || 0);
    fd.append("image", file.files[0]);
    addBtn.disabled = true;
    showStatus(addStatus, true, "Uploading…");
    try {
      await apiPost("/api/economy/icon-catalog", fd);
      name.value = "";
      file.value = "";
      const fresh = await api("/api/economy/icon-catalog");
      renderList(fresh);
      showStatus(addStatus, true, "Added");
    } catch (err) {
      showStatus(addStatus, false, err.message);
    } finally {
      addBtn.disabled = false;
    }
  });
}
