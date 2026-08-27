/**
 * Shop & Perks — the things an admin curates for members to buy.
 *
 * This page used to be all sixteen sections of the perk shop at once: the priced
 * dials, two work queues, and these three catalogs, 3,589px of scroll on an
 * empty database and growing with every icon, colour and item added. It is now
 * the three catalogs plus one card — What's On Sale, the per-perk switches,
 * which belongs with the catalogs rather than with the prices because it
 * answers "is this sold here", not "what does it cost". Its route id is
 * unchanged — ids are frozen, and this page still exists, so nothing needs a
 * MOVED_PAGES entry.
 *
 *   #/pricing         the numbers (one form, one Save, admin only)
 *   #/shop-approvals  the queues (manager-visible, which the old page was not)
 *
 * The three catalogs here share a shape: a list of rows you can edit in place,
 * and a form to add one. Two of them accept an image upload. All three are
 * setup-burst work — heavy for an afternoon when a server launches, then close
 * to dormant — which is why they sit apart from the queues, which fill on their
 * own and have to be worked.
 *
 * A row priced 0 falls through to the flat dial of the same name on #/pricing;
 * the hints say so, and say where, because "the price above" stopped being true
 * when the dials moved.
 */
import { api, apiPost, apiPut, apiDelete, request, esc } from "../api.js";
import {
  showStatus, guardForm, mountAsync, loadRoles,
} from "../config-helpers.js";
import { confirmDialog, toast } from "../ui.js";
import { DEFAULT_MAX, economyOffBanner } from "./economy-shop-shared.js";

/**
 * The shop lines an admin can switch off, in the order the shop shows them.
 * Mirrors ``SHOP_TOGGLE_PERKS`` in economy_service.py — a row here with no
 * field there (or the reverse) is a checkbox that saves nothing, so the two
 * lists are asserted against each other in the tests rather than trusted.
 *
 * The blurbs are the shop's own, so an admin reading this list sees what a
 * member sees rather than a column of field names.
 */
