from __future__ import annotations

import asyncio
import datetime
import discord
import json

from collections import Counter
from datetime import datetime, timedelta, timezone
from discord import app_commands
from typing import NamedTuple


MAX_MESSAGES = 400
MAX_CHARS_PER_MSG = 240
MAX_TOTAL_CHARS = 40_000
SAFE_TEXT_CHUNK = 1900


class UserMsg(NamedTuple):
    created_at: datetime
    channel_id: int
    channel_mention: str
    jump_url: str
    content: str
    mentions: list[str]
    reply_to: str | None
    reply_content: str | None


def extract_json_object(s: str):
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass

    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None
    return None


def build_transcript(lines: list[str]) -> str:
    out = []
    total = 0
    for line in lines:
        if total + len(line) > MAX_TOTAL_CHARS:
            break
        out.append(line)
        total += len(line)
    return "\n".join(out)


async def llm_user_review(ctx, member: discord.Member, transcript: str, stats: dict):
    prompt = f"""
        You are helping moderators review a user for promotion.

        User: {member} (id {member.id})
        Window: last {stats['hours']} hours
        Messages included: {stats['found']}
        Channels posted in: {stats['unique_channels_posted']}

        You will receive a transcript where each message line is numbered.

        Your job:
        1. Write a promotion candidate report

        Write a concise MOD-ONLY report in Markdown:
            ## Activity snapshot
            - posting frequency (based on transcript), breadth of channels, consistency

            ## Themes & participation style
            - what they talk about, how they engage (questions, support, jokes, etc.)

            ## Consent / BDSM rules & boundaries (if applicable)
            - flag any patterns that suggest consent issues, coercion, DM pressure, boundary pushing, unsafe framing
            - be careful: consensual flirting and kink discussion is allowed
            - if there’s insufficient evidence, say so

            ## Tone & community fit
            - respectful? supportive? chronic conflict? chronic negativity? (only if supported)

            ## Recommendation
            - “Looks good for promotion” / “Needs mod check-in” / “Insufficient data”
            - 1–2 sentences why

        2. Identify up to:
           - 5 messages that indicate poor conduct around consent and boundary respect. Let's look for negative sentament as well.
           - 5 messages that demonstrate positive conduct

        Return ONLY valid JSON in this format:

        {{
          "summary": "markdown summary",
          "poor_indices": [3, 18],
          "good_indices": [5, 7, 22]
        }}

        Rules:
        - Only select messages that truly stand out.
        - If none exist, return empty arrays.
        - Do not invent anything.
        """

    numbered_lines = [f"{idx}. {line}" for idx, line in enumerate(transcript.splitlines(), start=1)]
    resp = await asyncio.to_thread(
        ctx.client.chat.completions.create,
        model=ctx.bigmodel,
        messages=[
            {"role": "system", "content": "You are a careful moderation analyst. Output valid JSON only."},
            {"role": "user", "content": prompt + "\n\nTRANSCRIPT:\n" + "\n".join(numbered_lines)},
        ],
        temperature=0.2,
    )
    raw = resp.choices[0].message.content.strip()
    return extract_json_object(raw)


