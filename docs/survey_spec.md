# Anonymous Survey — Feature Spec

Admins build a survey (an ordered list of questions), then post a **launcher button** in any channel. A member clicks it and the bot DMs them the questions one at a time. Answers are collected in the DM and stored **without a reversible link to the member's identity** — only a per-survey salted hash of their user ID is kept, so the panel can report a unique-respondent count and (optionally) cap re-takes without ever revealing who answered. A web dashboard panel lists every survey and lets an admin review its responses in aggregate and one-by-one.

The goal is honest feedback: because responses land in a DM and are stored de-identified, members can say what they actually think.

## At a glance

| Piece | What it is |
|---|---|
| Survey | An admin-authored, ordered set of questions with a title and lifecycle state (`draft` / `open` / `closed`) |
| Launcher | A persistent button posted in a channel; one survey can have any number of launchers in different channels |
| DM session | A per-user, in-DM walk through the survey's questions, one question at a time |
| Response | One completed (or partial) DM walkthrough, stored de-identified |
| Panel | A read-only dashboard view of surveys and their responses |

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/survey post <survey>` | Slash | Manage Guild | Post a launcher button for the chosen survey in the current channel |
| `/survey list` | Slash | Manage Guild | Ephemeral list of surveys with state, response count, and launcher locations |
| `/survey open <survey>` | Slash | Manage Guild | Move a `draft`/`closed` survey to `open` (accepting responses) |
| `/survey close <survey>` | Slash | Manage Guild | Stop accepting responses; existing launchers become inert |
| `/survey cancel` | Slash | Everyone (DM only) | Abandon the in-progress DM survey without saving |
| Survey builder | Web (dashboard) | Admin | Create/edit/delete surveys and their questions; set anonymity/cap options |
| Survey responses | Web (dashboard) | Admin | Review past surveys and their responses |

Survey **authoring** (questions, options, ordering) lives entirely in the web dashboard — there is no in-Discord question editor. The slash commands only handle posting, lifecycle, and the respondent-side cancel. This mirrors how other content-heavy features (config panels, question banks) keep authoring on the web where a real form beats slash-command ergonomics.

## Question types

A survey is an ordered list of questions. Each question has a `kind`:

| Kind | Respondent sees | Stored answer |
|---|---|---|
| `text` | A prompt; the bot captures their next DM message | The message text (trimmed, length-capped) |
| `choice` | The prompt plus one button per option (single-select) | The chosen option's stable key + label |
| `scale` | The prompt plus a row of numbered buttons (configurable min/max, e.g. 1–5 or 1–10) | The integer value |
| `multi` | The prompt plus a select menu (checkbox-style); respondent picks any number, then confirms | The list of chosen option keys + labels |

Common per-question attributes: `prompt` (required), `required` (default true), `kind`, and — for `choice`/`multi`/`scale` — the option set or scale bounds. Questions carry a stable `id` and a `position` so answers survive later reordering/editing of the survey. Editing a question's option set after responses exist is allowed; stored answers keep the option **label** captured at submit time, so historical responses stay readable even if options later change.

### Component limits

Discord caps a single action row at 5 buttons and a message at 5 rows (25 buttons), and a select menu at 25 options. The builder enforces:

- `choice`: 2–25 options (rendered as up to 5 rows of buttons; ≥6 options auto-render as a select menu instead).
- `scale`: min < max, and `max - min + 1 ≤ 25`. Recommended 1–5 or 1–10.
- `multi`: 2–25 options, optional `min_select` / `max_select` bounds.

## Respondent flow (DMs)

1. **Click.** A member clicks a launcher button. The bot immediately responds **ephemerally in the channel** ("Check your DMs — I've sent you the survey.") and opens a DM.
2. **Intro.** The DM opens with the survey title, an anonymity notice, and a "Start" button:
   > **{survey title}**
   > Your answers are anonymous — they're stored without any link to your Discord account, and admins only see aggregate results.
   > This survey has **{n} questions**. Reply here or use the buttons. Type `/survey cancel` any time to stop.
   > `[ Start ]`
3. **Questions.** After Start, the bot posts questions **one at a time**. For `text`, the bot waits for the member's next DM message. For `choice`/`scale`/`multi`, the bot posts the components and waits for the interaction. Each answered question is acknowledged briefly ("✓ Got it — next:") before the next prompt.
4. **Completion.** After the last question the bot posts a thank-you and writes the response. A completed response is `state = complete`.
5. **Cancel / timeout.** `/survey cancel` (or a per-question inactivity timeout, default 30 min) ends the session. Partial progress is saved as `state = partial` only if the survey opts into partial saving (default: discard partials). The member sees "Survey cancelled — nothing was saved" or, for partial-save surveys, "Stopped — your answers so far were saved."

### One session at a time

A member can have only one active DM session per survey. Clicking the launcher again while a session is live re-sends the current question rather than starting over. Clicking a **different** survey's launcher starts a separate session (sessions are keyed by survey).

### DMs closed

If the bot can't DM the member (privacy settings), the ephemeral channel reply becomes: "I couldn't DM you — enable **Direct Messages** from server members and click again." No session is created.

### Free-text capture

Because `text` answers are captured from arbitrary DM messages, the session is a small state machine keyed by `(user_id, survey_id)` held in memory and mirrored to the DB (see **Crash recovery**). Messages that arrive when the bot expects a button click (or vice-versa) get a gentle nudge ("Tap one of the buttons above to answer this one.") and don't advance the survey.

### DM listener precedence

Several other cogs already read DMs via `on_message` (e.g. `dm_perms_cog`, `whisper_cog`, `bios_cog`, `confessions_cog`). When a survey session is active for a member, **the survey listener claims that member's next DM message and other DM consumers must not also act on it.** Implement this as an early check in the survey `on_message` handler (active session for this `user_id`? → consume, mark handled, `return`) and, where those other cogs could double-handle a survey-in-progress DM, guard them behind "no active survey session for this user." The bios DM wizard is the closest precedent for how an in-progress DM flow claims messages; follow its guarding approach so two DM flows can't both grab the same message.

## Anonymity model

The chosen model is **hashed identity, unlimited submissions by default**:

- On submit, the bot computes `respondent_hash = HMAC(survey_salt, user_id)` where `survey_salt` is a random value generated per survey at creation and **never exposed** through any API or panel. Different surveys → different salts → the same member's hashes are uncorrelated across surveys.
- `respondent_hash` is the **only** identity-derived value stored. No `user_id`, username, or avatar is written to any survey table.
- The panel uses `respondent_hash` solely to report **unique respondents** vs **total submissions**. It is never displayed and there is no lookup from hash back to a member.
- **Submissions are unlimited by default.** A survey may optionally set `max_per_respondent` (default `0` = unlimited). When set to 1, a member who already submitted is told "You've already completed this survey — thanks!" and no new session opens. The cap is enforced via `respondent_hash`, preserving anonymity.

This gives privacy-preserving dedup *if the admin wants it* without ever storing a reversible identity link. It does not defend against a determined admin with DB access brute-forcing hashes over the (small) member list — that's an accepted limitation documented in **Non-goals**. Admins who need a stronger guarantee should use unlimited mode, where the hash carries no dedup meaning.

## Launcher panels

Modeled on `ticket_panels` (multiple panels per guild, one row each). A launcher is a message the bot posts carrying a persistent button whose `custom_id` encodes the survey id (e.g. `survey|<survey_id>`). Persistent views are re-registered on boot so buttons survive restarts, following the existing `add_view` registration in `__main__.py`.

- `/survey post <survey>` posts the launcher embed + button in the current channel and records a `survey_launchers` row.
- Multiple launchers per survey are allowed (e.g. one in `#general`, one in `#feedback`).
- Closing a survey leaves the launcher message in place but makes the button inert: clicking a closed survey's button returns an ephemeral "This survey is closed." A background reconcile isn't required — state is checked at click time.
- If the launcher message is deleted, the row is cleaned up lazily on the next failed edit/lookup.

