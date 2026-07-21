BRAND_COLOR = 0xDAA520  # Goldenrod
WARNING_COLOR = 0xFF6B35
SUCCESS_COLOR = 0x57F287
ERROR_COLOR = 0xED4245

# Game phase colors — used consistently across all cogs
PHASE_JOINING  = 0xDAA520   # goldenrod  — lobby / join
PHASE_PLAYING  = 0x4E9AF1   # blue       — active round
PHASE_RESULTS  = 0x57F287   # green      — round results
PHASE_RECAP    = 0xB8860B   # dark gold  — final recap / game over

# Clapback-specific colors
CLAPBACK_COLOR = 0xFF4500       # Orange-red (main game)
CLAPBACK_VOTE_COLOR = 0x5865F2  # Blurple (voting phase)
CLAPBACK_WIN_COLOR = 0xFFD700   # Gold (winner / CLAPBACK moments)
CLAPBACK_TIE_COLOR = 0x99AAB5   # Gray (ties)

GAME_ICONS = {
    'ffa': '🎭',
    'ffa_banner': '🃏',
    # 'photo' is intentionally absent — Photo Challenge left the games menu and
    # the /games help list (it's scheduled-only now). GAME_NAMES keeps its
    # display name for logs/scheduler lookups.
    'traditional': '🎲',
    'compliment': '💛',
    'mfk': '💍',
    'wyr': '🤔',
    'nhie': '⛔',
    'mlt': '👑',
    'ttl': '🤥',
    'hottakes': '🔥',
    'story': '📖',

    'ama': '🎙️',
    'fantasies': '✨',
    'price': '💰',
    'rushmore': '🗿',
    'clapback': '⚔️',
    'legitlibs': '📝',
    'pressure': '♨️',
    'risky_roll': '🎰',
}

GAME_NAMES = {
    'ffa': 'Anonymous Truth or Dare',
    'ffa_banner': 'Truth or Dare Card',
    'photo': 'Photo Challenge',
    'traditional': 'Truth or Dare',
    'compliment': 'Spin the Compliment',
    'mfk': 'Marry, Fornicate, Kiss',
    'wyr': 'Would You Rather',
    'nhie': 'Never Have I Ever',
    'mlt': 'Most Likely To',
    'ttl': 'Two Truths and a Lie',
    'hottakes': 'Hot Takes',
    'story': 'Story Builder',

    'ama': 'Anonymous AMA',
    'fantasies': 'Fantasies & Dealbreakers',
    'price': 'Name Your Price',
    'rushmore': 'Mt. Rushmore Draft',
    'clapback': 'Clapback',
    'legitlibs': 'LegitLibs',
    'pressure': 'Pressure Cooker',
    'risky_roll': 'Risky Rolls',
}

# ── Scheduling registry ─────────────────────────────────────────────────────
# Party games that can be auto-launched by the scheduler (web dashboard).
# PvP / duel / lobby-challenge games (e.g. 'pressure', Quickdraw, Chicken,
# Hot Potato, Musical Chairs) are intentionally excluded — they need a live
# challenge/opponent flow. Adding a game here REQUIRES registering a launcher
# in its cog setup() (see bot.game_launchers); the startup coverage check warns
# on drift.
# NOTE: 'photo' is intentionally NOT here — Photo Challenge is its own
# standalone dashboard feature (/api/photo-challenge, own channel + schedule),
# not part of the shared games menu/scheduler. Its schedule rows still ride
# the games_scheduled table + this loop (game-type-agnostic), but they're
# created via the dedicated routes and hidden from the shared scheduler UI.
SCHEDULABLE_GAME_TYPES = [
    'ffa', 'ffa_banner', 'traditional', 'compliment', 'mfk', 'wyr', 'nhie', 'mlt', 'ttl',
    'hottakes', 'story', 'ama', 'fantasies', 'price', 'rushmore', 'clapback',
    'legitlibs', 'risky_roll',
]

# Some schedulable types are display variants of a base game — they share the
# question bank, history rows, and the base game's enable/disable toggle. Map a
# variant to its base so a single games-config toggle governs both. Used by the
# scheduler's enable check (see scheduled_games_service).
SCHEDULE_BASE_GAME_TYPE = {
    'ffa_banner': 'ffa',
}

