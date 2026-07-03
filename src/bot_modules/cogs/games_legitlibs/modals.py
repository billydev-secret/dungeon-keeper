import math
import logging
import discord
from .data import resolve_blank
from .validation import validate_fill, strip_mass_mentions
from bot_modules.games.utils.game_manager import channel_name, modify_payload

log = logging.getLogger(__name__)

MODAL_PAGE_SIZE = 5


def _make_text_input(blank: dict, prompts: dict, tier: int, position: int, default: str | None = None) -> discord.ui.TextInput:
    """Construct a single Discord TextInput for one blank.

    Per-blank overrides honored:
        blank["prompt"]  — fully custom label, bypasses the axis lookup
        blank["example"] — custom placeholder example
    """
    resolved = resolve_blank(prompts, blank["pos"], blank.get("domain"), blank.get("form"), tier)

    if blank.get("prompt"):
        label_core = blank["prompt"]
    elif resolved:
        label_core = resolved["prompt"]
    else:
        label_core = "(fill something in)"

    if blank.get("example"):
        placeholder = f"e.g. {blank['example']}"
    elif resolved and resolved["examples"]:
        placeholder = f"e.g. {', '.join(resolved['examples'][:2])}"
    else:
        placeholder = label_core

    length_cap = (resolved["length_cap"] if resolved and resolved["length_cap"] else 100)
    max_length = min(length_cap, 1000)
    prefill = default[:max_length] if default else None
    return discord.ui.TextInput(
        label=f"Blank {position}: {label_core}"[:45],
        placeholder=placeholder[:100],
        style=discord.TextStyle.short,
        required=True,
        max_length=max_length,
        custom_id=blank["id"],
        default=prefill,
    )


def make_fill_modal(game_id, db, prompts, blanks, tier, on_submit_callback, existing_fills: dict | None = None):
    """Return the first modal page for any number of blanks.

    existing_fills: prior fills for this player, used to prefill inputs on resubmit.
    """
    existing_fills = existing_fills or {}
    if len(blanks) <= MODAL_PAGE_SIZE:
        return FillModal(game_id, db, prompts, blanks, tier, on_submit_callback, existing_fills=existing_fills)
    total_pages = math.ceil(len(blanks) / MODAL_PAGE_SIZE)
    return FillModalPage(
        game_id, db, prompts,
        page_blanks=blanks[:MODAL_PAGE_SIZE],
        remaining_blanks=blanks[MODAL_PAGE_SIZE:],
        tier=tier,
        fills_so_far={},
        on_submit_callback=on_submit_callback,
        page_num=1,
        total_pages=total_pages,
        existing_fills=existing_fills,
    )


class FillModal(discord.ui.Modal):
    """Single modal for templates with 1–5 blanks."""

    def __init__(self, game_id: str, db, prompts: dict, blanks: list[dict], tier: int, on_submit_callback, existing_fills: dict | None = None):
        super().__init__(title="Fill in the Blanks")
        self.game_id = game_id
        self.db = db
        self._blanks = blanks
        self._callback = on_submit_callback
        existing_fills = existing_fills or {}

        for i, blank in enumerate(blanks):
            field = _make_text_input(blank, prompts, tier, i + 1, default=existing_fills.get(blank["id"]))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted Fill modal in #%s", interaction.user.display_name, channel_name(interaction.channel))

        fills = {}
        errors = []
        for blank in self._blanks:
            child = discord.utils.get(self.children, custom_id=blank["id"])
            if child is None:
                continue
            val = strip_mass_mentions(child.value)
            err = validate_fill(val, child.max_length)
            if err:
                errors.append(f"Blank '{blank['id']}': {err}")
            else:
                fills[blank["id"]] = val

        if errors:
            await interaction.response.send_message("\n".join(errors), ephemeral=True)
            return

        await self._callback(interaction, fills, partial=False)


class FillModalPage(discord.ui.Modal):
    """One page of a multi-page fill flow. Chains to the next page automatically."""

    def __init__(self, game_id, db, prompts, page_blanks, remaining_blanks,
                 tier, fills_so_far, on_submit_callback, page_num, total_pages,
                 existing_fills: dict | None = None):
        super().__init__(title=f"Fill in the Blanks ({page_num}/{total_pages})")
        self.game_id = game_id
        self.db = db
        self._prompts = prompts
        self._page_blanks = page_blanks
        self._remaining_blanks = remaining_blanks
        self._tier = tier
        self._fills_so_far = fills_so_far
        self._callback = on_submit_callback
        self._page_num = page_num
        self._total_pages = total_pages
        self._existing_fills = existing_fills or {}

        offset = (page_num - 1) * MODAL_PAGE_SIZE
        for i, blank in enumerate(page_blanks):
            field = _make_text_input(blank, prompts, tier, offset + i + 1, default=self._existing_fills.get(blank["id"]))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted Fill modal page %d/%d in #%s",
                 interaction.user.display_name, self._page_num, self._total_pages,
                 channel_name(interaction.channel))

        fills_this_page = {}
        errors = []
        for blank in self._page_blanks:
            child = discord.utils.get(self.children, custom_id=blank["id"])
            if child is None:
                continue
            val = strip_mass_mentions(child.value)
            err = validate_fill(val, child.max_length)
            if err:
                errors.append(f"Blank '{blank['id']}': {err}")
            else:
                fills_this_page[blank["id"]] = val

        if errors:
            await interaction.response.send_message("\n".join(errors), ephemeral=True)
            return

        accumulated = {**self._fills_so_far, **fills_this_page}

        if self._remaining_blanks:
            uid = interaction.user.id
            def _persist(p):
                p.setdefault("submissions", {})[str(uid)] = {"fills": accumulated, "partial": True}
            await modify_payload(self.db, self.game_id, _persist)

            next_page_blanks = self._remaining_blanks[:MODAL_PAGE_SIZE]
            next_modal = FillModalPage(
                self.game_id, self.db, self._prompts,
                page_blanks=next_page_blanks,
                remaining_blanks=self._remaining_blanks[MODAL_PAGE_SIZE:],
                tier=self._tier,
                fills_so_far=accumulated,
                on_submit_callback=self._callback,
                page_num=self._page_num + 1,
                total_pages=self._total_pages,
                existing_fills=self._existing_fills,
            )
            start = self._page_num * MODAL_PAGE_SIZE + 1
            end = start + len(next_page_blanks) - 1
            view = _ContinueView(next_modal)
            await interaction.response.send_message(
                f"✅ Blanks 1–{self._page_num * MODAL_PAGE_SIZE} saved — "
                f"**click below to fill in blanks {start}–{end}**",
                view=view,
                ephemeral=True,
            )
        else:
            await self._callback(interaction, accumulated, partial=False)


class _ContinueView(discord.ui.View):
    def __init__(self, next_modal: "FillModalPage"):
        super().__init__(timeout=600)
        self._next_modal = next_modal

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.primary)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s clicked Continue in fill modal flow in #%s",
                 interaction.user.display_name, channel_name(interaction.channel))
        self.stop()
        await interaction.response.send_modal(self._next_modal)