**Launcher embed**
```
Title:  📋 {survey title}
Body:   {optional one-line description}
        {n} questions · anonymous · ~{est} min
Button: [ Take the survey ]  (custom_id = survey|<survey_id>)
```

## Web dashboard

Two panels under a new **Surveys** dashboard section, both admin-only, following the existing FastAPI `APIRouter` + `require_perms({"admin"})` + `get_active_guild_id` pattern and registered in `web_server/server.py` under `prefix="/api/surveys"`.

### Builder panel

- List surveys for the active guild with state, question count, response count, and launcher locations.
- Create a survey (title, optional description, anonymity/cap options, partial-save toggle).
- Add/edit/remove/reorder questions; per question set kind, prompt, required flag, and options/scale bounds with the component-limit validation above.
- Lifecycle buttons (open/close/delete). Deleting a survey with responses requires a typed confirmation and cascades to its questions, launchers, and responses.

### Responses panel (the review surface)

Read-only. For a selected survey:

- **Header:** title, state, date opened, total submissions, unique respondents, completion rate (complete ÷ started).
- **Per-question aggregates:**
  - `choice` / `scale`: bar chart of counts per option/value plus mean/median for `scale`. Uses the existing `charts.js` helpers.
  - `multi`: bar chart of per-option selection counts (counts sum to > respondents).
  - `text`: a scrollable, paginated list of all free-text answers, plus a copy-all button. No sentiment/AI processing in v1.
