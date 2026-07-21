# TGM Community Health Dashboard — Concept Spec

A concept doc for a 12-tile community-health dashboard. The Golden Meadow is an adult (21+) Discord community centered on genuine connection and consent-forward culture; NSFW content exists but is secondary to the social fabric. This dashboard monitors community health across six research-backed dimensions, surfaces actionable insights for admins and moderators, and gives members motivating (not competitive) stats.

The core principle: every metric must answer "what should I do next?" If a number can't be tied to a specific intervention when it moves outside healthy range, it doesn't belong on the dashboard. Total member count and all-time message count are explicitly excluded as vanity metrics.

This doc is intentionally implementation-light. Per-tile wiring belongs in feature specs.

---

## Existing data that feeds the dashboard

The bot already collects message volume, reply-to and @mention edges, presence, XP, recent joins, ticket / warning counts, voice occupancy, 1h-active channel/user snapshots, return-after-break, conversation-starter counts, "people talked to" counts, and NSFW media volume.

The reply-to / @mention data is the social graph — every reply is a directed edge from replier to author, every @mention is a directed edge from mentioner to mentioned. Aggregated over a 30-day rolling window and weighted by frequency, this gives clustering coefficient, betweenness centrality, reciprocity, community detection, network density, average path length, and the small-world quotient — without any new data collection.

---

## The six health dimensions

The composite score is built from six weighted dimensions. Activity and engagement carry the most weight because everything else depends on them; the remaining four are equally weighted because they're interdependent.

| Dimension | Weight | What it measures | Why it matters |
|---|---|---|---|
| Activity | 20% | How many people show up and how often | The baseline pulse |
| Engagement depth | 20% | Real conversations vs broadcast posting | Distinguishes community from chatroom |
| Participation distribution | 15% | Whether many people talk or just a few dominate | Concentrated participation creates fragility |
| Network health | 15% | Whether people form actual relationships | The social skeleton |
| Growth & retention | 15% | Whether new members stick around | Treadmill-vs-growth signal |
| Sentiment & tone | 15% | Vibe, stability, resilience | Lagging indicator of everything else; spikes are leading indicators of incidents |

---

## Dashboard structure

The dashboard has 12 tiles organized into four categories. Each tile has two views: a compact tile for the main grid (answers "do I need to worry?" in 2 seconds) and a full-page deep dive (answers "what exactly is happening and what should I do?").

Above the grid sits a **live status bar** showing right-now data — current online count, active users (1h), active channels (1h), in voice now, recent joins. (Concept only — not yet rendered as a header component.)

Three user tiers see different views: admins see everything, moderators see operational tiles without individual member data, members see personal stats and community milestones without moderation or network internals.

---

## Tile 1: DAU/MAU stickiness ratio

**What it measures.** The percentage of monthly active members who show up on any given day — the most universally cited engagement metric in community management.

**Why it matters.** Stickiness measures the daily pull. A server with 200 monthly actives but only 10 daily actives (5%) is "a place people check occasionally." Fifty daily actives (25%) is "a daily habit."

**What healthy looks like.** Above 20% is healthy for a social Discord server. Above 30% is excellent. Below 10% means most members aren't finding daily reasons to come back. Discord communities typically sit between 15–30%.

**Tile view.** DAU/MAU percentage with health badge; 30-day sparkline; WAU/MAU (smoother) and raw DAU/MAU counts with current online presence as real-time heartbeat.

**Full-page view.** 90-day DAU/MAU + WAU/MAU trend with a 20% green-zone floor; raw DAU vs MAU envelope (so a flat ratio with growing MAU still reads as growth); the engagement-depth funnel (total → MAU → WAU → DAU → voice-active); active-user composition (returning regulars vs reactivated vs brand-new); day-of-week breakdown.

**Tooltips.** Hover text explaining DAU/MAU, WAU/MAU, lurker activation rate, active-user composition, online-now, reactivated, weekend/weekday ratio, and the engagement-depth funnel.

---

## Tile 2: Activity heatmap

**What it measures.** Message density across every hour of every day of the week, averaged over a 30-day window.

