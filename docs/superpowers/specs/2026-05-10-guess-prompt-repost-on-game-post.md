# Guess: Repost Prompt After Game Post

## Problem

The sticky channel-bottom prompt (`GuessPromptView`) only repositions itself when a human sends a message in the guess channel. Bot messages are excluded from the `on_message` listener. When `SubmitPreviewView._on_post` posts a new game round, the bot message lands below the current prompt, leaving the prompt above the new game post.

## Fix

At the end of `SubmitPreviewView._on_post` (in `cogs/guess_cog.py`), after `game_msg` is successfully sent, call `_repost_prompt(self.bot, guess_channel, self.guild_id)` inside a try/except block — best-effort, consistent with how `_delayed_repost_prompt` handles failures.

No debouncing needed: game posts are intentional, non-bursty bot actions initiated by a human clicking "Post."

## Change Surface

- **File:** `cogs/guess_cog.py`
- **Method:** `SubmitPreviewView._on_post`
- **Change:** Add one `try/except` block calling `await _repost_prompt(...)` after `game_msg` is sent and the DB writes complete.
