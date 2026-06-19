-- Migration 054: seed FFA (Truth or Dare) prompts into the shared games
-- question bank. Previously these lived only in games_ffa/prompts.py; that
-- module is kept as the seed source and as an unfiltered-empty fallback.
-- 'truth'/'dare'/'nsfw' are reserved tags; the card label derives from the
-- truth/dare tag. Each INSERT is guarded so re-application can't duplicate.

INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s the most embarrassing thing you''ve ever done in front of a crowd?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the most embarrassing thing you''ve ever done in front of a crowd?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s a small lie you tell people all the time?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s a small lie you tell people all the time?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'Who in this server would you trust with your deepest secret?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Who in this server would you trust with your deepest secret?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s the pettiest reason you''ve ever stopped talking to someone?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the pettiest reason you''ve ever stopped talking to someone?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s the most childish thing you still do as an adult?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the most childish thing you still do as an adult?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s a compliment you secretly give yourself?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s a compliment you secretly give yourself?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s the worst gift you''ve ever received and pretended to like?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the worst gift you''ve ever received and pretended to like?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s something everyone seems to love that you just don''t get?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s something everyone seems to love that you just don''t get?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s the most trouble you ever got into as a kid?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the most trouble you ever got into as a kid?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s a habit of yours that would annoy a roommate?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s a habit of yours that would annoy a roommate?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s the last thing you searched on your phone?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the last thing you searched on your phone?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s the cringiest phase you ever went through?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the cringiest phase you ever went through?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'If you could read one person''s mind in this server, whose would it be?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'If you could read one person''s mind in this server, whose would it be?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s a talent you have that almost no one knows about?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s a talent you have that almost no one knows about?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth"]', 'What''s the longest you''ve gone without showering?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the longest you''ve gone without showering?');

INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'Who was the last person who made you blush, and why?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Who was the last person who made you blush, and why?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s your biggest turn-on that you don''t usually admit?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s your biggest turn-on that you don''t usually admit?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s the boldest thing you''ve ever done to get someone''s attention?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the boldest thing you''ve ever done to get someone''s attention?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'Describe your ideal first kiss in one sentence.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Describe your ideal first kiss in one sentence.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s a fantasy you''ve never told anyone about?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s a fantasy you''ve never told anyone about?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s the most spontaneous hookup story you''re willing to share?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the most spontaneous hookup story you''re willing to share?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s something you find unexpectedly attractive in a person?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s something you find unexpectedly attractive in a person?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'Who in your DMs right now would you actually go on a date with?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Who in your DMs right now would you actually go on a date with?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s the cheesiest pickup line that''s actually worked on you?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the cheesiest pickup line that''s actually worked on you?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s the riskiest place you''ve ever made out?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the riskiest place you''ve ever made out?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s a secret you''d only tell someone you trust completely?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s a secret you''d only tell someone you trust completely?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s the last thing that gave you butterflies?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the last thing that gave you butterflies?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s a relationship ''ick'' that instantly ruins it for you?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s a relationship ''ick'' that instantly ruins it for you?');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["truth","nsfw"]', 'What''s the most flirtatious text you''ve ever sent?'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'What''s the most flirtatious text you''ve ever sent?');

INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Post the most recent photo in your camera roll (keep it clean!).'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Post the most recent photo in your camera roll (keep it clean!).');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Send a voice note singing the chorus of the last song you listened to.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Send a voice note singing the chorus of the last song you listened to.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Type your next 3 messages in all caps.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Type your next 3 messages in all caps.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Change your nickname to whatever the person above you suggests for 10 minutes.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Change your nickname to whatever the person above you suggests for 10 minutes.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Do your best impression of someone in this server and post it as a voice note.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Do your best impression of someone in this server and post it as a voice note.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Send the 4th emoji in your recently-used list and explain why it''s there.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Send the 4th emoji in your recently-used list and explain why it''s there.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Write a haiku about the last thing you ate.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Write a haiku about the last thing you ate.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Talk in rhymes for your next 2 replies.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Talk in rhymes for your next 2 replies.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Post a screenshot of your home screen.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Post a screenshot of your home screen.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Compliment three different people in the thread right now.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Compliment three different people in the thread right now.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Send a voice note reading your last text in your most dramatic voice.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Send a voice note reading your last text in your most dramatic voice.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare"]', 'Set your status to something the thread picks for the next 10 minutes.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Set your status to something the thread picks for the next 10 minutes.');

INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Send a voice note moaning the name of your latest crush.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Send a voice note moaning the name of your latest crush.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Describe your flirting style in one spicy sentence.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Describe your flirting style in one spicy sentence.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Send the last flirty text you sent (you can censor the name).'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Send the last flirty text you sent (you can censor the name).');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Rate the thread on a scale of 1-10 and say who''d you''d shoot your shot with.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Rate the thread on a scale of 1-10 and say who''d you''d shoot your shot with.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Send a voice note saying something you''d whisper to someone you like.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Send a voice note saying something you''d whisper to someone you like.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Confess the most scandalous thought you''ve had today.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Confess the most scandalous thought you''ve had today.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Describe your ''type'' in explicit-but-tasteful detail.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Describe your ''type'' in explicit-but-tasteful detail.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Send your boldest pickup line as a voice note.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Send your boldest pickup line as a voice note.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Tell the thread your green flag that makes you irresistible.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Tell the thread your green flag that makes you irresistible.');
INSERT INTO games_question_bank (game_type, tags, question_text)
SELECT 'ffa', '["dare","nsfw"]', 'Describe the last dream you had that you''d be embarrassed to share.'
WHERE NOT EXISTS (SELECT 1 FROM games_question_bank
                  WHERE game_type = 'ffa' AND question_text = 'Describe the last dream you had that you''d be embarrassed to share.');