# Per-game option fields the scheduler/web UI can collect. Each field:
#   name    — key in the options dict passed to launch()
#   label   — UI label
#   type    — 'str' | 'int' | 'bool' | 'choice'
#   default — default value when omitted
#   (int)   — optional 'min'/'max'
#   (choice)— 'choices': list of {'value', 'label'}
# Games with no setup options have an empty list. Mirrors each cog's slash params.
SCHEDULE_OPTION_SCHEMA = {
    'ffa': [
        {'name': 'kind', 'label': 'Prompt type', 'type': 'choice', 'default': 'random',
         'choices': [{'value': 'random', 'label': 'Random'},
                     {'value': 'truth', 'label': 'Truth'},
                     {'value': 'dare', 'label': 'Dare'}]},
        {'name': 'prompt', 'label': 'Custom prompt (optional)', 'type': 'str', 'default': ''},
    ],
    'ffa_banner': [
        {'name': 'kind', 'label': 'Prompt type', 'type': 'choice', 'default': 'random',
         'choices': [{'value': 'random', 'label': 'Random'},
                     {'value': 'truth', 'label': 'Truth'},
                     {'value': 'dare', 'label': 'Dare'}]},
        {'name': 'prompt', 'label': 'Custom prompt (optional)', 'type': 'str', 'default': ''},
    ],
    'traditional': [
        {'name': 'single_choice', 'label': 'One category per player (radio-style)',
         'type': 'bool', 'default': False},
    ],
    'compliment': [],
    'mfk': [
        {'name': 'options', 'label': 'Custom categories (comma-separated, optional)',
         'type': 'str', 'default': ''},
    ],
    'wyr': [
        {'name': 'question', 'label': "Opening question ('option A | option B', optional)",
         'type': 'str', 'default': ''},
    ],
    'nhie': [
        {'name': 'question', 'label': 'Opening statement (optional)', 'type': 'str', 'default': ''},
        {'name': 'lives', 'label': 'Lives (0 = no elimination)', 'type': 'int',
         'default': 3, 'min': 0, 'max': 10},
    ],
    'mlt': [
        {'name': 'question', 'label': 'Opening prompt (optional)', 'type': 'str', 'default': ''},
    ],
    'ttl': [
        {'name': 'prompt', 'label': 'Theme/prompt (optional)', 'type': 'str', 'default': ''},
        {'name': 'vote_timer', 'label': 'Vote seconds (0 = host advances)', 'type': 'int',
         'default': 0, 'min': 0, 'max': 300},
    ],
    'hottakes': [],
    'story': [
        {'name': 'max_sentences', 'label': 'Max sentences', 'type': 'int',
         'default': 10, 'min': 1, 'max': 30},
        {'name': 'visibility', 'label': 'Visibility', 'type': 'choice', 'default': 'blind',
         'choices': [{'value': 'blind', 'label': 'Blind (prev sentence only)'},
                     {'value': 'full', 'label': 'Full (whole story)'}]},
        {'name': 'starter', 'label': 'Starter sentence (optional)', 'type': 'str', 'default': ''},
    ],
    'ama': [
        {'name': 'mode', 'label': 'Mode', 'type': 'choice', 'default': 'unfiltered',
         'choices': [{'value': 'unfiltered', 'label': 'Unfiltered (post immediately)'},
                     {'value': 'screened', 'label': 'Screened (host approves)'}]},
        {'name': 'format', 'label': 'Format', 'type': 'choice', 'default': 'hot_seat',
         'choices': [{'value': 'hot_seat', 'label': 'Hot Seat (one at a time)'},
                     {'value': 'panel', 'label': 'Open Panel (ask anyone opted in)'}]},
    ],
    'fantasies': [],
    'price': [
        {'name': 'rounds', 'label': 'Rounds', 'type': 'int', 'default': 5, 'min': 1, 'max': 20},
        {'name': 'timer', 'label': 'Submission seconds/round', 'type': 'int', 'default': 30},
        {'name': 'vote_timer', 'label': 'Voting seconds/round', 'type': 'int', 'default': 20},
        {'name': 'source', 'label': 'Scenario source', 'type': 'choice', 'default': 'host',
         'choices': [{'value': 'host', 'label': 'Host writes'},
                     {'value': 'players', 'label': 'Players submit'},
                     {'value': 'ai', 'label': 'AI generated'},
                     {'value': 'bank', 'label': 'Question bank'}]},
    ],
    'rushmore': [
        {'name': 'topic', 'label': 'Topic (optional)', 'type': 'str', 'default': ''},
        {'name': 'timer', 'label': 'Pick seconds', 'type': 'int', 'default': 30},
        {'name': 'vote_timer', 'label': 'Voting seconds', 'type': 'int', 'default': 30},
        {'name': 'source', 'label': 'Topic source', 'type': 'choice', 'default': 'host',
         'choices': [{'value': 'host', 'label': 'Host writes'},
                     {'value': 'ai', 'label': 'AI generated'},
                     {'value': 'bank', 'label': 'Question bank'}]},
        {'name': 'mode', 'label': 'Draft mode', 'type': 'choice', 'default': 'snake',
         'choices': [{'value': 'snake', 'label': 'Snake draft (one at a time)'},
                     {'value': 'blitz', 'label': 'Blitz (everyone picks at once)'}]},
    ],
    'clapback': [
        {'name': 'rounds', 'label': 'Rounds', 'type': 'int', 'default': 5, 'min': 1, 'max': 15},
        {'name': 'timer', 'label': 'Answer seconds', 'type': 'int', 'default': 120, 'min': 15, 'max': 180},
        {'name': 'vote_timer', 'label': 'Vote seconds/matchup', 'type': 'int', 'default': 40, 'min': 10, 'max': 60},
        {'name': 'anonymous', 'label': 'Hide authors until recap', 'type': 'bool', 'default': False},
        {'name': 'tags', 'label': 'Prompt tags (comma-separated, optional)', 'type': 'str', 'default': ''},
    ],
    'legitlibs': [
        {'name': 'mode', 'label': 'Mode', 'type': 'choice', 'default': 'classic',
         'choices': [{'value': 'classic', 'label': 'Classic (sequential fill)'},
                     {'value': 'quiplash', 'label': 'Quiplash (all fill, all revealed)'}]},
        {'name': 'tier', 'label': 'Heat tier (1-4)', 'type': 'int', 'default': 2, 'min': 1, 'max': 4},
    ],
    'risky_roll': [
        {'name': 'auto_close_players', 'label': 'Auto-close after N players roll', 'type': 'int',
         'default': 25, 'min': 2, 'max': 100},
        {'name': 'auto_close_minutes', 'label': 'Auto-close after N minutes', 'type': 'int',
         'default': 120, 'min': 1, 'max': 1440},
    ],
}

