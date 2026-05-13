# Veil Prompt Repost After Game Post Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a Veil game round is posted to the channel, reposition the sticky channel-bottom prompt so it appears below the new round.

**Architecture:** Add a best-effort `_repost_prompt(...)` call at the end of `SubmitPreviewView._on_post` in `cogs/veil_cog.py`. The function already exists and handles its own errors; the new call mirrors the pattern in `_delayed_repost_prompt`.

**Tech Stack:** Python, discord.py, pytest-asyncio, unittest.mock

---

### Task 1: Repost prompt after game post

**Files:**
- Modify: `cogs/veil_cog.py` (end of `SubmitPreviewView._on_post`)
- Test: `tests/cogs/test_veil_submit.py`

- [ ] **Step 1: Write the failing test**

Add this test to `tests/cogs/test_veil_submit.py`:

```python
@pytest.mark.asyncio
async def test_on_post_reposts_prompt_after_game_message():
    """After posting a game round, _repost_prompt is called to move the
    sticky status bar below the new round."""
    from cogs.veil_cog import SubmitPreviewView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()

    fake_channel = MagicMock(spec=discord.TextChannel)
    fake_channel.is_nsfw = MagicMock(return_value=True)
    fake_channel.send = AsyncMock(return_value=_fake_game_message())

    guild = FakeGuild(id=GUILD_ID)
    guild.channels[VEIL_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(guild=guild)
    interaction.guild.get_channel = lambda cid: guild.channels.get(cid)

    view = SubmitPreviewView(
        bot,
        crops=[b"fake-crop"],
        guild_id=GUILD_ID,
        veil_channel_id=VEIL_CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=1,
    )

    with patch("cogs.veil_cog._do_insert_round", return_value=42), \
         patch("cogs.veil_cog._do_update_round_message"), \
         patch("cogs.veil_cog._do_audit"), \
         patch("cogs.veil_cog._repost_prompt", new_callable=AsyncMock) as mock_repost:
        await view._on_post(interaction)

    mock_repost.assert_awaited_once_with(bot, fake_channel, GUILD_ID)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/cogs/test_veil_submit.py::test_on_post_reposts_prompt_after_game_message -v`

Expected: FAIL — `mock_repost.assert_awaited_once_with` will fail because `_repost_prompt` is never called.

- [ ] **Step 3: Add the `_repost_prompt` call to `_on_post`**

In `cogs/veil_cog.py`, at the end of `SubmitPreviewView._on_post` — after `await interaction.edit_original_response(...)` — add:

```python
        try:
            await _repost_prompt(self.bot, veil_channel, self.guild_id)
        except Exception:
            log.exception("veil: prompt repost after game post failed for guild %d", self.guild_id)
```

The full tail of `_on_post` becomes:

```python
        await interaction.edit_original_response(
            content=f"✅ Posted to {veil_channel.mention}!",
            view=self,
        )

        try:
            await _repost_prompt(self.bot, veil_channel, self.guild_id)
        except Exception:
            log.exception("veil: prompt repost after game post failed for guild %d", self.guild_id)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/cogs/test_veil_submit.py::test_on_post_reposts_prompt_after_game_message -v`

Expected: PASS

- [ ] **Step 5: Run the full veil test suite to check for regressions**

Run: `pytest tests/cogs/test_veil_submit.py tests/cogs/test_veil_prompt.py -v`

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add cogs/veil_cog.py tests/cogs/test_veil_submit.py
git commit -m "feat(veil): repost prompt after game round is posted"
```
