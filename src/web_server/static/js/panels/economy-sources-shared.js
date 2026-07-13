// Labels for the custom income sources — the JS mirror of TRIGGER_KINDS in
// bot_modules/economy/quests.py. Used by the Bank Manager quest form and the
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
};

// Kinds whose quests can carry a trigger-channel scope.
export const CHANNEL_SCOPED_KINDS = new Set(["media_post"]);
