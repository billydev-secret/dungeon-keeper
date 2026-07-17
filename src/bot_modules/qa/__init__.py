"""QA Tracker — pure helpers shared by the cog and the post-commit hook.

Keep this package stdlib-only: ``scripts/post_testing_docs.py`` (a
standalone, dependency-free hook script) imports it to build cards over
raw REST, so nothing here may import discord or the DB layer.
"""
