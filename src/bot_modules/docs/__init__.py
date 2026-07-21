"""Docs — single-source markdown documents rendered as embeds in many places.

Layout mirrors the ``bios`` package:

- ``render``  pure markdown → embed-spec logic (no discord import; unit-tested)
- ``db``      CRUD + placement bookkeeping over the ``docs`` tables
- ``sync``    reconcile rendered embeds against live Discord messages
"""
