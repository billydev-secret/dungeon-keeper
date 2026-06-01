"""Privacy: data-deletion business logic extracted from privacy_cog.

The cog (``bot_modules.cogs.privacy_cog``) drives the Discord side of the
``/delete_me`` / ``/delete_user`` flow. This package holds the parts that
are pure Python — message bucketing, snowflake-time partitioning, progress
bar rendering, ephemeral status text — so they can be unit-tested without
spinning up a Discord client or a fake interaction.

The DB-side purge has its own module under
``bot_modules.services.privacy_service``.
"""
