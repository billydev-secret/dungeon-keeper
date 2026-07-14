"""Chat Revive starter pack — ~60 curated questions so the feature is useful
the moment it's turned on. Each entry is (category, nsfw, text). The spicy
questions are flagged adult-only and can only ever surface in channels Discord
marks age-restricted.
"""

from __future__ import annotations

STARTER_QUESTIONS: tuple[tuple[str, int, str], ...] = (
    # -- general -----------------------------------------------------------
    ("general", 0, "What's a skill you learned entirely by accident?"),
    ("general", 0, "What's the best purchase under $25 you've ever made?"),
    ("general", 0, "What tiny everyday thing instantly improves your mood?"),
    ("general", 0, "What's a smell that teleports you straight to a memory?"),
    ("general", 0, "What's the most useful thing you own that most people don't?"),
    ("general", 0, "What hobby did you pick up and drop the fastest?"),
    ("general", 0, "What's a habit you stole from someone else that stuck?"),
    ("general", 0, "What's the last thing that made you laugh out loud when you were alone?"),
    ("general", 0, "What's a piece of advice you ignored and later wished you hadn't?"),
    ("general", 0, "What's something you're weirdly good at for no reason?"),
    ("general", 0, "What's the most interesting thing you've read or watched this week?"),
    ("general", 0, "If you had a free hour right now, no obligations, what would you actually do?"),
    # -- deep --------------------------------------------------------------
    ("deep", 0, "What belief did you hold strongly five years ago that you've since dropped?"),
    ("deep", 0, "What's something you had to unlearn from how you grew up?"),
    ("deep", 0, "When was the last time you changed your mind about something big?"),
    ("deep", 0, "What compliment do you wish people gave you more often?"),
    ("deep", 0, "What's a small kindness from a stranger you still remember?"),
    ("deep", 0, "What part of your daily life would have amazed you as a kid?"),
    ("deep", 0, "What's a fear you've actually beaten, and how?"),
    ("deep", 0, "If your life had chapters, what would this one be called?"),
    ("deep", 0, "What do you know now that you wish you could tell your younger self — in one sentence?"),
    ("deep", 0, "What's something you do purely for yourself, with no audience?"),
    # -- silly -------------------------------------------------------------
    ("silly", 0, "What's the most cursed food combination you secretly enjoy?"),
    ("silly", 0, "You get one useless superpower. What do you pick?"),
    ("silly", 0, "What animal would be the most terrifying if it were the size of a horse?"),
    ("silly", 0, "What's the dumbest hill you're willing to die on?"),
    ("silly", 0, "What would your villain origin story be?"),
    ("silly", 0, "What's a completely normal thing that you do in a weird way?"),
    ("silly", 0, "If your pet could talk, what would it expose about you?"),
    ("silly", 0, "What's the worst name you could give a boat?"),
    ("silly", 0, "Which kitchen appliance are you, and why?"),
    ("silly", 0, "What's a sound effect you make out loud for no reason?"),
    ("silly", 0, "You must fight one hundred duck-sized horses or one horse-sized duck. Which, and what's your strategy?"),
    ("silly", 0, "What conspiracy theory would you start if you had to invent one?"),
    # -- photo -------------------------------------------------------------
    ("photo", 0, "Drop the last photo you took that you actually like."),
    ("photo", 0, "Show us your current desktop or phone wallpaper."),
    ("photo", 0, "Post the view from where you're sitting right now."),
    ("photo", 0, "Share a photo of something you made — any craft counts."),
    ("photo", 0, "Post the oldest photo on your phone you're willing to share."),
    ("photo", 0, "Show off the most chaotic corner of your room, no cleaning first."),
    # -- music -------------------------------------------------------------
    ("music", 0, "What song have you had on repeat lately?"),
    ("music", 0, "What's the best concert or live show you've ever been to?"),
    ("music", 0, "What's a song you loved as a teenager that still holds up?"),
    ("music", 0, "What artist would you put everyone in this server onto?"),
    ("music", 0, "What's your go-to karaoke song, real or hypothetical?"),
    ("music", 0, "What album could you listen to front-to-back with no skips?"),
    # -- food --------------------------------------------------------------
    ("food", 0, "What's your ride-or-die comfort meal?"),
    ("food", 0, "What food did you hate as a kid but love now?"),
    ("food", 0, "What's the best thing you know how to cook?"),
    ("food", 0, "Pineapple on pizza: make your case, either way."),
    ("food", 0, "What's a local food from your area everyone else is missing out on?"),
    ("food", 0, "What's your 3am gas-station snack of choice?"),
    # -- gaming ------------------------------------------------------------
    ("gaming", 0, "What game have you sunk the most hours into, and was it worth it?"),
    ("gaming", 0, "What's a game you love that nobody else seems to have played?"),
    ("gaming", 0, "What game world would you actually want to live in?"),
    ("gaming", 0, "What's your most controversial gaming opinion?"),
    ("gaming", 0, "What boss fight or level still haunts you?"),
    # -- spicy (adult-only; age-restricted channels only) --------------------
    ("spicy", 1, "What's your most embarrassing date story?"),
    ("spicy", 1, "What's a red flag you ignored that you'll never ignore again?"),
    ("spicy", 1, "What's the boldest thing you've ever done to get someone's attention?"),
    ("spicy", 1, "What's your unpopular opinion about modern dating?"),
    ("spicy", 1, "What's the worst pickup line ever used on you — or by you?"),
)
