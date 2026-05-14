import re
import discord


async def check_consent(db, user_id: int) -> bool:
    """Returns True if user has opted in to the consent system."""
    return True  # consent system disabled


async def format_name(db, member: discord.Member) -> str:
    """Returns mention string if consented, display name if not."""
    if await check_consent(db, member.id):
        return member.mention
    return member.display_name


async def scan_mentions_for_consent(
    db, message: discord.Message, game_channel_ids: set
) -> bool:
    """
    Called from on_message. If message is in an active game channel
    and mentions a non-consenting user, delete the message and
    send a polite notification. Returns True if message was deleted.
    """
    if message.channel.id not in game_channel_ids:
        return False
    if not message.mentions:
        return False
    for mentioned_user in message.mentions:
        if mentioned_user.bot:
            continue
        if not await check_consent(db, mentioned_user.id):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await message.channel.send(
                f"{message.author.mention} Your message was removed because "
                f"**{mentioned_user.display_name}** hasn't opted into the consent system yet. "
                f"They can opt in anytime with `/consent`.",
                delete_after=15,
            )
            return True
    return False


def extract_mentioned_ids(content: str) -> list[int]:
    """Extract user IDs from Discord mention strings like <@123456789>."""
    return [int(uid) for uid in re.findall(r"<@!?(\d+)>", content)]
