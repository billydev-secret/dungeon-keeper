# NSFW Classifier — Feature Spec

A shared service that answers one question — *is this uploaded image explicit?* — for every feature that needs it. Runs the Marqo whole-image classifier over image attachments and returns a verdict plus the probability behind it.

It is deliberately not a feature in its own right: it has no commands, no listener, and no user-visible surface. Three consumers call it — reaction tipping, spoiler enforcement, and SFW nudity prevention — and because they all fire off the same `on_message`, the verdict is computed **once per attachment** and shared.

Implemented in `src/bot_modules/services/nsfw_classifier_service.py`; inference lives behind `marqo_nsfw.score_bytes()`.

## Two models, two jobs

| | model | runs in | produces | acted on? |
|---|---|---|---|---|
| Verdict | `Marqo/nsfw-image-detection-384` (ONNX) | every channel | one probability | **yes** — this is the verdict |
| Tags | NudeNet (`guess_nudenet`) | age-gated channels only | labels + boxes | **no** — metrics only |

Marqo replaced NudeNet as the verdict engine because NudeNet could not see the content the gates exist to catch. A dark, warm-monochrome boudoir photo passed an *enforcing* SFW gate: NudeNet 320n returned zero detections (even cropped and brightened), 640m returned only a 0.26 `MALE_BREAST_EXPOSED`, and a Falconsai ViT called it `normal` at 0.9997. Marqo scores that image **0.91**, against **0.04–0.08** for non-explicit control images. That lighting is simply outside NudeNet's training data.

NudeNet was kept as a **tagger** because Marqo has no localization: it answers "is it?" and nothing about "where?". The Guess game still calls `guess_nudenet.detect` directly for the bounding boxes it blurs and crops with — that pipeline is untouched by this split.

Tagging runs on the *recording* path rather than beside the verdict, and the same `channel_is_nsfw` flag drives both. That is what makes it structurally impossible to derive a body-part inventory of an upload in general chat. It is also cheaper than the arrangement it replaced, where NudeNet ran on every image in every channel and its labels were discarded outside age-gated ones.

## Verdict

Three-valued, and the third value is the point:

| verdict | meaning |
|---|---|
| `True` | the image scored at or above the threshold |
| `False` | the image was read and classified, and scored below it |
| `UNKNOWN` (`None`) | the image could not be read or classified at all |

`UNKNOWN` is never collapsed into `False`. The consumers have opposite failure tolerances, so the service refuses to pick for them:

| consumer | where it runs | threshold | on `UNKNOWN` |
|---|---|---|---|
| Reaction tipping | `is_nsfw()` channels with a tipping rule | standard | react anyway — a CDN hiccup must not cost a poster their tips |
| Spoiler enforcement | `spoiler_required_channels` | standard | delete — preserves the pre-classifier behavior; unreadable is treated as maybe-explicit |
| SFW nudity prevention | every other channel | **higher** | do nothing — never delete on a failed read |

The higher threshold for SFW prevention is deliberate. A false positive there deletes a member's innocent photo, so it demands more certainty than merely qualifying a post for coins.

A missing model file lands as `UNKNOWN` too, so a deploy without the weights degrades to "we could not tell" rather than to a wrong verdict. That is safe per image but not per deploy: the weights live in gitignored `models/` and are placed by hand, so unlike the pip-bundled detector they replaced they can go missing — and since spoiler enforcement deletes on `UNKNOWN` by design, the gate would silently revert to removing *every* unspoilered image. `on_ready` therefore checks `marqo_nsfw.is_available()` and logs the absence at ERROR once at boot, rather than leaving it to a per-image warning nobody reads.

## Preprocessing

The transform matches timm's eval transform for this model (`crop_pct=1.0`): RGB, centre crop to the source's largest square, resized to 384×384 bicubic, normalized `mean=0.5 std=0.5`, fed as float32 NCHW. Softmax over the two output logits; `label_names` is `['NSFW', 'SFW']`, so index **0** is the probability used.