- **Per-response view:** a paginated walkthrough of individual completed responses — each shows the full set of answers for one submission, labeled only "Response #k" and a submit timestamp. **No identity, ever.** Ordering is by submit time; `k` is a display index, not linkable to a member.
- **Export:** download responses as CSV/JSON (answers + timestamps only; `respondent_hash` is **not** included in exports).

### API sketch

| Method + path | Purpose |
|---|---|
| `GET /api/surveys` | List surveys for the active guild |
| `POST /api/surveys` | Create a survey |
| `GET /api/surveys/{id}` | Survey with its questions |
| `PUT /api/surveys/{id}` | Update title/description/options/state |
| `POST /api/surveys/{id}/questions` | Add a question |
| `PUT /api/surveys/{id}/questions/{qid}` | Edit/reorder a question |
| `DELETE /api/surveys/{id}/questions/{qid}` | Remove a question |
| `GET /api/surveys/{id}/responses` | Aggregates + paginated per-response data (never identity) |
| `GET /api/surveys/{id}/export?fmt=csv\|json` | Download responses |
| `DELETE /api/surveys/{id}` | Delete survey + cascade |

## Stored data (migration `059_surveys.sql`)

```sql
CREATE TABLE IF NOT EXISTS surveys (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    title              TEXT    NOT NULL,
    description        TEXT    NOT NULL DEFAULT '',
    state              TEXT    NOT NULL DEFAULT 'draft',   -- draft | open | closed
    salt               BLOB    NOT NULL,                   -- per-survey HMAC salt; never exposed
    max_per_respondent INTEGER NOT NULL DEFAULT 0,         -- 0 = unlimited
    save_partial       INTEGER NOT NULL DEFAULT 0,
    inactivity_secs    INTEGER NOT NULL DEFAULT 1800,
    created_at         REAL    NOT NULL DEFAULT (unixepoch()),
    closed_at          REAL
);

CREATE TABLE IF NOT EXISTS survey_questions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id    INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    kind         TEXT    NOT NULL,                          -- text | choice | scale | multi
    prompt       TEXT    NOT NULL,
    required     INTEGER NOT NULL DEFAULT 1,
    options_json TEXT    NOT NULL DEFAULT '[]',             -- [{key,label}] for choice/multi
    scale_min    INTEGER,                                   -- scale only
    scale_max    INTEGER,                                   -- scale only
    min_select   INTEGER,                                   -- multi only
    max_select   INTEGER                                    -- multi only
);
CREATE INDEX IF NOT EXISTS idx_survey_questions_survey
    ON survey_questions (survey_id, position);

CREATE TABLE IF NOT EXISTS survey_launchers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id  INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at REAL    NOT NULL DEFAULT (unixepoch()),
    UNIQUE (survey_id, channel_id)
);

CREATE TABLE IF NOT EXISTS survey_responses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id       INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    respondent_hash BLOB    NOT NULL,                       -- HMAC(salt, user_id); de-identified
    state           TEXT    NOT NULL DEFAULT 'complete',    -- complete | partial
    started_at      REAL    NOT NULL DEFAULT (unixepoch()),
    submitted_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_survey_responses_survey
    ON survey_responses (survey_id, submitted_at);
CREATE INDEX IF NOT EXISTS idx_survey_responses_dedup
    ON survey_responses (survey_id, respondent_hash);

CREATE TABLE IF NOT EXISTS survey_answers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id INTEGER NOT NULL REFERENCES survey_responses(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES survey_questions(id) ON DELETE CASCADE,
    value_text  TEXT,                                       -- text: message; choice: chosen label
    value_num   INTEGER,                                    -- scale: the integer value
    value_json  TEXT                                        -- multi ONLY: [{key,label}] (sole home for multi)
);
CREATE INDEX IF NOT EXISTS idx_survey_answers_response
    ON survey_answers (response_id);
CREATE INDEX IF NOT EXISTS idx_survey_answers_question
    ON survey_answers (question_id);

-- In-flight DM sessions, mirrored from memory so a restart can resume.
CREATE TABLE IF NOT EXISTS survey_sessions (
    survey_id    INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL,                          -- live routing key only; NOT copied into responses
    dm_channel_id INTEGER NOT NULL,
    response_id  INTEGER NOT NULL REFERENCES survey_responses(id) ON DELETE CASCADE,
    cur_position INTEGER NOT NULL DEFAULT 0,
    last_active  REAL    NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (survey_id, user_id)
);
```

