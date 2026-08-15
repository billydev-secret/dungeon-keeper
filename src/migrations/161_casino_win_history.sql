-- Migration 161: a rolling window of winning payouts, so the public big-win
-- broadcast can tell a genuinely rare haul from a merely large one.
--
-- WHY: the broadcast header now escalates with size (Big Win / Huge Win /
-- Monster Win at 1x/3x/10x the guild's casino_broadcast_min_payout), and the
-- top of that ladder — the one that pings @here — is defined as the top 3% of
-- recent winnings rather than a fixed multiple. A multiple can't stay honest:
-- what counts as a big win drifts as the economy is retuned (19 dials moved on
-- 2026-07-30 alone) and differs ~8x between the two live guilds. A percentile
-- re-reads the room on every play; a constant would have to be re-tuned by
-- hand and quietly wouldn't be.
--
-- Nothing existing could answer the question. casino_ticker records every play
-- across all nine games but keeps only TICKER_KEEP = 25 rows per guild — a
-- sample far too small for a 97th percentile — and casino_member_stats holds
-- sums, which no percentile can be recovered from. Raising the ticker's
-- retention was the cheap option and is the wrong one: those rows carry
-- user_id, so it would have silently retained 20x more per-member play history
-- to power a header.
--
-- NO user_id, DELIBERATELY. This table answers exactly one question — "how big
-- is a big win around here lately" — and that question never needs to know who
-- won. Storing only (guild_id, payout, ts) keeps it outside personal data
-- entirely: nothing for purge_user_data to clear, no data_register.md row, no
-- access-request surface. It is the minimization default doing real work
-- rather than a note promising it.
--
-- Populated from record_play() in the settle transaction, for the nine games
-- that can broadcast (TICKER_GAMES) and only when payout > stake — a push is
-- not a win and would drag the percentile down. Pools is excluded with the
-- ticker: its day-long parimutuel payouts are a different distribution and
-- settle on their own panel, so folding them in would skew the bar for games
-- they never share a channel moment with.
--
-- Trimmed to WIN_HISTORY_KEEP rows per guild on insert, the same pattern
-- casino_ticker already uses. The window is what makes it self-tuning: an
-- all-time percentile would calcify around whatever the economy looked like
-- years ago.
--
-- Starts empty, and that is handled rather than papered over: under
-- PING_MIN_SAMPLE rows the percentile is refused outright and no broadcast can
-- ping, so a fresh guild cannot @here its very first win off a sample of one.

CREATE TABLE IF NOT EXISTS casino_win_history (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    payout   INTEGER NOT NULL,
    ts       REAL    NOT NULL
);

-- The percentile read is ORDER BY payout within one guild; the trim walks the
-- same rows by id. One composite index serves the read, and the primary key
-- serves the trim.
CREATE INDEX IF NOT EXISTS idx_casino_win_history_guild_payout
    ON casino_win_history (guild_id, payout);