const SHOP_LINES = [
  ["shop_role_name_enabled", "Custom Role Name", "nickname + role"],
  ["shop_role_color_enabled", "Custom Role Color", "any solid color"],
  ["shop_role_preset_enabled", "Palette Color", "a curated fade"],
  ["shop_role_gradient_enabled", "Gradient Role", "a two-color fade they pick"],
  ["shop_role_holographic_enabled", "Holographic Role", "Discord’s shimmer preset"],
  ["shop_role_icon_enabled", "Role Icon", "a badge beside their name"],
  ["shop_voice_style_enabled", "Voice Style", "renaming and sizing their voice room"],
  ["shop_streak_shield_enabled", "Streak Shield", "one-shot, saves a login streak"],
];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading the shop…</div></div>`;

  return mountAsync(container, async () => {
    const [cfg, icons, colors] = await Promise.all([
      api("/api/economy/config"),
      api("/api/economy/icon-catalog").catch(() => []),
      api("/api/economy/color-catalog").catch(() => []),
    ]);
    render(container, cfg);
    wireOnSale(container, cfg);
    wirePalette(container, colors);
    wireCatalog(container, icons);
    wireShopItems(container);
  }, { errorMsg: "Couldn’t load the shop catalogs." });
}

/**
 * The "What's On Sale" checkboxes.
 *
 * Saved as one PUT of all eight, not one per click: the whole point of the card
 * is deciding what the shop stocks, and a half-applied set (some boxes saved,
 * one request failed) would leave the page disagreeing with the bot with no
 * sign of which half won. Check-all / uncheck-all only move the boxes — they
 * still go through Save, so "turn the whole shop off" is never one stray click.
 */
function wireOnSale(container, cfg) {
  const form = container.querySelector("[data-onsale-form]");
  const status = form.querySelector("[data-onsale-status]");
  const boxes = [...form.querySelectorAll("[data-onsale]")];

  for (const box of boxes) box.checked = !!cfg[box.name];

  guardForm(form);

  const setAll = (on) => {
    for (const box of boxes) box.checked = on;
    // The buttons bypass the inputs' own change events, so nudge the form's
    // dirty tracking the same way typing would.
    form.dispatchEvent(new Event("change", { bubbles: true }));
  };
  form.querySelector("[data-onsale-all]").addEventListener("click", () => setAll(true));
  form.querySelector("[data-onsale-none]").addEventListener("click", () => setAll(false));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {};
    for (const box of boxes) payload[box.name] = box.checked;
    try {
      await apiPut("/api/economy/config", payload);
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

function render(container, cfg) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Shop &amp; Perks</h2>
        <div class="subtitle">What members can buy: which perks are on sale at all,
          the colour palette, the rentable icon catalog, and your own custom items.
          Prices live on <a href="#/pricing">Pricing</a> and purchases needing a
          person are on <a href="#/shop-approvals">Approvals</a>.</div>
      </header>
      ${economyOffBanner(cfg)}

      <form class="form card" data-onsale-form>
        <div class="section-label">What’s On Sale</div>
        <div class="field-hint" style="margin-bottom:10px;">
          Uncheck anything you don’t want sold here. It disappears from the shop and
          from <code>/bank gift</code>, and nobody can buy it — including moderators,
          if you have them comped. Anyone already renting it keeps it until their
          week is up, then it stops renewing and they aren’t charged again; the bot
          DMs them to say why it ended. Re-check it before their week runs out and
          they simply renew as normal, so this is safe to change your mind about.
        </div>
        <div data-onsale-list>
          ${SHOP_LINES.map(([key, label, blurb]) => `
            <label class="field" style="display:flex;gap:8px;align-items:baseline;margin-bottom:6px;">
              <input type="checkbox" name="${key}" data-onsale />
              <span><strong>${esc(label)}</strong>
                <span class="field-hint" style="display:inline;">— ${esc(blurb)}</span>
              </span>
            </label>`).join("")}
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;">
          <button type="button" class="btn" data-onsale-all>Check all</button>
          <button type="button" class="btn" data-onsale-none>Uncheck all</button>
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-onsale-status></span>
        </div>
        <div class="field-hint" style="margin-top:10px;">
          The weekly raffle has its own switch on <a href="#/pricing">Pricing</a>, and
          each of your custom items below has its own — they aren’t repeated here,
          so there’s only ever one place that decides whether something is sold.
          Prices are on <a href="#/pricing">Pricing</a>: a price of 0 means free, not
          off. This is the off switch.
        </div>
      </form>

      <section class="form card">
        <div class="section-label">Color Palette</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Named gradient colors members rent from <code>/bank shop</code> — the curated
          alternative to picking their own two colors. Colors are authored by file name:
          upload a swatch called <code>ColorName_HEX1_HEX2.png</code> and press Sync, and
          the name, gradient and ordering all come from the file. Leave a color's price at
          0 to charge the Palette Color price above, or give it its own. A color somebody
          is renting cannot be deleted — clear "Offer in the shop" instead and current
          renters keep it. Your server needs Discord's enhanced role colors feature for
          gradients to show up at all.
        </div>

        <div data-palette></div>
        <div data-palette-empty class="field-hint" style="display:none;">
          No colors yet. Upload swatch images below and press Sync Palette.
        </div>

        <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--rule);">
          <div class="section-label">Swatch Images</div>
          <div class="field-hint" style="margin-bottom:10px;">
            One image per color, named <code>ColorName_HEX1_HEX2.png</code> — for example
            <code>Ruby_ff0000_8b0000.png</code>. The two hex codes become the gradient.
            Images are stored for this server only.
          </div>
          <div data-swatch-active class="field-hint" style="margin-bottom:10px;"></div>
          <div data-swatch-list style="margin-bottom:12px;"><div class="empty">Loading swatch images…</div></div>
          <form class="field-row" style="flex-wrap:wrap;align-items:flex-end;" data-upload-form>
            <div class="field">
              <label for="sink-swatch-input">Image Files</label>
              <input type="file" id="sink-swatch-input" name="files"
                accept="image/png,image/jpeg,image/gif,image/webp" multiple data-swatch-input />
              <div class="field-hint">Several at once is fine. Uploading alone changes nothing — press Sync Palette after.</div>
            </div>
            <button type="submit" class="btn btn-primary" data-upload-btn>Upload Images</button>
            <span data-upload-status></span>
          </form>
        </div>

        <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--rule);">
          <div class="section-label">Sync Palette From Swatches</div>
          <div class="field-hint" style="margin-bottom:10px;">
            Makes the palette match the images above: new files become new colors, and
            existing colors pick up any renamed file, changed gradient or new ordering.
            A color whose image is gone stops being offered — if someone is renting it they
            keep it, and it is deleted outright only when nobody holds it. No Discord roles
            are created or deleted, so members wearing an old booster color keep it either way.
          </div>
          <form class="field-row" style="align-items:center;" data-sync-form>
            <button type="submit" class="btn btn-primary" data-sync-btn>Sync Palette</button>
            <span data-sync-status></span>
          </form>
        </div>

        <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--rule);">
          <div class="section-label">Old Showroom Channel</div>
          <div class="field-hint" style="margin-bottom:10px;">
            Members now browse the swatches inside <code>/bank shop</code> — the picker shows
            every gradient as a picture, so no channel has to hold them. If this server still
            has the old showroom sitting in a channel, this deletes those messages. Nothing
            else changes: the colors, their prices and anyone renting one are untouched.
          </div>
          <form class="form" data-takedown-form>
            <div style="display:flex;gap:8px;align-items:center;">
              <button type="submit" class="btn btn-danger" data-takedown-btn>Delete Old Showroom</button>
              <span data-takedown-status></span>
            </div>
          </form>
        </div>
      </section>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Rentable Icon Catalog</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Role icons you curate, which members rent from <code>/bank shop</code> at whatever
          price you give each one. A price change takes effect at each renter's next weekly
          renewal, never mid-week. An icon somebody is currently renting cannot be deleted —
          stop offering it instead, and the people already renting it keep it. Uploaded
          images are shrunk to a small PNG, because Discord will not accept a role icon over
          256&nbsp;kilobytes, and your server needs Discord's Role Icons feature for any of
          this to appear.
        </div>

        <div data-catalog></div>
        <div data-catalog-empty class="field-hint" style="display:none;">
          No icons in the catalog yet. Add one below and members will see it in the shop.
        </div>

        <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--rule);">
          <div class="section-label">Add an Icon</div>
          <div class="field-row" style="flex-wrap:wrap;align-items:flex-end;">
            <div class="field">
              <label for="sink-add-name">Name</label>
              <input type="text" id="sink-add-name" data-add-name maxlength="64" placeholder="e.g. Golden Crown" style="max-width:200px;" />
              <div class="field-hint">What members see in the shop.</div>
            </div>
            <div class="field">
              <label for="sink-add-price">Price Per Week</label>
              <input type="number" id="sink-add-price" data-add-price min="0" max="${DEFAULT_MAX}" step="1" value="75" style="max-width:120px;" />
            </div>
            <div class="field">
              <label for="sink-add-file">Image</label>
              <input type="file" id="sink-add-file" data-add-file accept="image/png,image/webp,image/jpeg,image/gif" />
              <div class="field-hint">A PNG, WEBP, JPEG, or GIF. Square images look best.</div>
            </div>
            <button type="button" class="btn btn-primary" data-add>Add Icon</button>
            <span data-add-status></span>
          </div>
        </div>
      </section>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Custom Shop Items</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Things <b>you</b> decide to sell, listed in <code>/bank shop</code> under
          “Server Store” beside the built-in perks. Each one either <b>grants a role</b>
          the moment it is bought, or <b>goes on the mod todo list</b> for a human to do —
          and is either a <b>one-off</b> purchase or a <b>weekly rental</b> that renews out
          of the buyer’s wallet. Money is taken at purchase either way; if you turn an
          order down, the buyer gets it back. An item somebody has an open order or a live
          rental on can’t be deleted — switch it off instead, and the people already
          holding it keep it.
        </div>

        <div data-items></div>
        <div data-items-empty class="field-hint" style="display:none;">
          Nothing in the store yet. Add something below and it appears in the shop.
        </div>

        <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--rule);">
          <div class="section-label">Add an Item</div>
          <div class="field-row" style="flex-wrap:wrap;align-items:flex-end;">
            <div class="field">
              <label for="item-add-name">Name</label>
              <input type="text" id="item-add-name" data-item-name maxlength="60"
                     placeholder="e.g. Shoutout" style="max-width:200px;" />
            </div>
            <div class="field">
              <label for="item-add-blurb">Short Note</label>
              <input type="text" id="item-add-blurb" data-item-blurb maxlength="40"
                     placeholder="e.g. in announcements" style="max-width:180px;" />
              <div class="field-hint">Shown beside the name in the shop. Keep it short.</div>
            </div>
            <div class="field">
              <label for="item-add-price">Price</label>
              <input type="number" id="item-add-price" data-item-price min="0"
                     max="${DEFAULT_MAX}" step="1" value="100" style="max-width:110px;" />
            </div>
            <div class="field">
              <label for="item-add-kind">When bought</label>
              <select id="item-add-kind" data-item-kind style="max-width:190px;">
                <option value="manual">Add to the mod todo list</option>
                <option value="role">Give them a role</option>
              </select>
            </div>
            <div class="field">
              <label for="item-add-billing">Charge</label>
              <select id="item-add-billing" data-item-billing style="max-width:150px;">
                <option value="once">Once</option>
                <option value="weekly">Every week</option>
              </select>
            </div>
            <div class="field" data-item-role-wrap style="display:none;">
              <label for="item-add-role">Role</label>
              <select id="item-add-role" data-item-role style="max-width:190px;"></select>
            </div>
            <button type="button" class="btn btn-primary" data-item-add>Add Item</button>
            <span data-item-add-status></span>
          </div>
        </div>
      </section>
    </div>
  `;
}

