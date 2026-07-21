"""Shared member-facing reply strings (see docs/embed_style_guide.md).

One string owns each recurring reply so features can't drift apart.
Error/denial replies open with the ❌ prefix (ruling 2026-07-21); prefer a
role-specific denial that says how to fix it where one exists.
"""

NO_PERMISSION = "❌ You don't have permission to use this command."