HOW_TO_PLAY = {
    'ffa': (
        "🎭 **Truth or Dare**\n"
        "The host drops a Truth or Dare prompt in two flavors:\n\n"
        "• **/games play ffa** — posts an embed with anonymous reply buttons; "
        "replies land back in the channel and a live counter tracks them:\n"
        "   • **🎭 Reply Anonymously** — same anon nickname the whole time\n"
        "   • **🎲 Reply as Someone New** — a fresh nickname each time\n"
        "   Replies are posted with no attribution (mods can still see who sent them).\n"
        "• **/games play ffa_banner** — just drops the prompt card in the channel "
        "for open discussion (no anonymous replies)\n\n"
        "💡 The host picks Truth/Dare/random or writes their own prompt — and can schedule "
        "an automated series from the dashboard. Spicier (NSFW) prompts appear only in "
        "channels marked age-restricted in Discord."
    ),
    'photo': (
        "📸 **Photo Challenge**\n"
        "The host drops a Photo Challenge card in the channel.\n\n"
        "1. Read the challenge on the card\n"
        "2. Post your photo right in the channel\n"
        "3. Browse everyone else's takes as they roll in\n\n"
        "💡 Challenges are curated in the **Games Studio** (web dashboard). The host "
        "can write their own challenge or schedule an automated series. Spicier (NSFW) "
        "challenges appear only in channels marked age-restricted in Discord."
    ),
    'traditional': (
        "🎲 **Truth or Dare**\n"
        "Classic Truth or Dare, but everyone picks what they're up for.\n\n"
        "1. Click the categories you want to opt into: **SFW Truth**, **SFW Dare**, "
        "**NSFW Truth**, **NSFW Dare** — pick as many as you like\n"
        "2. The host clicks **Ask Question**, the bot picks a player from the pool, "
        "and the host writes a custom question for them\n"
        "3. Each player gets one question per category they opted into\n\n"
        "💡 Players who haven't been asked yet are picked first to keep things fair.\n"
        "💡 **Bank Round** deals everyone a question from the web question bank. It "
        "counts toward the one-per-category limit, so pressing it again only serves "
        "players who haven't been asked yet — perfect for late joiners.\n"
        "💡 Host tip: pass **single_choice** when starting the game to make each "
        "player pick just one category (the buttons act like radio buttons)."
    ),
    'compliment': (
        "💛 **Spin the Compliment**\n"
        "Random pairings — everyone gives one person a compliment.\n\n"
        "1. Click **Join** to enter the pool\n"
        "2. The host clicks **Close & Generate** when the pool is ready\n"
        "3. Pairings are revealed publicly — each player sees who they're giving to\n"
        "4. Reply in the channel with a compliment for your assigned partner\n\n"
        "💡 Need at least 2 players to generate pairings."
    ),
    'mfk': (
        "💍 **Marry, Fornicate, Kiss**\n"
        "Join the pool, get assigned three names, slot them into the categories.\n\n"
        "1. Click **Join** to enter the pool\n"
        "2. The host clicks **Close & Assign** when ready (need 4+ players)\n"
        "3. Each player is given 3 random names from the pool — never themselves\n"
        "4. Reply in the channel saying who you'd Marry, Fornicate, and Kiss\n\n"
        "💡 **Custom categories:** the host can pass `options:` to use any 3 categories — "
        "e.g. `Cruise, Wedding, Vacation`."
    ),
    'wyr': (
        "🤔 **Would You Rather**\n"
        "Two options per round — pick your side.\n\n"
        "1. Vote **🅰️** or **🅱️** — you can switch before the round ends\n"
        "2. The host clicks **⏭️ Next** to advance to the next round\n"
        "3. Use **✍️ Pose Question** to queue your own (format: `option A | option B`)\n"
        "4. The host can **👀 Reveal Voters** to show who voted for what\n\n"
        "💡 Questions come from the bank by default — your queued questions get used first."
    ),
    'nhie': (
        "⛔ **Never Have I Ever**\n"
        "A statement is read each round — confess or claim innocence.\n\n"
        "1. Vote **😈 Guilty** if you've done it, **😇 Innocent** if you haven't\n"
        "2. Use **✍️ Pose Statement** to queue your own statement\n"
        "3. The host clicks **⏭️ Next** to advance\n\n"
        "❤️ **Lives mode (default 3):** every guilty vote costs you a heart. "
        "Last one standing wins. Set `lives:0` to disable elimination."
    ),
    'mlt': (
        "👑 **Most Likely To**\n"
        "Vote on which player fits each prompt best.\n\n"
        "1. Click **Join** to enter the pool (need 3+ players)\n"
        "2. Each round shows a prompt — vote for the player who fits it best\n"
        "3. The most-voted player gets the crown for that round\n"
        "4. Use **✍️ Pose Prompt** to queue your own\n\n"
        "💡 You can vote for anyone in the pool, including yourself."
    ),
    'ttl': (
        "🤥 **Two Truths and a Lie**\n"
        "Submit three statements — two true, one a lie. The room guesses which.\n\n"
        "1. Click **Submit Statements** and fill in your three statements + which is the lie\n"
        "2. The host clicks **Start Guessing** when everyone's submitted (need 2+ players)\n"
        "3. For each player, the room votes which statement they think is the lie\n"
        "4. Voters who get it right earn points; players who fool the room earn points too\n\n"
        "💡 Statements get shuffled before display so position doesn't give it away."
    ),
    'hottakes': (
        "🔥 **Hot Takes**\n"
        "Submit your spiciest opinion anonymously, then rate the room's takes.\n\n"
        "1. Click **Submit Hot Take** — your name is never attached\n"
        "2. The host clicks **Start Voting** when submissions are in\n"
        "3. Each take is shown one at a time in random order\n"
        "4. Vote your temperature: 🧊 Strongly Disagree → 👎 → 😐 → 👍 → 🔥 Strongly Agree\n"
        "5. The average temperature for each take is revealed at the end\n\n"
        "💡 Submissions stay anonymous through the whole game."
    ),
    'story': (
        "📖 **Story Builder**\n"
        "Take turns writing one sentence to build a collaborative story.\n\n"
        "1. Click **Join** before the host starts the story\n"
        "2. On your turn, click **✍️ Write Your Sentence** and add to the story\n"
        "3. The story ends after the chosen sentence count (default 10, max 30)\n\n"
        "👁️ **Visibility modes:**\n"
        "• **Blind** — you only see the previous sentence (chaotic, default)\n"
        "• **Full** — you see the entire story so far\n\n"
        "💡 The host can skip a player whose turn is taking too long."
    ),

    'ama': (
        "🎙️ **Anonymous AMA**\n"
        "Players answer anonymous questions from the room.\n\n"
        "1. Players **Volunteer** to take questions\n"
        "2. Anyone clicks **Ask a Question** to send one via popup\n"
        "3. The person asked replies — replies are signed, questions are not\n\n"
        "🎭 **Formats:**\n"
        "• **Hot Seat** — one player at a time; the seat rotates after a few "
        "questions or when handed off (default)\n"
        "• **Open Panel** — everyone who volunteers is listed at once; pick who "
        "to ask from a dropdown\n\n"
        "🛡️ **Modes:**\n"
        "• **Unfiltered** — questions post immediately (default)\n"
        "• **Screened** — the host approves each question before it's shown\n\n"
        "💡 The bot DMs you when your anonymous question gets a reply."
    ),
    'fantasies': (
        "✨ **Fantasies & Dealbreakers**\n"
        "Anonymously share what you'd love or hate, then vote on each entry.\n\n"
        "1. The host clicks **Start Round** to open submissions\n"
        "2. Click **Submit** and pick **Fantasy** (something you'd love) or "
        "**Dealbreaker** (something you'd never tolerate)\n"
        "3. The host closes submissions when ready\n"
        "4. Each entry is revealed one at a time — vote **Same** or **Not for me**\n"
        "5. The host can run additional rounds before ending the game\n\n"
        "💡 All submissions are anonymous — only the votes are public."
    ),
    'price': (
        "💰 **Name Your Price**\n"
        "A scenario is posed — something absurd, personal, or uncomfortable. "
        "Everyone secretly submits how much money it would take for them to do it. "
        "All prices are revealed sorted lowest to highest. "
        "After the reveal, the room votes on Most Reasonable and Most Unhinged.\n\n"
        "Submit your price via the modal. No cap — $1 to $999,999,999."
    ),
    'rushmore': (
        "🗿 **Mt. Rushmore Draft**\n"
        "A topic is chosen. Players take turns drafting their top 4 picks for that topic — "
        "snake draft style (1st picker in round 1 goes last in round 2).\n\n"
        "**No duplicates** — if someone picks it before you, it's gone.\n\n"
        "After 4 rounds, everyone's Mt. Rushmore is displayed and the room votes on the best one."
    ),
    'clapback': (
        "⚔️ **Clapback — How to Play**\n\n"
        "1. A funny prompt is shown to everyone\n"
        "2. Everyone writes their funniest answer (via popup)\n"
        "3. Answers are paired up head-to-head for voting\n"
        "4. The room votes on which answer is funnier\n"
        "5. Points = your vote percentage (75% of votes = 75 pts)\n"
        "6. Get ALL the votes? That's a **CLAPBACK**! (+25 bonus pts!)\n\n"
        "💡 **Tips:**\n"
        "• Funny beats accurate\n"
        "• Short and punchy usually wins\n"
        "• You can resubmit before time runs out\n"
        "• You can't vote on your own matchup"
    ),
    'legitlibs': (
        "📝 **LegitLibs — How to Play**\n\n"
        "Everyone fills in the blanks to complete a story — the results are always unhinged.\n\n"
        "**Quiplash mode (default):**\n"
        "1. The host starts a round and everyone joins\n"
        "2. Click **Submit Fills** to open the form — fill in each blank\n"
        "3. The timer runs out → every version of the story is revealed one by one\n"
        "4. At the end, the full cast is shown so you know who wrote what\n\n"
        "**Heat tiers:** 🌶️ Flirty · 🌶️🌶️ Spicy · 🌶️🌶️🌶️ Filthy · 💀 Unhinged\n\n"
        "💡 You can resubmit to overwrite your fills before the timer runs out."
    ),
    'pressure': (
        "♨️ **Pressure Cooker**\n"
        "A high-stakes nickname duel — pump the gauge and hope it doesn't blow.\n\n"
        "1. Use `/pressure challenge @user` to issue a challenge (optional: add custom stakes text)\n"
        "2. The target has 60 seconds to **Accept** or **Decline**\n"
        "3. Players take turns clicking **Pump** — each pump adds a random amount to the gauge\n"
        "4. First player to push the gauge past 100 **BUSTS** and loses\n"
        "5. The winner sets a nickname for the loser (default: 24 hours)\n\n"
        "⚙️ **Other commands:**\n"
        "• `/pressure cancel` — cancel your pending challenge\n"
        "• `/pressure stats` — view your win/loss record\n"
        "• `/pressure revert` — request early nickname restoration (if enabled by mods)\n"
        "• `/pressure config` — configure cooldowns, sentence length, etc. (mods only)"
    ),
}