function iconRow(icon) {
  const bust = Date.now();
  const usedBadge = icon.in_use
    ? `<span class="badge" title="Members are renting this icon right now">In use</span>`
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
        <label>Price Per Week</label>
        <input type="number" data-price min="0" max="${DEFAULT_MAX}" step="1" value="${icon.price}" style="max-width:120px;" />
      </div>
      <label style="display:flex;gap:6px;align-items:center;">
        <input type="checkbox" data-enabled${enabledAttr} /> Offer in the shop
      </label>
      ${usedBadge}
      <div style="display:flex;gap:8px;margin-left:auto;">
        <button type="button" class="btn btn-primary" data-save>Save</button>
        <button type="button" class="btn btn-danger" data-delete>Delete</button>
      </div>
      <span data-row-status></span>
    </div>`;
}


function colorRow(color) {
  const bust = Date.now();
  const usedBadge = color.in_use
    ? `<span class="badge" title="Members are renting this color right now">In use</span>`
    : "";
  // A row whose swatch filename never parsed has no gradient to project, so the
  // shop cannot offer it however it is priced. Say that rather than showing a
  // blank chip and an editable price that does nothing.
  const brokenBadge = color.rentable
    ? ""
    : `<span class="badge badge-danger"
         title="No gradient could be read from this color's file name — re-upload it as ColorName_HEX1_HEX2 and sync">Needs a re-sync</span>`;
  // The real swatch art, over the gradient it encodes: the art can carry texture
  // the two hex codes don't, and the gradient behind it still reads if the file
  // has gone missing from disk.
  const fallback = color.rentable
    ? `linear-gradient(135deg,#${esc(color.hex1)},#${esc(color.hex2)})`
    : "repeating-linear-gradient(45deg,#555,#555 6px,#333 6px,#333 12px)";
  const swatch = `
    <img src="/api/economy/color-catalog/${color.id}/image?t=${bust}" alt=""
         width="48" height="48"
         style="width:48px;height:48px;border-radius:8px;object-fit:cover;flex:none;
                border:1px solid var(--rule);background:${fallback};" />`;
  return `
    <div class="card" data-color-id="${color.id}"
         style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px;">
      ${swatch}
      <div class="field" style="margin:0;">
        <label>Name</label>
        <input type="text" data-name maxlength="64" value="${esc(color.name)}" style="max-width:200px;" />
      </div>
      <div class="field" style="margin:0;">
        <label>Price Per Week</label>
        <input type="number" data-price min="0" max="${DEFAULT_MAX}" step="1" value="${color.price}" style="max-width:120px;" />
        <div class="field-hint">0 uses the Palette Color price above.</div>
      </div>
      <label style="display:flex;gap:6px;align-items:center;">
        <input type="checkbox" data-enabled${color.enabled ? " checked" : ""} /> Offer in the shop
      </label>
      ${usedBadge}
      ${brokenBadge}
      <div style="display:flex;gap:8px;margin-left:auto;">
        <button type="button" class="btn btn-primary" data-save>Save</button>
        <button type="button" class="btn btn-danger" data-delete>Delete</button>
      </div>
      <span data-row-status></span>
    </div>`;
}


function wirePalette(container, colors) {
  const listEl = container.querySelector("[data-palette]");
  const emptyEl = container.querySelector("[data-palette-empty]");

  function renderList(rows) {
    listEl.innerHTML = rows.map(colorRow).join("");
    emptyEl.style.display = rows.length ? "none" : "block";
  }
  renderList(colors);

  // Row actions via delegation so re-rendered rows stay wired.
  listEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest("[data-color-id]");
    const id = row.getAttribute("data-color-id");
    const rowStatus = row.querySelector("[data-row-status]");

    if (btn.hasAttribute("data-save")) {
      const nameInput = row.querySelector("[data-name]");
      const priceInput = row.querySelector("[data-price]");
      if (!nameInput.value.trim()) {
        showStatus(rowStatus, false, "Name cannot be empty");
        nameInput.focus();
        return;
      }
      const price = parseInt(priceInput.value, 10);
      if (!Number.isFinite(price) || price < 0 || price > DEFAULT_MAX) {
        showStatus(rowStatus, false, `Price Per Week must be a whole number from 0 to ${DEFAULT_MAX}`);
        priceInput.focus();
        return;
      }
      btn.disabled = true;
      try {
        await request("PATCH", `/api/economy/color-catalog/${id}`, {
          body: {
            name: nameInput.value.trim(),
            price,
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
      const colorName = row.querySelector("[data-name]").value.trim() || "this color";
      const ok = await confirmDialog(
        `"${colorName}" disappears from the shop and the showroom. Its swatch image stays, `
        + "so the next sync brings it back — delete the image too if you want it gone. "
        + "To retire a color while keeping it for current renters, clear \"Offer in the "
        + "shop\" and save instead.",
        { title: "Delete this color?", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
      btn.disabled = true;
      try {
        await apiDelete(`/api/economy/color-catalog/${id}`);
        renderList(await api("/api/economy/color-catalog"));
      } catch (err) {
        // 409 = in use: surface the reason, keep the row.
        showStatus(rowStatus, false, err.message);
        btn.disabled = false;
      }
    }
  });

  // ── swatch images ──
  const swatchList = container.querySelector("[data-swatch-list]");
  const swatchActive = container.querySelector("[data-swatch-active]");

  function renderSwatchList(data) {
    const files = data.files || [];
    if (!files.length) {
      swatchList.innerHTML = `<div class="empty">No swatch images uploaded yet.</div>`;
    } else {
      swatchList.innerHTML = files.map((f) => {
        const chip = f.valid
          ? `<span style="display:inline-block;width:28px;height:18px;border-radius:4px;border:1px solid var(--rule);background:linear-gradient(135deg,#${esc(f.hex1)},#${esc(f.hex2)});flex:none;"></span>`
          : `<span style="display:inline-block;width:28px;height:18px;border-radius:4px;border:1px solid var(--rule);background:repeating-linear-gradient(45deg,#555,#555 4px,#333 4px,#333 8px);flex:none;"></span>`;
        const meta = f.valid
          ? `<span>${esc(f.label)}</span>`
          : `<span style="color:var(--red-text)">⚠ Skipped when syncing — rename it to ColorName_HEX1_HEX2 plus its extension.</span>`;
        return `
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid var(--rule);">
            ${chip}
            <span style="flex:none;font-family:monospace;opacity:.85;">${esc(f.name)}</span>
            ${meta}
            <button type="button" class="btn btn-danger" style="margin-left:auto;padding:2px 8px;" data-swatch-del="${esc(f.name)}">Delete</button>
          </div>`;
      }).join("");
    }
    swatchActive.innerHTML = data.using_managed
      ? ""
      : `<strong>Syncing currently reads a folder on the server:</strong> <code>${esc(data.active_dir)}</code>. Upload at least one correctly named image here to switch syncing over to this server's own set.`;

    swatchList.querySelectorAll("[data-swatch-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = btn.dataset.swatchDel;
        const ok = await confirmDialog(
          `Delete the image "${name}"? At the next sync its color stops being offered — `
          + "anyone renting it keeps it, and it is removed outright only if nobody holds it.",
          { title: "Delete Swatch Image", danger: true, confirmLabel: "Delete Image" },
        );
        if (!ok) return;
        try {
          renderSwatchList(
            await apiDelete(`/api/economy/color-catalog/swatches/${encodeURIComponent(name)}`),
          );
        } catch (err) {
          toast(err.message, "error");
        }
      });
    });
  }

  // The trailing .catch is not redundant with the try/inside: a throw from
  // renderSwatchList itself would escape as an unhandled rejection and leave
  // the list on "Loading swatch images…" forever.
  (async () => {
    try {
      renderSwatchList(await api("/api/economy/color-catalog/swatches"));
    } catch (err) {
      swatchList.innerHTML = `<div class="error">Couldn't load the swatch images: ${esc(err.message)}</div>`;
    }
  })().catch(() => {
    swatchList.innerHTML = `<div class="error">Couldn't load the swatch images.</div>`;
  });

  const uploadForm = container.querySelector("[data-upload-form]");
  const uploadBtn = container.querySelector("[data-upload-btn]");
  const uploadStatus = container.querySelector("[data-upload-status]");
  const swatchInput = container.querySelector("[data-swatch-input]");
  guardForm(uploadForm);
  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!swatchInput.files.length) {
      showStatus(uploadStatus, false, "Choose at least one image file first.");
      swatchInput.focus();
      return;
    }
    const fd = new FormData();
    for (const file of swatchInput.files) fd.append("files", file);
    uploadBtn.disabled = true;
    uploadStatus.textContent = "Uploading…";
    try {
      const data = await apiPost("/api/economy/color-catalog/swatches", fd);
      renderSwatchList(data);
      swatchInput.value = "";
      const n = data.saved?.length || 0;
      showStatus(uploadStatus, true, `Uploaded ${n} image${n === 1 ? "" : "s"}`);
    } catch (err) {
      showStatus(uploadStatus, false, err.message);
    } finally {
      uploadBtn.disabled = false;
    }
  });

  // ── sync ──
  const syncForm = container.querySelector("[data-sync-form]");
  const syncBtn = container.querySelector("[data-sync-btn]");
  const syncStatus = container.querySelector("[data-sync-status]");
  syncForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    syncBtn.disabled = true;
    syncStatus.textContent = "Syncing…";
    try {
      const data = await apiPost("/api/economy/color-catalog/sync");
      const parts = [];
      if (data.added?.length) parts.push(`added ${data.added.length}`);
      if (data.disabled?.length) parts.push(`stopped offering ${data.disabled.length}`);
      if (data.removed?.length) parts.push(`removed ${data.removed.length}`);
      showStatus(syncStatus, true, parts.length ? parts.join(", ") : "Already up to date");
      if (data.still_disabled?.length) {
        // Sync never re-enables — it can't tell a deliberate retirement from a
        // swatch that was deleted by mistake and put back.
        toast(
          `Not offered in the shop: ${data.still_disabled.join(", ")}. `
          + "Tick \"Offer in the shop\" to bring one back.",
          "info",
        );
      }
      renderList(await api("/api/economy/color-catalog"));
    } catch (err) {
      showStatus(syncStatus, false, err.message);
    } finally {
      syncBtn.disabled = false;
    }
  });

  // ── the old showroom channel, on its way out ──
  const takedownForm = container.querySelector("[data-takedown-form]");
  const takedownBtn = container.querySelector("[data-takedown-btn]");
  const takedownStatus = container.querySelector("[data-takedown-status]");
  guardForm(takedownForm);
  takedownForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ok = await confirmDialog(
      "Delete the showroom messages this server posted in a channel? "
      + "Members browse the colors in /bank shop instead — nothing else changes.",
      { title: "Delete Old Showroom", danger: true, confirmLabel: "Delete Messages" },
    );
    if (!ok) return;
    takedownBtn.disabled = true;
    try {
      const data = await apiPost("/api/economy/color-catalog/remove-panel");
      const n = data.deleted || 0;
      showStatus(
        takedownStatus, true,
        n ? `Deleted ${n} message${n === 1 ? "" : "s"}` : "Nothing left to delete",
      );
    } catch (err) {
      showStatus(takedownStatus, false, err.message);
    } finally {
      takedownBtn.disabled = false;
    }
  });
}

