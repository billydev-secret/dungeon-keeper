-- Migration 199: voice transcription can clear the audio it replaced (2026-08-31).
--
-- The transcript used to be a reply hanging off the voice message. Once the
-- audio is deleted a reply renders as a dangling "original message was
-- deleted" stub, so the transcript becomes a standalone post from the bot that
-- carries the speaker's name itself. That is a change to how the transcript
-- looks whether or not the new dial is on, and it is the reason the dial can
-- exist at all.
--
-- Off by default, and deliberately so: deleting the audio is irreversible and
-- `base.en` is a small model, so a misheard word can never afterwards be
-- checked against the recording. A guild opts in.
--
-- No per-user data: the column is a per-guild switch and names no member, so
-- no docs/data_register.md row. The transcript text itself lives only in the
-- Discord message the bot posts -- nothing here stores it.

ALTER TABLE voice_transcription_config
    ADD COLUMN delete_after_transcribe INTEGER NOT NULL DEFAULT 0;
