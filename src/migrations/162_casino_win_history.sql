-- Migration 162: a rolling window of winning payouts, so the public big-win
-- broadcast can tell a genuinely rare haul from a merely large one.
--
-- WHY: the broadcast header now escalates with size (Big Win / Huge Win at
-- 1x/3x the guild's casino_broadcast_min_payout), and above that ladder sits
-- the rung that pings @here — defined as the top 3% of the wins this guild has
-- recently ANNOUNCED, rather than a fixed multiple. A multiple can't stay
-- honest: what counts as a big win drifts as the economy is retuned (19 dials
-- moved on 2026-07-30 alone) and differs ~8x between the two live guilds. It
-- also can't be guessed — a 10x rung shipped first and was measured against
-- prod the same day, where the largest win in 4,350 winning bets is 3,000
-- against a 500 bar, so it could never have rendered at all. A percentile
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
-- Populated from the cog's broadcast seam, ONE row per public announcement,
-- written after that announcement's percentile has been read. Not from
-- record_play: banking per settled bet would have counted a five-bet roulette
-- round five times for one card, banked jackpot spins whose big-win card is
-- deliberately suppressed, and — because record_play runs inside the settle
-- transaction, which commits before the broadcast reads — put each win into
-- the population it was about to be ranked against, so a payout tying the
-- recent maximum always cleared its own mark.
--
-- The population is announced wins only. Ranking EVERY win instead put the
-- mark below the broadcast bar, because the overwhelming majority of casino
-- wins are small pair payouts: prod's average stake is 36 coins and its
-- average win returns 71. That left the floor deciding everything and the
-- percentile contributing nothing.
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

-- Two guild-scoped indexes, because the two queries sort differently: the
-- percentile read is ORDER BY payout within one guild, and the trim walks the
-- same guild's rows by id. The primary key does NOT serve the trim — it is on
-- id alone, so a guild-scoped ORDER BY id falls back to a temp b-tree over the
-- whole window on every winning play, inside the settle transaction holding
-- the write lock. casino_ticker carries the same pair for the same reason
-- (idx_casino_ticker_guild, migration 128) at a fortieth of the row count.
CREATE INDEX IF NOT EXISTS idx_casino_win_history_guild_payout
    ON casino_win_history (guild_id, payout);
CREATE INDEX IF NOT EXISTS idx_casino_win_history_guild_id
    ON casino_win_history (guild_id, id);