**Why it matters.** Communities have biological rhythms. Knowing when the server is alive, when it's dead, and when it's active-but-unmoderated answers three operational questions: when to schedule events, when to staff mods, which channels to check on.

**What healthy looks like.** A clear daily cycle with evening peaks and overnight troughs. What matters is whether the pattern is consistent week-to-week (stable) or erratic (depending on a handful of individuals). Dead hours below 1 msg/hr are expected overnight, concerning during evenings.

**Tile view.** Compact 7×24 mini heatmap with continuous color scale; peak slot called out ("Sat 9–11pm"); quietest slot and dead-hours-per-week count.

**Full-page view.** Full heatmap with per-cell hover; weekday vs weekend hourly volume chart with voice occupancy overlaid; per-channel mini heatmaps (each channel's own rhythm); mod-coverage analysis ranking time windows where activity exceeds mod presence; event impact analysis with the optimal-new-event-slot recommendation.

**Tooltips.** Heatmap cell, dead hours, mod-coverage gap, event lift, optimal event slot.

---

## Tile 3: Channel health

**What it measures.** A composite score per channel built from message volume, unique participants, conversation depth, sentiment, and participation equity.

**Why it matters.** Channels are the rooms of the community. This tile decides which to celebrate, which to boost, which to merge, which to archive. Too many channels spreads conversation thin and lowers engagement across the board.

**What healthy looks like.** For a 150-MAU community, 10–14 active channels is the sweet spot. Each should have a clear purpose, multiple regular participants, and conversation depth appropriate to its purpose.

**Tile view.** Active-channel count with flagged-channel badge; top 5 by composite score with multi-metric mini-bars (volume / depth / sentiment); dormant (14+ days) and archive candidate (30+ days) counts.

**Full-page view.** Full channel roster table (score, msgs/day, weekly uniques, thread depth, sentiment, channel Gini, 30-day trend); for NSFW channels, the media-to-conversation ratio feeds the score and the SFW/NSFW bridge metric on the social graph tile; 90-day trend chart for key channels; thread-depth bar chart; channel-management recommendation cards (archive / merge / boost / celebrate).

**Tooltips.** Channel score, thread depth, channel Gini, dormant, archive candidate, message concentration, media-to-conversation ratio.

---

## Tile 4: Participation Gini coefficient

**What it measures.** Inequality of who does the talking, on the same 0–1 scale economists use for wealth. 0 = everyone posts equally, 1 = one person posts everything.

**Why it matters.** The best bullshit detector for vanity metrics. Total messages can rise while community health falls — the Gini forces you to look at the shape of participation. Above 0.85 means a handful of people are performing for an audience; if they burn out, visible activity collapses.

**What healthy looks like.** 0.50–0.70 for a Discord community. A "fat middle" — many moderate contributors — is what you want; a barbell (lots of lurkers + a few power users + nothing between) is unhealthy.

**Tile view.** Gini value with color badge (green < 0.70, amber 0.70–0.85, red > 0.85); 30-day sparkline; top-5% contribution share bar.

**Full-page view.** Lorenz curve (the visualisation of the Gini); 90-day trend with green / amber / red zone shading; per-channel Gini bars (event channels usually best, niche channels often high); participation tiers (lurker / light / moderate / active / power); Palma ratio (top-10% share / bottom-40% share); a weighted Gini that counts reactions (at reduced weight) and voice minutes alongside messages; an XP-distribution Gini comparing how well the XP formula rewards diverse contribution types vs raw message volume.

**Tooltips.** Gini coefficient, Lorenz curve, top-N% share, Palma ratio, participation tiers, weighted Gini, XP distribution Gini.

---

## Tile 5: Social graph health

> *Concept doc — the underlying reply/@mention data is recorded today (see [[reporting-spec]]), but the frontend force-directed graph, betweenness ranking, cross-cluster matrix, and SFW/NSFW bridge metric are not yet rendered.*

**What it measures.** The structure of relationships — who talks to whom, which friend groups have formed, whether subgroups connect or silo, which individuals hold the community together.

**Why it matters.** This is the deepest layer of community health. Activity, engagement, and sentiment are surface signals; the social graph is the skeleton. A community with high activity but a fragmented graph (isolated cliques) is one drama away from splitting. **Bridge users** — members active across subgroups, often spanning SFW and NSFW — are critical: lose enough of them and the community disconnects.

The existing "conversation starters" metric is especially powerful here. A starter whose threads attract replies from people who don't otherwise talk to each other is functioning as a bridge user — creating connections through content. The existing "people talked to" count is degree centrality; split into out-degree (people you reach out to) and in-degree (people who reach out to you), it reveals isolation risk (high out, low in — trying to connect, not getting responses) and bus-factor risk (high in, low out — community anchor).

**What healthy looks like.** Clustering coefficient 0.25–0.55. Average path length under 3.0 (small-world). Reciprocity above 0.35. At least 6 bridge users with no single one holding more than 25% of total betweenness.

**Tile view.** Clustering coefficient with health badge; mini sociogram (small force-directed preview with cluster colors and highlighted bridges); network density, bridge user count, and isolate count.

**Full-page view.** Interactive force-directed network (node size = degree, color = cluster, edge opacity = frequency, bridges highlighted); 90-day clustering / reciprocity / density trend; betweenness centrality ranking with bus-factor risk callouts; detected clusters with cross-cluster interaction matrix; **SFW/NSFW bridge health** section showing what percentage of members are active in both spaces and whether that's growing or shrinking.

**Tooltips.** Clustering coefficient, network density, average path length, reciprocity, betweenness centrality, bridge user, bus factor, isolates, cross-cluster interaction, small-world quotient, conversation-starter bridging, out-degree vs in-degree.

---

## Tile 6: Sentiment and tone

**What it measures.** Average message sentiment on a -1.0 to +1.0 scale, the mix of emotion categories (joy, playful, neutral, frustration, anger), and the pattern of negative spikes.

**Why it matters.** Sentiment is both a lagging indicator (declining average reflects degradation elsewhere) and a leading indicator (sudden negative spikes flag specific incidents — arguments, drama, boundary violations). Emotion category mix adds texture the average can't capture.

**What healthy looks like.** Average sentiment above +0.20. Positive-to-negative ratio above 3:1 (Gottman's relationship research suggests 5:1 is the threshold for healthy interpersonal dynamics; the same transfers to communities). Negative spikes brief (under 10 min), rare (<2/week), recover quickly (back to baseline within 20 min). 33%+ peer de-escalation rate (spikes resolved by community members before mod intervention) is a strong signal of self-regulation.

**Tile view.** Average sentiment with health badge; emotion-category distribution bars; negative spike count (7d); positive/negative ratio.

**Full-page view.** 90-day sentiment trend with 7-day rolling average and red-dot spike markers; emotion-category stacked area chart (week-to-week composition); per-channel sentiment as diverging bars with 30-day delta; negative-spike log with per-incident narrative cards; sentiment-event correlations (game-night lift, mod-action dip, weekend bump, newcomer-welcome burst); spike pattern analysis (duration, recovery, peer de-escalation rate, spike-to-churn correlation).

**Tooltips.** Average sentiment, positive/negative ratio, emotion categories, negative spike, sentiment stability, recovery time, peer de-escalation rate.

---

## Tile 7: Newcomer activation funnel

**What it measures.** How effectively the community converts a new join into a retained, connected member, as a multi-step funnel: join → first message → first reply received → 3+ channels visited → D7 return.

**Why it matters.** Onboarding is the single highest-leverage investment in community health. Members who rated onboarding "very easy" reported 95% engagement vs 18% for "difficult" — a 5x gap. The single most predictive metric: members who form 3+ reciprocal relationships within their first 60 days retain at 87%; those who don't retain at 34%.

**What healthy looks like.** Activation rate (full funnel) above 40%. Time-to-first-message under 4 hours. First-response latency under 5 minutes (the community's welcome speed).

**Tile view.** Activation rate with badge; mini funnel (5 narrowing bars); companion cards for time-to-first-message and first-response latency.

**Full-page view.** Funnel visualisation with stage-by-stage conversion rates; time-to-first-message distribution; cohort comparison (recent vs older activation); the 3-connection threshold as a prominent display; channel first-touch analysis (which channels newcomers visit first vs long-term retention).

**Tooltips.** Activation rate, time-to-first-message, first-response latency, 3-connection threshold.

---

## Tile 8: Cohort retention curves

**What it measures.** What percentage of each joining cohort (weekly groups) is still active at D1, D7, D14, D30, D60, and D90.

**Why it matters.** The only way to tell whether the community is getting better or worse at keeping members. A rising DAU/MAU could mask declining retention if growth is masking churn — cohort analysis separates the signals.

**What healthy looks like.** D7 above 60%. D30 above 40%. D90 above 25%. Recent cohorts should retain equal to or better than older ones — if newer cohorts retain worse, something changed.

**Tile view.** D7 retention for the most recent cohort with badge; mini retention decay curve; D30 retention and cohort size cards.

**Full-page view.** Family of retention curves (each weekly cohort a line, recent in strong colors, older faded); color-coded retention heatmap table; channel-correlated retention (which channels newcomers engage with first vs long-term retention).

**Tooltips.** D7 retention, D30 retention, retention curve, cohort.

---

## Tile 9: Churn risk early warning

**What it measures.** A 0–100 composite risk score per member predicting how likely they are to disengage, based on five signals: declining message frequency (30%), narrowing channel breadth (25%), loss of reciprocal replies (20%), negative sentiment trend (15%), growing gaps between visits (10%).

**Why it matters.** Churn is rarely sudden — there's a ~38-day window between detectable decline and actual departure. That's the intervention window. Gradual decline is more predictive than sudden drops (sudden stops are often vacations).

**What healthy looks like.** At-risk population (score 30+) under 10% of MAU. Save rate above 25%. Personal DMs from a mod achieve 62% re-engagement; event invites 45%; no intervention 8%.

**Tile view.** At-risk count with weekly-change badge; 30-day at-risk trend sparkline; three tier indicators — critical (80+, 7–14 days to departure), declining (50–79, 30–60 days), watch (30–49, early signals).

**Full-page view.** Signal weights panel (the five-factor model, transparent); at-risk roster table with per-member score, tier, and individual signal-strength bars; disengagement timeline plotting weekly message counts for critical members over 12 weeks; risk-score distribution histogram across the full MAU; prediction-accuracy metrics (true positives, false positives, lead time, save rate); intervention-effectiveness ranking; churn-trigger analysis (conflict-preceded 31%, social isolation 44%, natural fade 25%) with 3-connection threshold validation.

**Tooltips.** Churn risk score, critical tier, declining tier, watch tier, save rate, lead time, conflict-preceded churn, social isolation churn.

---

## Tile 10: Moderator workload

**What it measures.** How moderation work is distributed, how quickly incidents are addressed, and whether any mod is approaching burnout.

**Why it matters.** Moderation quality degrades when mods burn out. The workload Gini (the inequality measure applied to mod actions) captures concentration risk in a single number.

**What healthy looks like.** Median response time under 5 min. Workload Gini below 0.45 (no single mod carrying more than 30%). Average actions per mod per day under 8 for sustainability. Burnout risk scores below 50% for all mods. Escalation rate (warnings → timeouts) under 25%. Recidivism rate (warned members re-offending within 14 days) under 15%.

**Tile view.** Median response time with target badge; per-mod action bars showing the distribution; workload Gini and total 7d actions.

**Full-page view.** Per-mod profile cards (action count, response time, actions/day, active hours, burnout risk); response-time distribution showing median + P95 (P95 spikes during unmoderated hours connect back to the heatmap's coverage gaps); action-type enforcement pyramid (deletes / verbal warnings → timeouts → mutes → kicks → bans); repeat-offender tracking; escalation and recidivism rates.

**Tooltips.** Median response time, 95th percentile, workload Gini, burnout risk, escalation rate, recidivism rate, actions per mod per day.

---

## Tile 11: Incident detection

**What it measures.** Real-time anomaly detection across six signal types, with an incident log, post-incident analysis, and detection accuracy tracking.

**Why it matters.** The only real-time tile — everything else is backward-looking trends or forward-looking predictions. This one looks at *right now* and alerts on-call moderators within seconds of a raid, derailment, or harassment campaign. Message timing is often more predictive than content; bitter frustration typically appears ~3 comments before overt toxicity.

**What healthy looks like.** Zero active incidents most of the time. Average detection time under 60 seconds. Average resolution time under 5 minutes. False positive rate under 20%.

**Tile view.** Active-incident count with status badge (green clear / red active); 7-day incident timeline with resolved-incident dots colored by type; four alert-category indicators (velocity spikes, report clusters, raid attempts, sentiment storms).

**Full-page view.** Real-time velocity monitor (6-hour rolling window of msg/5min with dynamic 2-sigma alert threshold); incident log table over 30 days; six anomaly detection signal definitions as cards (velocity spike, new-account clustering, report clustering, sentiment storm, thread-depth anomaly, plus one more); post-incident narrative cards; detection-performance metrics (true / false positives, missed, precision over 90 days); incident timing pattern card.

**Tooltips.** Velocity spike, new account clustering, report clustering, sentiment storm, thread depth anomaly, false positive rate, detection precision.

---

## Tile 12: Composite health score

**What it measures.** A single 0–100 number rolling up all six dimensions into one answer to "how is my community doing?"

**Why it matters.** The admin needs a headline. The composite is the glanceable answer; the dimension breakdown tells you where to dig. Recommendations close the loop by connecting weak sub-metrics to specific actions.

**What healthy looks like.** 0–39 = Critical (immediate intervention). 40–59 = Needs work. 60–79 = Good. 80–100 = Excellent.

**Tile view.** Half-circle gauge with the 0–100 score, color-coded badge, and 30-day sparkline; six dimension mini-bars showing individual scores.

**Full-page view.** A **flower chart** is the centerpiece — six petals, petal length proportional to dimension score, current snapshot overlaid on a faded 30-day-ago ghost so improvement direction is visible. (A flower chart is used instead of a radar chart because radar charts distort perception — top dimensions look more important than bottom ones, and enclosed area doesn't scale linearly with the underlying values.) 90-day dimension trend (composite line thick, six dimension lines thin behind); full dimension breakdown with sub-metric score bars, weights, 30-day deltas; period comparison table (now / 30d / 90d / per dimension); **recommendations** ranked by estimated composite-score impact.

**Tooltips.** Community health score, dimension score, flower chart, score interpretation, estimated impact.

---

## User-facing tier — design principles

The user dashboard follows gamification research that motivates without creating toxic competition.

- **Private by default.** Personal stats only, unless the member opts into public visibility.
- **Impact over volume.** "Your messages got 48 reactions this week" beats "you sent 127 messages." The existing "conversation starters" metric is the perfect impact metric: "You started 3 conversations this week that got 28 total replies."
- **Reframe XP as progress.** Show the bar filling toward the next level or weekly goal, not the raw count. If the XP formula weights reactions, voice, and threads, surface that breakdown so members learn that diverse participation is valued.
- **Unique people talked to as personal growth.** Frame as "you talked to 12 people this week, up from 8" rather than "#47 of 156." Individual leaderboards on social metrics exclude lower-ranked members.
- **Team competition over individual ranking.** Small teams competing fosters cooperation within and healthy competition between.
- **Time-bounded resets.** Weekly or monthly leaderboard resets prevent permanent hierarchies.
- **Multiple recognition categories.** Most helpful, most creative, best newcomer mentor, best game-night energy, top conversation starter — not just "most messages."
- **Community milestones over individual stats.** Shared progress bars ("Community goal: 10,000 messages this month — we're at 7,200!") build collective identity without individual pressure.
- **Moderate gamification.** Research shows excessive gamification causes exhaustion — 25% of participants in one study reported increased stress from competitive elements. Less is more.
