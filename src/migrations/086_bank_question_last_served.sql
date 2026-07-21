-- Round-robin banked-question selection.
--
-- Small per-category pools (traditional TOD's ~20 questions/category, nhie's
-- 7, etc.) were served with pure random.choice, which has no memory across
-- separate game sessions — the same handful of questions could resurface
-- within a few games purely by chance. last_served_at lets selection prefer
-- the least-recently-served row (NULL = never served, sorts first) so a pool
-- doesn't repeat a question until every row in it has been served once.
ALTER TABLE games_question_bank ADD COLUMN last_served_at TIMESTAMP;
