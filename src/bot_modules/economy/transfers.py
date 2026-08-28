"""Payment receipt rendering for ``/bank pay``.

Two decisions live here rather than in the cog, because both are the kind
that go wrong silently:

**The balance never appears on a public receipt.** ``finalize_pay`` has always
footered the receipt with the sender's balance, which is fine in an ephemeral
reply and is a balance leak the moment the same embed is posted to a channel.
:func:`build_payment_receipt` refuses to render the footer in the public
variant *even when handed a balance*, so a caller can't leak it by forgetting
to pass ``None`` — and a test asserts exactly that.

**A public receipt can be silently downgraded.** :func:`receipt_is_public` is
the single place the downgrade rule lives; see its docstring for why the two
reasons it downgrades are deliberately indistinguishable.
"""

from __future__ import annotations

import discord

from bot_modules.economy.view_helpers import unit
from bot_modules.services.economy_service import EconSettings


def receipt_is_public(requested: bool, *, blocked: bool, can_post: bool) -> bool:
    """Whether the receipt for this payment may be posted to the channel.

    ``blocked`` is the no-contact verdict for the two members; ``can_post``
    is whether the bot may actually speak in the invoking channel.

    Both downgrades are silent and produce the *ordinary* ephemeral receipt,
    with no note explaining why nothing was posted. That is deliberate and
    load-bearing: a message saying "couldn't post that publicly" would be a
    probe — a sender could tick ``public`` on a 1-coin payment and read the
    reply to learn whether the recipient has a no-contact rule against them,
    which is the one thing docs/no_contact_spec.md says a gate must never
    disclose. Folding the permissions case into the same silent path is what
    makes the no-contact case unremarkable. The cost, accepted knowingly, is
    that a genuine "bot can't post here" misconfiguration also looks like
    nothing happened; the cog logs it.
    """
    return requested and not blocked and can_post


def build_payment_receipt(
    settings: EconSettings,
    accent: discord.Color | None,
    sender: discord.abc.User,
    recipient: discord.abc.User,
    amount: int,
    *,
    memo: str | None = None,
    balance: int | None = None,
    public: bool = False,
) -> discord.Embed:
    """The "Payment Sent" embed, in its private and public variants.

    The private variant is the sender's own ephemeral receipt: the recipient
    is named, the sender is not (they're the only one reading it), and the
    footer carries their new balance.

    The public variant names *both* members — it lands in the channel as a
    followup, which Discord renders as a bare bot message with no "so-and-so
    used /bank pay" attribution, so an embed that named only the recipient
    would post a payment from nobody. It carries no footer: ``balance`` is
    ignored here rather than trusted, because the leak this guards against
    is a caller forgetting to withhold it.

    ``memo`` is expected pre-cleaned and is markdown-escaped here. It renders
    in both variants — a member who types a note and ticks ``public`` is
    publishing it, which the command option says in as many words.
    """
    safe_memo = discord.utils.escape_markdown(memo) if memo else None
    amount_text = f"{settings.currency_emoji} **{amount:,}** {unit(settings, amount)}"
    desc = (
        f"{amount_text} · {sender.mention} → {recipient.mention}"
        if public
        else f"{amount_text} → {recipient.mention}"
    )
    if safe_memo:
        desc += f"\n\n*{safe_memo}*"

    embed = discord.Embed(
        title=f"{settings.currency_emoji} Payment Sent",
        description=desc,
        color=accent,
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)
    if not public and balance is not None:
        embed.set_footer(text=f"Your balance: {balance:,}")
    return embed
