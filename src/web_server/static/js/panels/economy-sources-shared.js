// Labels for the custom income sources — the JS mirror of TRIGGER_KINDS in
// bot_modules/economy/quests.py. Used by the Quests authoring form and the
// Income Sources page so the two never drift apart.
export const KIND_LABELS = {
  photo_reply: "📸 Reply to a Photo Challenge card with a photo",
  party_game: "🎲 Finish a party game",
  duel: "⚔️ Finish a duel / PvP challenge",
  risky_roll: "🎰 Take a Risky Roll dare",
  guess: "🕵️ Play a Guess Who round",
  voice_session: "🎙️ Be active in voice chat",
  qotd_reply: "📣 Answer the Question of the Day",
  starboard: "⭐ Get a message on the starboard",
  invite: "📨 Invite a new member",
  boost: "🚀 Boost the server",
  bio_set: "📇 Set or update your bio",
  media_post: "🖼️ Post an image (channel-scopable)",
  pen_pal: "💌 Get matched with a Pen Pal",
  message_sent: "💬 Send a message (channel-scopable)",
  reply_sent: "↩️ Reply to someone's message (channel-scopable)",
  reaction_given: "👍 React to someone's message (channel-scopable)",
  game_win: "🏆 Win a party game",
  duel_win: "🥇 Win a duel / PvP challenge",
  duel_lose: "🥈 Lose a duel / PvP challenge",
};

// Kinds whose quests can carry a trigger-channel scope.
export const CHANNEL_SCOPED_KINDS = new Set([
  "media_post", "message_sent", "reply_sent", "reaction_given",
]);