Cropping **before** resizing is deliberate and is a denial-of-service fix, not a style choice. timm expresses the transform as `Resize(shortest edge → 384)` then `CenterCrop(384)`, which in source coordinates selects exactly that square, so the two orders agree (measured: within 0.0012). But resizing first builds an intermediate of `(long / short) × 384 × 384` pixels, and the only upstream guard is `MAX_IMAGE_BYTES`, which bounds *encoded bytes* and says nothing about dimensions. A **114-byte** 4000×2 PNG passes the 25 MB cap and expands to 295 Mpx — roughly 0.9 GB and 2.5 s; 16000×1 would be ~7 GB. A member posting a handful of those could take the process out. Cropping first makes every resize output exactly 384×384.

Matching timm exactly is worth the few extra lines: squashing the image to a square instead scores the reference image 0.879 where the real transform scores 0.912 — a silent accuracy regression nothing else would notice.

The cost is a real blind spot: a centre crop means content at the far edge of a very wide or very tall image is never seen. That is accepted as the price of matching the training distribution.

## Tags

Exposed nudity only:

`MALE_GENITALIA_EXPOSED`, `FEMALE_GENITALIA_EXPOSED`, `ANUS_EXPOSED`, `FEMALE_BREAST_EXPOSED`, `BUTTOCKS_EXPOSED`, `SEX_ACT`

The paired `*_COVERED` labels (lingerie, swimwear, implied nudity) are not reported as the headline tag, nor are `BELLY_*`, `ARMPITS_*` or face labels. `SEX_ACT` is not a NudeNet label — it is synthesised by `guess_pipeline.merge_sex_act_detections()` when two different genital labels overlap.

This set is **descriptive, not a gate**. It used to be guild-configurable; a per-label vocabulary has nothing to attach to under a single probability, and a stored preference that enforces nothing is worse than no preference at all, so the config key and its dashboard grid were retired (migration 147 deletes the rows).

No threshold is applied when picking the headline tag: NudeNet's own floor is the only bar that means anything for a label, and the configured threshold is a whole-image probability with nothing to say about one body part.

## Scope

**One definition of "image".** `is_image_attachment` in this module is the single predicate; `post_monitoring.attachment_is_image` delegates to it and the auto-react cog filters with `is_classifiable` (the same predicate plus the size cap). They previously disagreed — over `.tiff`, and over attachments Discord serves with no `content_type` — which meant a file could be handed to the classifier by one consumer and silently skipped by another.

