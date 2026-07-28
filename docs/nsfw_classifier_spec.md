# NSFW Classifier — Feature Spec

A shared service that answers one question — *is this uploaded image explicit?* — for every feature that needs it. Runs the bundled NudeNet 320n ONNX model over image attachments and returns a verdict plus the raw detections behind it.

It is deliberately not a feature in its own right: it has no commands, no listener, and no user-visible surface. Three consumers call it — reaction tipping, spoiler enforcement, and SFW nudity prevention — and because they all fire off the same `on_message`, the verdict is computed **once per attachment** and shared.

Implemented in `src/bot_modules/services/nsfw_classifier_service.py`; inference lives behind `guess_nudenet.detect_bytes()`, shared with the Guess pipeline so the model loads once per process.

## Verdict

Three-valued, and the third value is the point:

| verdict | meaning |
|---|---|
| `True` | a qualifying label scored at or above the threshold |
| `False` | the image was read and classified, and nothing qualified |
| `UNKNOWN` (`None`) | the image could not be read or classified at all |

`UNKNOWN` is never collapsed into `False`. The consumers have opposite failure tolerances, so the service refuses to pick for them:

| consumer | where it runs | threshold | on `UNKNOWN` |
|---|---|---|---|
| Reaction tipping | `is_nsfw()` channels with a tipping rule | standard | react anyway — a CDN hiccup must not cost a poster their tips |
| Spoiler enforcement | `spoiler_required_channels` | standard | delete — preserves the pre-classifier behavior; unreadable is treated as maybe-explicit |
| SFW nudity prevention | every other channel | **higher** | do nothing — never delete on a failed read |

The higher threshold for SFW prevention is deliberate. A false positive there deletes a member's innocent photo, so it demands more certainty than merely qualifying a post for coins.

## Qualifying labels

Exposed nudity only, by default:

`MALE_GENITALIA_EXPOSED`, `FEMALE_GENITALIA_EXPOSED`, `ANUS_EXPOSED`, `FEMALE_BREAST_EXPOSED`, `BUTTOCKS_EXPOSED`, `SEX_ACT`

The paired `*_COVERED` labels (lingerie, swimwear, implied nudity) do **not** qualify, nor do `BELLY_*`, `ARMPITS_*` or face labels. `SEX_ACT` is not a NudeNet label — it is synthesised by `guess_pipeline.merge_sex_act_detections()` when two different genital labels overlap, and the classifier applies that same merge so its verdicts agree with the Guess pipeline's.

The reported `top_label` is the highest-scoring *qualifying* detection, so a confident `BELLY_EXPOSED` is never presented as the reason an image was judged explicit.

## Scope

**Attachments only.** Embeds are never classified. The auto-react cog's `_has_image` matches `gifv`/`rich` embeds whose images live on arbitrary external hosts; fetching those would point the bot's outbound requests at member-supplied URLs — SSRF probing of the local network, IP-logging pixels, hostile payloads — so they are out of scope entirely. In a tipping-enabled channel this means embeds get no emoji at all, since a bot-placed emoji is a live tip and nothing may be tipped that wasn't classified.

Attachments over 25 MB are not downloaded. Downloads time out at 10 s. Both failures land as `UNKNOWN`.

Inference runs through `asyncio.to_thread` — onnxruntime blocks in C++, and calling it inline would stall the bot's heartbeat for the duration of every classification.

## Caching

Verdicts are cached in-process by attachment id (bounded LRU, 512 entries) so the consumers that fire on one message classify between them exactly once. The cache is keyed on identity alone, so a caller needing a *different* threshold gets a fresh classification rather than the cached one — this is how SFW prevention runs its stricter bar over an image another consumer already judged.

`UNKNOWN` results are never cached; a transient CDN failure must not pin "unreadable" for the life of the process.

## Recording

**Coverage and recording deliberately differ.** Classification runs on attachments in *every* channel, because SFW prevention needs a verdict everywhere. Rows are written **only for uploads in Discord-age-gated (`is_nsfw`) channels**, so no dataset is built out of general chat.

`nsfw_classifications` — one row per `(message_id, attachment_id)`: verdict, top label and score, model name, **the threshold and label set that were applied**, inference milliseconds, and source byte size. Storing the threshold and label set per row rather than reading config at query time is what keeps the data interpretable after a retune, and is what makes "what would 0.4 have changed?" answerable in hindsight.

`nsfw_detections` — every detection the model returned, *including non-qualifying ones*. A threshold sweep is only possible if the near-misses were kept.

`UNKNOWN` verdicts are not recorded. A row claiming `verdict=0` for an image nobody could read would poison the accuracy metrics the table exists to provide.

Retention is indefinite (a deliberate choice — see `docs/plans/nsfw-classifier-and-reaction-tips.md`).

### Privacy

`nsfw_detections` is the most sensitive table this bot holds: effectively a labelled body-part inventory of members' uploads. It is derived metadata rather than message content, which fits the project's "derive at ingest, store minimal" rule, but two minimisations apply regardless:

- **No `author_id` column.** Authorship joins through `messages` rather than being duplicated here.
- **Admin-gated on the dashboard.** These rows are never surfaced to non-admins in any view.

## Configuration

Dashboard only; no in-Discord configuration. Stored in the shared `config` table, per guild:

| key | default | what it does |
|---|---|---|
| `nsfw_classifier_threshold` | `0.5` | confidence a qualifying label must reach |
| `nsfw_classifier_sfw_threshold` | `0.75` | stricter bar used by SFW nudity prevention |
| `nsfw_classifier_labels` | (built-in set) | comma-separated qualifying labels |

Both thresholds are validated on read: a value outside `(0, 1]` is rejected in favor of the default, because such a value answers the same way for every image and would silently disable the gate rather than loosen it. An empty label set falls back to the default for the same reason.

## Cost

Measured on the production host (Intel N150, 4 cores) with the bundled 320n model:

| | |
|---|---|
| cold start (import + model load) | ~470 ms, once per process |
| warm inference, 1536×1024 | 74 ms median (65–81) |
| resident memory | +132 MB, process lifetime |
| image posts/day, server-wide | 200–500 (~290 avg over 14 days) |
| CPU/day | ~21 s |
| busiest single minute observed | 31 images → 2.3 s, ~4% of one core |

Compute is not the constraint. The costs that matter are CDN bandwidth (previously the bot never fetched image bytes at all) and the ~1–2 s between an image appearing and a consumer being able to act on it.

## Tests

`tests/test_nsfw_classifier_service.py` — label membership per qualifying and non-qualifying label, threshold boundaries (inclusive at the threshold), stricter-threshold and custom-label-set overrides, config parsing and out-of-range rejection, attachment type/size gating, `UNKNOWN` for download failure / timeout / inference failure / unclassifiable type, cache reuse across consumers, cache bypass on a differing threshold, `UNKNOWN` never cached, recording writes both tables with near-misses kept, `UNKNOWN` never recorded, recording idempotent per attachment, and no `author_id` column.