async def llm_summarize(ctx, channel_name: str, transcript: str, hours: int) -> str:
    summary_prompt = f"""
        You are summarizing a Discord channel for moderators.

        Channel: #{channel_name}
        Time window: last {hours} hours

        Output in Markdown with these sections:

        ## Themes (3–6 bullets)
        ## Notable moments (bullets)
        ## Participation
        - Activity level: low/medium/high
        - Top participants (approx counts if possible)
        - Threading pattern: (few long threads / many short exchanges)

        ## Tone & climate
        - Overall vibe (1–2 sentences)
        - Venting present? (yes/no + neutral note)
        - If negativity appears: classify as normal venting vs targeted negativity vs repeated downer framing

        ## Potential friction / discomfort (ranked)
        For each item: category tag, confidence 0–1, and suggested soft mod action.
        Only include if supported by transcript.

        ## Action items / follow-ups
        Only list explicit decisions or asks. If none, say “None observed.”

        Rules:
        - Do not moralize; keep neutral.
        - Do not invent facts; if unsure, say “insufficient data.”
        - No long quotes; paraphrase.
        """

    resp = await asyncio.to_thread(
        ctx.client.chat.completions.create,
        model=ctx.model,
        messages=[
            {"role": "system", "content": "You are a helpful, careful moderation summarizer."},
            {"role": "user", "content": summary_prompt + "\n\nTRANSCRIPT:\n" + transcript},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()


async def collect_user_messages(ctx, guild: discord.Guild, member: discord.Member, hours: int = 168, max_msgs: int = 200, per_channel_limit: int = 300) -> tuple[list[UserMsg], dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    found: list[UserMsg] = []
    scanned_channels = 0
    skipped_no_access = 0
    scanned_msgs_total = 0
    per_channel_hits = Counter()

    for ch in guild.text_channels:
        if len(found) >= max_msgs:
            break
        scanned_channels += 1
        me = ctx.get_bot_member(guild)
        if me and not ch.permissions_for(me).read_message_history:
            skipped_no_access += 1
            continue
        try:
            async for msg in ch.history(limit=per_channel_limit, after=cutoff, oldest_first=False):
                scanned_msgs_total += 1
                if msg.author.id != member.id or not msg.content:
                    continue
                content = msg.content.replace("\n", " ").strip()
                if not content:
                    continue

                reply_to = None
                reply_content = None
                if msg.reference:
                    ref = msg.reference
                    if isinstance(ref.resolved, discord.Message):
                        reply_to = ref.resolved.author.display_name
                        if ref.resolved.content:
                            reply_content = ref.resolved.content[:120]
                    elif ref.message_id:
                        try:
                            ref_channel = guild.get_channel(ref.channel_id)
                            if isinstance(ref_channel, discord.TextChannel):
                                fetched = await ref_channel.fetch_message(ref.message_id)
                                reply_to = fetched.author.display_name
                                if fetched.content:
                                    reply_content = fetched.content[:120]
                        except (discord.NotFound, discord.Forbidden):
                            pass

                found.append(
                    UserMsg(
                        created_at=msg.created_at,
                        channel_id=ch.id,
                        channel_mention=ch.mention,
                        jump_url=f"https://discord.com/channels/{guild.id}/{ch.id}/{msg.id}",
                        content=content[:MAX_CHARS_PER_MSG],
                        mentions=[m.display_name for m in msg.mentions],
                        reply_to=reply_to,
                        reply_content=reply_content,
                    )
                )
                per_channel_hits[ch.id] += 1
                if len(found) >= max_msgs:
                    break
        except discord.Forbidden:
            skipped_no_access += 1
            continue

    found.sort(key=lambda m: m.created_at)
    stats = {
        "hours": hours,
        "max_msgs": max_msgs,
        "found": len(found),
        "scanned_channels": scanned_channels,
        "skipped_no_access": skipped_no_access,
        "scanned_msgs_total": scanned_msgs_total,
        "unique_channels_posted": len({m.channel_id for m in found}),
        "top_channels": per_channel_hits.most_common(5),
        "cutoff": cutoff,
    }
    return found, stats


def format_user_transcript(items: list[UserMsg]) -> str:
    lines = []
    for m in items:
        ts = m.created_at.strftime("%Y-%m-%d %H:%M")
        meta_parts = []
        if m.reply_to:
            meta_parts.append(f"reply_to={m.reply_to}")
        if m.reply_content:
            meta_parts.append(f"reply_excerpt='{m.reply_content}'")
        if m.mentions:
            meta_parts.append(f"mentions={','.join(m.mentions)}")
        meta = f" ({' | '.join(meta_parts)})" if meta_parts else ""
        lines.append(f"[{ts}] {m.channel_mention}{meta}: {m.content}")
    return build_transcript(lines)


async def send_markdown(channel: discord.TextChannel | discord.Thread, text: str) -> None:
    while text:
        chunk = text[:1800]
        text = text[1800:]
        await channel.send(f"```markdown\n{chunk}\n```")


async def send_ephemeral_markdown(interaction: discord.Interaction, text: str) -> None:
    while text:
        chunk = text[:1800]
        text = text[1800:]
        await interaction.followup.send(f"```markdown\n{chunk}\n```", ephemeral=True)


def chunk_text(text: str, limit: int = SAFE_TEXT_CHUNK) -> list[str]:
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


async def send_ephemeral_text(interaction: discord.Interaction, text: str) -> None:
    for chunk in chunk_text(text):
        await interaction.followup.send(chunk, ephemeral=True)


def format_member_activity_line(member: discord.Member, activity) -> str:
    if activity is None:
        return f"{member.display_name} - no recorded message yet"

    created_at = int(activity.created_at)
    if getattr(activity, "channel_id", 0) <= 0:
        return (
            f"{member.display_name} - last seen <t:{created_at}:R> "
            f"(<t:{created_at}:f>)"
        )
    return (
        f"{member.display_name} - last seen <t:{created_at}:R> "
        f"(<t:{created_at}:f>) in <#{activity.channel_id}>"
    )


def register_reports(bot: discord.Client, ctx) -> None:
    @bot.tree.command(name="summarize", description="Summarize this channel over a time window.", guild=discord.Object(id=ctx.guild_id) if ctx.debug else None)
    @app_commands.describe(hours="How many hours back to summarize (e.g., 24, 72).")
    async def summarize(interaction: discord.Interaction, hours: int = 24):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("This command only works in text channels.", ephemeral=True)
            return

        after_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        lines = []
        count = 0
        async for msg in channel.history(limit=None, after=after_dt, oldest_first=True):
            if msg.author.bot or not msg.content:
                continue
            content = msg.content.replace("\n", " ").strip()
            if not content:
                continue
            lines.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author.display_name}: {content[:MAX_CHARS_PER_MSG]}")
            count += 1
            if count >= MAX_MESSAGES:
                break

        if not lines:
            await interaction.followup.send(f"No messages found in the last {hours}h.", ephemeral=True)
            return

        summary = await llm_summarize(ctx, channel.name, build_transcript(lines), hours)
        await send_ephemeral_markdown(interaction, summary)

    @bot.tree.command(name="listrole", description="List all members in a role", guild=discord.Object(id=ctx.guild_id) if ctx.debug else None)
    @app_commands.describe(role="The role to inspect")
    async def listrole(interaction: discord.Interaction, role: discord.Role):
        if not role.members:
            await interaction.response.send_message(f"No members found in **{role.name}**.", ephemeral=True)
            return
        output = "\n".join(member.display_name for member in role.members)
        if len(output) > 1900:
            output = output[:1900] + "\n... (truncated)"
        await interaction.response.send_message(f"**Members in {role.name}:**\n{output}", ephemeral=True)

    @bot.tree.command(name="inactive_role", description="Report inactivity for a role", guild=discord.Object(id=ctx.guild_id) if ctx.debug else None)
    @app_commands.describe(role="Role to analyze", days="Number of days to check (default 7)")
    async def inactive_role(interaction: discord.Interaction, role: discord.Role, days: app_commands.Range[int, 1, 60] = 7):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command only works in a server.", ephemeral=True)
            return
        cutoff = discord.utils.utcnow() - timedelta(days=days)
        cutoff_ts = cutoff.timestamp()
        role_members = sorted(role.members, key=lambda current: current.display_name.lower())
        role_member_ids = [current.id for current in role_members]
        with ctx.open_db() as conn:
            activities = ctx.get_member_last_activity_map(conn, guild.id, role_member_ids)

        inactive_members = [
            current
            for current in role_members
            if activities.get(current.id) is None or activities[current.id].created_at < cutoff_ts
        ]
        total = len(role_members)
        inactive_count = len(inactive_members)
        percent = (inactive_count / total * 100) if total else 0
        summary = (
            f"**Role Activity Report — {role.name} ({days} days)**\n"
            f"Total Members: {total}\n"
            f"Inactive: {inactive_count} ({percent:.1f}%)\n"
            f"Tracking Coverage: {len(activities)}/{total}\n"
            f"----------------------------------\n"
        )
        if inactive_members:
            block = "\n".join(format_member_activity_line(current, activities.get(current.id)) for current in inactive_members)
            summary += "\n**Inactive Members:**\n" + block
        else:
            summary += "\nAll members active in this period."
        if any(current.id not in activities for current in inactive_members):
            summary += "\n\nSome members have no recorded message yet because activity tracking starts after this version is deployed."
        await send_ephemeral_text(interaction, summary)

    @bot.tree.command(name="user_review", description="Review a user's recent message history (for promotions/mod check-ins).", guild=discord.Object(id=ctx.guild_id) if ctx.debug else None)
    @app_commands.describe(member="User to review", hours="How many hours back (default 168 = 7 days)", max_msgs="Max messages to include (default 200)")
    async def user_review(interaction: discord.Interaction, member: discord.Member, hours: app_commands.Range[int, 1, 720] = 168, max_msgs: app_commands.Range[int, 20, 500] = 200):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Guild context missing.", ephemeral=True)
            return

        items, stats = await collect_user_messages(ctx, guild, member, hours=hours, max_msgs=max_msgs, per_channel_limit=400)
        if not items:
            await interaction.followup.send("No messages found in that window (or I lack access).", ephemeral=True)
            return

        analysis = await llm_user_review(ctx, member, format_user_transcript(items), stats)
        if not analysis:
            await interaction.followup.send("LLM analysis failed.", ephemeral=True)
            return

        await interaction.followup.send(
            (
                f"**User Review - {member.mention}**\n"
                f"Window: last {hours}h | Messages: {stats['found']} | Channels: {stats['unique_channels_posted']}\n"
                f"Requested by {interaction.user.mention}"
            ),
            ephemeral=True,
        )

        await send_ephemeral_markdown(interaction, analysis.get("summary", "No summary provided."))

        def build_quote_block(index_list, label):
            blocks = [f"**{label}"]
            for i in index_list:
                if 1 <= i <= len(items):
                    msg = items[i - 1]
                    snippet = msg.content if len(msg.content) < 400 else msg.content[:400] + "…"
                    blocks.append(f"**{msg.channel_mention} [{msg.created_at.strftime('%Y-%m-%d %H:%M')}]**\n> {snippet}\n")
            return blocks

        poor_blocks = build_quote_block(analysis.get("poor_indices", []), "⚠️ Needs Review")
        good_blocks = build_quote_block(analysis.get("good_indices", []), "✅ Positive Conduct")
        if poor_blocks:
            await send_ephemeral_markdown(interaction, "\n\n".join(poor_blocks))
        if good_blocks:
            await send_ephemeral_markdown(interaction, "\n\n".join(good_blocks))