**One documented exception.** `enforce_spoiler_requirement` filters on `SPOILER_IMAGE_EXTENSIONS` — `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, and no `content_type` check — so a `.bmp`, a `.tiff`, or an extensionless upload Discord types as an image is not subject to the *spoiler* rule. This predates the Marqo swap and is kept deliberately: everything the gate matches becomes deletable, including on an unreadable image, so widening it is a behaviour change rather than a tidy-up.

It is a named constant rather than an inline literal precisely so this can't be "fixed" by accident, and `test_spoiler_extensions_are_a_strict_subset` fails if the two lists converge or diverge further — a divergence recorded only here would be recorded only in the surface this project declares subordinate to the code.

**That is not a way through.** Spoiler enforcement returns early only when it *deleted*, so a skipped `.bmp` falls through to SFW prevention, which has no exemption for spoiler-required channels — only age-gated and explicitly exempt ones. In a spoiler-required channel that isn't age-gated, with prevention enforcing, that `.bmp` is still removed: by the other gate, at the stricter threshold, recorded under `surface='sfw'`. Under the shipped defaults (prevention off, spoiler channels usually age-gated) it simply isn't checked.

**Attachments only.** Embeds are never classified. The auto-react cog's `_has_image` matches `gifv`/`rich` embeds whose images live on arbitrary external hosts; fetching those would point the bot's outbound requests at member-supplied URLs — SSRF probing of the local network, IP-logging pixels, hostile payloads — so they are out of scope entirely. In a tipping-enabled channel this means embeds get no emoji at all, since a bot-placed emoji is a live tip and nothing may be tipped that wasn't classified.

Attachments over 25 MB are not downloaded. Downloads time out at 10 s. Both failures land as `UNKNOWN`.

Inference runs through `asyncio.to_thread` — onnxruntime blocks in C++, and calling it inline would stall the bot's heartbeat for the duration of every classification.

## Caching

What is cached is the **score**, not the verdict — the model's output depends only on the image, while `evaluate()` applies the threshold on top. So every consumer of an attachment shares one download and one inference no matter what bar each applies, and an admin who retunes the threshold can't be served a verdict computed under the old one.

One cache entry covers **both** models. They could be cached separately, but each would then need its own download of the same bytes, and holding the bytes to avoid that would keep up to 25 MB alive per cached attachment. So a single task downloads once, scores, and tags in the same pass. The entry records whether that pass included tagging; since tagging is a property of the attachment's *channel* and an attachment belongs to exactly one message, it cannot differ between two consumers of the same entry.

The cache (bounded LRU, 512 entries, keyed on attachment id) holds the **in-flight task** rather than its result. discord.py dispatches each cog's listener as its own task, so the consumers reach the classifier concurrently rather than in sequence; without this they would each start their own download of the same bytes.

A failed task is evicted, so a transient CDN failure doesn't pin "unreadable" for the life of the process.

Model loading is lazy and lock-guarded in both `marqo_nsfw` and `guess_nudenet`: several `to_thread` workers can reach the init at once, and without the lock each would build its own session.

## Binding to a message

Consumers don't call the classifier directly — they get one from `classifier_for(db_path, message, strict=...)`, which returns a small `MessageClassifier` value object. It derives `channel_is_nsfw` itself (callers used to pass it, which meant asserting a precondition enforced by a guard in another module), loads settings **once per message** rather than once per attachment, and does so lazily so that building one costs nothing — spoiler enforcement constructs one for every message in a watched channel but consults it only for an unspoilered image.

It is a value object rather than a closure deliberately: it copies the handful of ids it needs instead of capturing, and keeping alive, the whole `discord.Message`.

## Recording

**Coverage and recording deliberately differ.** Classification runs on attachments in *every* channel, because SFW prevention needs a verdict everywhere. Classification rows are written **only for uploads in Discord-age-gated (`is_nsfw`) channels**, so no dataset is built out of general chat.

### Observation

Recording being scoped to age-gated channels says *where* a row may be written; it does not say a row **is** written. Each consumer asks for a verdict only where it might act on one, so for the first week in production the table held 31 rows against ~441 image posts in the same channels — and every one of those 31 came from spoiler enforcement, because the other two consumers were unconfigured. What it recorded was therefore not "what gets posted here" but "what somebody forgot to spoiler": the compliance failures, and nothing else. Threshold tuning, the score histogram and the tagger-disagreement counts were all being read off the one sample guaranteed to be unrepresentative of the channel.

`observe_images` closes that gap. Where enabled it classifies **every** image attachment in an age-gated channel — spoilered or not, spoiler rule or not, whether or not any gate wanted an opinion — records the row, and then does nothing at all with the verdict.

It is not a fourth consumer, and the distinction is load-bearing:

- **It acts on nothing.** A spoilered image in a spoiler-required channel is *compliant*. The moment a verdict from this path can delete one, this stops being observation and becomes a gate that was never designed as one.
- **It does not move the privacy boundary.** Scope is age-gated channels, matching the boundary recording already lived inside — the same `channel_is_nsfw` flag drives both the row and the tagger. It widens *which* images inside that boundary are seen, and a general-chat upload is no more visible to it than before.
- **It is opt-in per guild** (`nsfw_observe_age_gated`, default off). What it does change is volume: `nsfw_detections` grows to cover ordinary compliant posts rather than only the ones a gate had to judge, and that table is the most sensitive thing this bot holds. Growing it is a decision a server makes, not a side effect of a deploy.

Cost is near-free where a gate already classifies — `_shared_infer` dedupes the download and both model passes per attachment, so observation and spoiler enforcement on the same upload cost one of each. Where no gate runs it is a fresh download plus inference, which is why it runs **fire-and-forget** from `on_message`: nothing downstream reads its result, and awaiting it would put a download per attachment in front of enforcement, persistence and XP. It never raises; a failure is a missing metrics row.

`nsfw_classifications` — one row per `(message_id, attachment_id)`: verdict, `marqo_score` (what the verdict was made from), the headline tag and its NudeNet confidence, model name, **the threshold that was applied**, inference milliseconds, and source byte size. Storing the threshold per row rather than reading config at query time is what keeps the data interpretable after a retune, and is what makes "what would 0.4 have changed?" answerable in hindsight.

`marqo_score IS NULL` identifies rows written before the swap, whose verdict came from NudeNet labels instead; the reports exclude them rather than mix two meanings of "explicit" into one number.

The `model` column names **both** engines (`marqo-384+640m`) — a row that named only one could not later be told apart from one whose tags are missing. Which NudeNet file is in play is read back from the loaded detector rather than assumed: the name used to be hardcoded to `320n`, which went wrong the moment `640m.onnx` was staged on disk and silently preferred.

`label_set` is written empty. No configurable label set governs a verdict any more, and writing the tagger's vocabulary there would imply one did.

`nsfw_detections` — every detection the tagger returned, *including non-qualifying ones*. A tag sweep is only possible if the near-misses were kept.

`UNKNOWN` verdicts are not recorded. A row claiming `verdict=0` for an image nobody could read would poison the accuracy metrics the table exists to provide.

`nsfw_blocks` — **every image a gate destroyed**, in *every* channel, including the ones no classification row is written for. Author, channel, filename, score, which gate (`sfw`/`spoiler`) and what happened (`removed`/`logged`). It exists because the places a deletion is most likely to be a mistake are exactly the places nothing else records.

It is deliberately not a body-part inventory: no labels, no boxes, no image bytes. `marqo_score IS NULL` means the image could not be read at all — spoiler enforcement deletes on an unreadable image by design, and a row claiming 0.0 would read as "the model was sure it was clean, and we deleted it anyway".

`author_id` is stored rather than joined. Both enforcement paths `return` from `on_message` **before** message persistence, so a blocked message never gets a `messages` row for authorship to join through; the minimisation `nsfw_classifications` uses is simply not available here.

Retention is indefinite for all three tables (a deliberate choice — see `docs/plans/nsfw-classifier-and-reaction-tips.md`).

### Privacy

`nsfw_detections` is the most sensitive table this bot holds: effectively a labelled body-part inventory of members' uploads. It is derived metadata rather than message content, which fits the project's "derive at ingest, store minimal" rule, but three minimisations apply regardless:

- **No `author_id` column** on `nsfw_classifications`/`nsfw_detections`. Authorship joins through `messages`.
- **The tagger never runs outside age-gated channels**, so no body-part inventory of a general-chat upload exists to leak, recorded or not. Observation does not relax this — it is scoped to the same channels and turned on per guild.
- **Admin-gated on the dashboard.** These rows are never surfaced to non-admins in any view.

## Configuration

Dashboard only; no in-Discord configuration. The **Image Guard** panel (Config → Moderation & Safety) holds all of it — spoiler-required channels, the SFW-prevention mode/log-channel/exemptions, and both thresholds.

Stored in the shared `config` table, per guild:

| key | default | what it does |
|---|---|---|
| `nsfw_classifier_threshold` | `0.5` | probability at which an image counts as explicit |
| `nsfw_classifier_sfw_threshold` | `0.75` | stricter bar used by SFW nudity prevention |
| `nsfw_observe_age_gated` | `0` | classify and record *every* image in age-gated channels, not only the ones a gate had to judge. Changes nothing about what happens to an image |

Both defaults are unchanged across the engine swap, and that is not laziness: the old values were detector confidences and the new ones are whole-image probabilities, but both live on the same 0–1 scale and 0.5/0.75 sit in a wide empty gap between the measured control scores (0.04–0.08) and the measured true positive (0.91).

Both thresholds are validated on read *and* on write, through the same `is_valid_threshold` predicate: a value outside `(0, 1]` is rejected, because such a value answers the same way for every image and would silently disable the gate rather than loosen it.

## Reports

Two admin-gated panels under Moderation → Audit Logs:

**Image Tags** (`/api/moderation/nsfw-tags`) — age-gated channels only, since that is the only scope the tagger runs in. Volume, verdict split, tag distribution with the mean verdict score per tag, and a 0.1-wide score histogram. Its two headline numbers are the **disagreements**: images the verdict engine called explicit that the tagger saw nothing in (the NudeNet blind spot that prompted the swap), and the reverse.

**Blocked Images** (`/api/moderation/nsfw-blocks`) — every channel. Who, where, which gate, what score, removed or log-only. This is how a false positive gets found and put right.

## Cost

Measured on the production host (Intel N150, 4 cores):

| | |
|---|---|
| cold start (Marqo import + model load) | ~470 ms, once per process |
| warm Marqo inference, 1536×1024 | 130–200 ms (~173 ms with preprocessing) |
| warm NudeNet tagging pass (age-gated only) | 74 ms median |
| model size on disk | 22 MB (`.onnx` + `.onnx.data`) |
| image posts/day, server-wide | 200–500 (~290 avg over 14 days) |
| busiest single minute observed | 31 images → ~5 s, ~8% of one core |

No new dependencies: onnxruntime, PIL and numpy were already in production.

Total compute is **lower** than before the swap in typical traffic, because NudeNet no longer runs on every image in every channel — only in age-gated ones. Compute is not the constraint regardless. The costs that matter are CDN bandwidth and the ~1–2 s between an image appearing and a consumer being able to act on it.

## Tests

`tests/test_marqo_nsfw.py` — preprocessing shape across eight aspect ratios (including the degenerate 4000×2 / 2×4000 pair that used to build a 0.9 GB intermediate), an assertion that **every resize targets 384×384 exactly** so the unbounded-intermediate bug cannot return, normalization range, channel-first RGB ordering, float32 dtype, centre-crop-not-squash, paletted/greyscale decoding, undecodable bytes, the NSFW label index, softmax overflow, missing-weights errors naming the file, and a single session under eight concurrent loaders.

`tests/test_nsfw_classifier_service.py` — threshold boundaries (inclusive at the threshold), the stricter-threshold override, tag membership per qualifying and non-qualifying label, model-name reporting, config parsing and out-of-range rejection, attachment type/size gating, `UNKNOWN` for download failure / timeout / inference failure / missing model / unclassifiable type, cache reuse across consumers, cache bypass on a differing threshold, `UNKNOWN` never cached, **the tagger never running or recording in a SFW channel**, tagging failure still recording the verdict, two consumers sharing one tagging pass and one download, recording writes both tables with near-misses kept, `UNKNOWN` never recorded, recording idempotent per attachment, no `author_id` column, and block rows keeping an unreadable image distinct from a low score.

`tests/test_post_monitoring.py` — spoiler and SFW block reporting, including the unreadable-image case and a report failure not resurrecting the message.

`tests/web/test_nsfw_report_routes.py` — both report endpoints plus the legacy Image Guard summary: the disagreement counts, the score histogram's top-bucket fold, pre-swap rows excluded from all three, snowflake-safe string ids, surface filtering, and guild scoping.