// ── Custom shop items ───────────────────────────────────────────────────────
//
// Admin-defined products sold beside the built-in perks. Two axes per item —
// what buying it does (grant a role, or file a mod todo) and how often it
// charges (once, or weekly) — so the row editor is wider than the icon
// catalog's, but it is the same delegated save/delete shape.

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
      const nameInput = row.querySelector("[data-name]");
      const priceInput = row.querySelector("[data-price]");
      if (!nameInput.value.trim()) {
        showStatus(rowStatus, false, "Name cannot be empty");
        nameInput.focus();
        return;
      }
      const price = parseInt(priceInput.value, 10);
      if (!Number.isFinite(price) || price < 0 || price > DEFAULT_MAX) {
        showStatus(rowStatus, false, `Price Per Week must be a whole number from 0 to ${DEFAULT_MAX}`);
        priceInput.focus();
        return;
      }
      btn.disabled = true;
      try {
        await request("PATCH", `/api/economy/icon-catalog/${id}`, {
          body: {
            name: nameInput.value.trim(),
            price,
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
      // Deleting throws away the curated icon AND the image that was uploaded
      // for it, with no undo — every sibling flow confirms first (W-C9).
      const iconName = row.querySelector("[data-name]").value.trim() || "this icon";
      const ok = await confirmDialog(
        `"${iconName}" and the image uploaded for it are deleted for good, and it disappears from the shop. `
        + "This cannot be undone. To retire an icon while keeping it for current renters, "
        + "clear \"Offer in the shop\" and save instead.",
        { title: "Delete this icon?", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
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
    if (!name.value.trim()) {
      showStatus(addStatus, false, "Give the icon a Name first");
      name.focus();
      return;
    }
    if (!file.files.length) {
      showStatus(addStatus, false, "Choose an Image first");
      file.focus();
      return;
    }
    const priceValue = parseInt(price.value, 10);
    if (!Number.isFinite(priceValue) || priceValue < 0 || priceValue > DEFAULT_MAX) {
      showStatus(addStatus, false, `Price Per Week must be a whole number from 0 to ${DEFAULT_MAX}`);
      price.focus();
      return;
    }
    const fd = new FormData();
    fd.append("name", name.value.trim());
    fd.append("price", priceValue);
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

// ── color palette ─────────────────────────────────────────────────────
//
// The palette is authored by file name, not by form: there is no "add a color"
// control because a color IS a swatch image plus a sync. What can be edited per
// row is what the sync deliberately never overwrites — the name shown in the

const ITEM_KINDS = { manual: "Mod todo", role: "Gives a role" };
const ITEM_BILLING = { once: "Once", weekly: "Weekly" };

function itemStockLabel(item) {
  if (item.stock === null || item.stock === undefined) return "Unlimited";
  const left = Math.max(0, item.stock - item.sold);
  return `${left} of ${item.stock} left`;
}

function itemRow(item, roleName) {
  const enabledAttr = item.enabled ? " checked" : "";
  // A role item whose role has since been deleted can still be bought, and the
  // grant would silently do nothing — say so where the admin is looking.
  const brokenRole = item.kind === "role" && item.role_id && !roleName
    ? `<span class="badge badge-danger"
         title="This role no longer exists, so buying the item would grant nothing">Role is gone</span>`
    : "";
  const roleChip = item.kind === "role" && roleName
    ? `<span class="badge">${esc(roleName)}</span>` : "";
  const soldOut = item.stock !== null && item.stock !== undefined
    && item.sold >= item.stock
    ? `<span class="badge">Sold out</span>` : "";
  return `
    <div class="card" data-item-id="${item.id}"
         style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px;">
      <div class="field" style="margin:0;">
        <label>Name</label>
        <input type="text" data-name maxlength="60" value="${esc(item.name)}" style="max-width:180px;" />
      </div>
      <div class="field" style="margin:0;">
        <label>Short Note</label>
        <input type="text" data-blurb maxlength="40" value="${esc(item.blurb || "")}" style="max-width:160px;" />
      </div>
      <div class="field" style="margin:0;">
        <label>Price</label>
        <input type="number" data-price min="0" max="${DEFAULT_MAX}" step="1" value="${item.price}" style="max-width:110px;" />
      </div>
      <div class="field" style="margin:0;">
        <label>Stock</label>
        <input type="number" data-stock min="0" step="1" placeholder="∞"
               value="${item.stock === null || item.stock === undefined ? "" : item.stock}" style="max-width:90px;" />
        <div class="field-hint">${esc(itemStockLabel(item))}</div>
      </div>
      <div class="field" style="margin:0;">
        <label>Max Each</label>
        <input type="number" data-limit min="1" step="1" placeholder="∞"
               value="${item.per_member_limit === null || item.per_member_limit === undefined ? "" : item.per_member_limit}" style="max-width:90px;" />
      </div>
      <span class="badge">${esc(ITEM_KINDS[item.kind] || item.kind)}</span>
      <span class="badge">${esc(ITEM_BILLING[item.billing] || item.billing)}</span>
      ${roleChip}${brokenRole}${soldOut}
      <label style="display:flex;gap:6px;align-items:center;">
        <input type="checkbox" data-enabled${enabledAttr} /> Offer in the shop
      </label>
      <label style="display:flex;gap:6px;align-items:center;">
        <input type="checkbox" data-ask-note${item.ask_note ? " checked" : ""} /> Ask for a note
      </label>
      <div style="display:flex;gap:8px;margin-left:auto;">
        <button type="button" class="btn btn-primary" data-save>Save</button>
        <button type="button" class="btn btn-danger" data-delete>Delete</button>
      </div>
      <span data-row-status></span>
    </div>`;
}

/** Read the optional whole-number fields back, treating blank as "no limit". */
function optionalInt(input) {
  const raw = String(input.value || "").trim();
  if (!raw) return null;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

function wireShopItems(container) {
  const listEl = container.querySelector("[data-items]");
  const emptyEl = container.querySelector("[data-items-empty]");
  const kindSel = container.querySelector("[data-item-kind]");
  const roleWrap = container.querySelector("[data-item-role-wrap]");
  const roleSel = container.querySelector("[data-item-role]");
  const addBtn = container.querySelector("[data-item-add]");
  const addStatus = container.querySelector("[data-item-add-status]");

  let roleName = () => "";

  // The role picker only exists for role items, so it only appears for them.
  function syncKind() {
    roleWrap.style.display = kindSel.value === "role" ? "" : "none";
  }
  kindSel.addEventListener("change", syncKind);
  syncKind();

  function renderList(rows) {
    listEl.innerHTML = rows.map((i) => itemRow(i, roleName(i.role_id))).join("");
    emptyEl.style.display = rows.length ? "none" : "block";
  }

  async function refresh() {
    let items = [];
    try {
      const [roles, rows] = await Promise.all([
        loadRoles().catch(() => []),
        api("/api/economy/shop-items"),
      ]);
      const byId = new Map(roles.map((r) => [String(r.id), r.name]));
      roleName = (id) => (id ? byId.get(String(id)) || "" : "");
      roleSel.innerHTML = roles
        .map((r) => `<option value="${esc(String(r.id))}">${esc(r.name)}</option>`)
        .join("");
      items = rows;
    } catch {
      emptyEl.textContent = "Couldn’t load the store items.";
      emptyEl.style.display = "block";
      return;
    }
    renderList(items);
  }

  listEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest("[data-item-id]");
    const id = row.getAttribute("data-item-id");
    const rowStatus = row.querySelector("[data-row-status]");

    if (btn.hasAttribute("data-save")) {
      const nameInput = row.querySelector("[data-name]");
      if (!nameInput.value.trim()) {
        showStatus(rowStatus, false, "Name cannot be empty");
        nameInput.focus();
        return;
      }
      const price = parseInt(row.querySelector("[data-price]").value, 10);
      if (!Number.isFinite(price) || price < 0 || price > DEFAULT_MAX) {
        showStatus(rowStatus, false, `Price must be a whole number from 0 to ${DEFAULT_MAX}`);
        return;
      }
      btn.disabled = true;
      try {
        const current = (await api("/api/economy/shop-items"))
          .find((i) => String(i.id) === String(id));
        if (!current) throw new Error("That item is gone — reload the page.");
        await request("PATCH", `/api/economy/shop-items/${id}`, {
          body: {
            ...current,
            name: nameInput.value.trim(),
            blurb: row.querySelector("[data-blurb]").value.trim(),
            price,
            stock: optionalInt(row.querySelector("[data-stock]")),
            per_member_limit: optionalInt(row.querySelector("[data-limit]")),
            enabled: row.querySelector("[data-enabled]").checked,
            ask_note: row.querySelector("[data-ask-note]").checked,
          },
        });
        showStatus(rowStatus, true);
        await refresh();
      } catch (err) {
        showStatus(rowStatus, false, err.message);
      } finally {
        btn.disabled = false;
      }
      return;
    }

    if (btn.hasAttribute("data-delete")) {
      const name = row.querySelector("[data-name]").value.trim() || "this item";
      const ok = await confirmDialog(
        `"${name}" stops being sold and disappears from the shop. Anything already `
        + "bought is untouched. To retire an item while keeping it for the people "
        + "already holding it, clear \"Offer in the shop\" and save instead.",
        { title: "Delete this item?", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
      btn.disabled = true;
      try {
        await apiDelete(`/api/economy/shop-items/${id}`);
        toast("Item deleted.");
        await refresh();
      } catch (err) {
        showStatus(rowStatus, false, err.message);
        btn.disabled = false;
      }
    }
  });

  addBtn.addEventListener("click", async () => {
    const name = container.querySelector("[data-item-name]").value.trim();
    if (!name) {
      showStatus(addStatus, false, "Give the item a name");
      return;
    }
    const price = parseInt(container.querySelector("[data-item-price]").value, 10);
    if (!Number.isFinite(price) || price < 0 || price > DEFAULT_MAX) {
      showStatus(addStatus, false, `Price must be a whole number from 0 to ${DEFAULT_MAX}`);
      return;
    }
    const kind = kindSel.value;
    if (kind === "role" && !roleSel.value) {
      showStatus(addStatus, false, "Pick the role this item gives");
      return;
    }
    addBtn.disabled = true;
    try {
      await apiPost("/api/economy/shop-items", {
        name,
        blurb: container.querySelector("[data-item-blurb]").value.trim(),
        description: "",
        price,
        kind,
        billing: container.querySelector("[data-item-billing]").value,
        role_id: kind === "role" ? roleSel.value : null,
        stock: null,
        per_member_limit: null,
        available_from: null,
        available_until: null,
        ask_note: false,
        enabled: true,
        sort_order: 0,
      });
      container.querySelector("[data-item-name]").value = "";
      container.querySelector("[data-item-blurb]").value = "";
      showStatus(addStatus, true, "Added");
      await refresh();
    } catch (err) {
      showStatus(addStatus, false, err.message);
    } finally {
      addBtn.disabled = false;
    }
  });

  refresh();
}