**Note on `survey_sessions.user_id`:** this is the one place a raw `user_id` appears, and only for routing a *live* DM conversation. On completion the session row is deleted; the finished `survey_responses` row carries only the hash. So the identity link exists only for the duration of an in-progress survey, never for stored results.

## Crash recovery

Active DM sessions are mirrored to `survey_sessions`. Persistent **launcher** views are re-registered from `survey_launchers` on boot, matching the games crash-recovery and booster-role button registration already in `__main__.py`.

**Per-question components are not persistent** — the buttons/selects on a `choice`/`scale`/`multi` question posted before a restart would be dead after it (their view isn't registered). To avoid a dead-button "interaction failed", the uniform resume rule is: **on boot, for each active session, re-send the current question fresh** (a short "Picking up where we left off:" line, then the question at `cur_position` with new components). This makes `text` and component questions resume identically and means per-question custom_ids never need to survive a restart. Sessions past their inactivity window are swept instead of resumed, and their partial responses discarded (or kept, per `save_partial`).

## User-visible errors

| When | The user sees |
|---|---|
| Clicks a closed survey's launcher | "This survey is closed." (ephemeral) |
| Bot can't DM the member | "I couldn't DM you — enable **Direct Messages** from server members and click again." |
| Already submitted, cap reached | "You've already completed this survey — thanks!" |
| Sends text when a button is expected | "Tap one of the buttons above to answer this one." |
| `/survey cancel` with no active session | "You don't have a survey in progress." |
| `/survey post` for a `draft` survey | "Open the survey first with `/survey open`." |
| Session times out | "Survey timed out — {nothing was saved / your answers so far were saved}." |

## Non-goals

- **Not cryptographically unlinkable.** Hashing de-identifies stored results and blocks casual correlation, but an admin with raw DB access could brute-force `respondent_hash` against the member list. For a stronger guarantee, run the survey in unlimited mode (the hash then carries no dedup meaning and reveals nothing an attacker doesn't already have). True zero-knowledge anonymity is out of scope.
- **No branching/skip logic.** Questions are a flat ordered list in v1 — no "if you answered X, skip to Y."
- **No editing submitted answers.** Once a response is complete it's immutable; a member who wants to change an answer re-takes the survey (if the cap allows).
- **No scheduled/auto-posted surveys.** Launchers are posted manually. A scheduled-open feature can build on `scheduled_games` later.
- **No AI analysis of free text in v1.** The panel shows raw text answers; sentiment/summarization is a later add-on.
- **No cross-guild surveys.** Every survey belongs to one guild.

## Files (new)

Following the existing package-per-feature layout (cf. `pen_pals`, `confessions`):

- `src/bot_modules/cogs/survey_cog.py` — slash commands, launcher persistent view, DM session driver, background inactivity sweep.
- `src/bot_modules/survey/` — `db.py` (queries + hashing), `logic.py` (state machine, validation, aggregation helpers), `embeds.py` (launcher + DM embeds), `views.py` (launcher button, per-question components).
- `src/web_server/routes/surveys.py` — the dashboard API, registered in `server.py`.
- `src/web_server/static/js/panels/surveys-builder.js` and `surveys-responses.js` — dashboard panels.
- `src/migrations/059_surveys.sql` — schema above.
- Persistent-view registration wired into `src/dungeonkeeper/__main__.py`.

---

## Elevator pitch

Every server owner eventually wants to ask the members something real — "what should we change?", "how's the vibe?", "rate the last event" — and every public poll gets skewed the moment people see each other's votes. This gives you a proper survey: build the questions in the dashboard, drop a **Take the survey** button in a channel, and the bot quietly walks each member through the questions in their DMs. Answers come back de-identified — you get the aggregate charts and every raw comment, but never a name attached to any of it — so people tell you what they actually think. Review it all later in one panel, export it, close it, run the next one.
