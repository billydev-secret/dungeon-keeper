-- Migration 017: add birthday request/preference field
ALTER TABLE member_birthdays ADD COLUMN preference TEXT;
