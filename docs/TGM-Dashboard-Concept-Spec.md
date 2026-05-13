# TGM Community Health Dashboard — Concept Spec

This document describes what to build and why. It contains no implementation details — no code, no SQL, no library choices, no architecture. Those decisions belong to whoever builds it. What this document provides is the reasoning behind every metric, every visualization, every threshold, and every tooltip, so the builder understands the intent well enough to make good implementation decisions on their own.

---

## What this dashboard is for

The Golden Meadow is an adult (21+) Discord community centered on genuine human connection, consent-forward culture, and community-first values. NSFW content exists but is secondary to the social fabric. The dashboard monitors community health across six research-backed dimensions, surfaces actionable insights for admins and moderators, and provides motivating (not competitive) stats for members.

The core design principle is: every metric must answer "what should I do next?" If a number can't be connected to a specific intervention when it moves outside healthy range, it doesn't belong on the dashboard. Total member count and all-time message count are explicitly excluded as vanity metrics.

---

## Existing data and what it already covers

The TGM bot already collects and tracks a set of metrics. This section maps each one to its role in the new dashboard so nothing is lost in the transition and the builder knows which data streams are already flowing.

### Already collected — direct inputs to dashboard tiles

| Existing metric | What it is | Where it feeds in the dashboard |
|---|---|---|
| Message volume | Total messages per time period | Sub-metric for the activity dimension. On its own it's a vanity metric (it only goes up), but paired with DAU/MAU and the Gini it becomes meaningful context. Used as an input to channel health scores and the activity heatmap. |
| Reply-to and @mention data | Records of who replied to whom and who mentioned whom | This IS the social interaction graph. Every reply-to is a directed edge from the replier to the person they replied to. Every @mention is a directed edge from the mentioner to the mentioned. Weight each edge by frequency over a rolling 30-day window and you have everything needed to compute clustering coefficient, betweenness centrality, reciprocity, community detection, and path length. The hardest part of the social graph tile is already done. |
| Presence (online/idle/dnd) | Real-time count of who's online | Useful for the activity heatmap and mod coverage analysis. Knowing how many people are online vs how many are actively messaging gives a lurker-to-participant ratio in real time. A channel with 40 online and 3 talking is very different from 5 online and 3 talking. Add as a companion to DAU/MAU: "38 DAU of 156 MAU, 67 currently online." |
| XP today | Experience points earned per day | The existing gamification layer. For the user-facing tier, reframe as progress (toward next level or weekly goal) rather than raw count. For the admin tier, compute an XP distribution Gini to check whether the leveling system is concentrating or distributing engagement. If the XP formula already weights reactions, voice time, and thread participation, it's already aligned with the weighted Gini concept. |
| Recent joins | New member count | The top of the newcomer activation funnel. Instead of just "5 new joins today," track them through the funnel stages: how many sent a first message, how many got a reply, how many visited 3+ channels. The raw count becomes the funnel's entry point rather than a standalone metric. |
| Tickets and warnings | Moderation action counts | Direct input to the mod workload tile. The spec adds context: response time, per-mod distribution, escalation rate, recidivism rate. Raw count is useful as a volume indicator; the dashboard wraps it in context that tells you whether 12 tickets this week is normal or concerning. |
| In voice now | Real-time voice channel occupancy | Used in three places: the engagement depth funnel (voice active is the deepest tier), the activity heatmap (voice occupancy overlaid on message volume), and the weighted Gini (voice minutes as a participation signal). Display as a live companion metric on the DAU/MAU or heatmap tile. |
| 1h active channels / 1h active users | Real-time activity snapshot | Keep as a live status bar at the top of the dashboard grid, above the tiles. "Right now: 14 active users across 6 channels" gives immediate situational awareness. Also feeds the incident detection tile — if active channels suddenly spike from 6 to 12 in 5 minutes, that's a velocity anomaly. |
| Returned after break | Members who came back after a period of inactivity | Maps directly to the "reactivated" segment in the DAU/MAU tile's active user composition. The spec enriches it by tracking what brought them back (event, DM, spontaneous) and feeding it into the churn tile's save rate and intervention effectiveness metrics. |
| Conversation starters | Members who initiate threads that others reply to | A quality signal the spec didn't originally track but should. A member who posts 5 messages that each generate 10-reply threads contributes far more to community health than someone who posts 50 messages nobody responds to. Add as a sub-metric in the engagement depth dimension. Also surface in the user-facing tier as an impact metric: "You started 3 conversations this week that got 28 total replies." |
| Number of people talked to | Count of unique members someone interacted with | A proxy for degree centrality in the social graph. The spec takes it further with reciprocity and cross-cluster analysis, but the raw count is a great user-facing metric. Frame as personal progress ("you talked to 12 people this week, up from 8") rather than a competitive ranking — individual leaderboards on social metrics can make lower-ranked members feel excluded. |

### Already collected — should be added to the spec

| Existing metric | What it is | How to integrate |
|---|---|---|
| NSFW media volume | Count of media posts in NSFW channels | Track separately from NSFW conversation messages. The ratio of media posts to conversation messages is a culture signal for NSFW channels. A channel that's all media dumps with no conversation has high volume but low community value (the thread depth problem). Add as a sub-metric within the channel health tile for NSFW channels, and use it in the SFW/NSFW bridge health section of the social graph to see whether the people posting media are also the ones having conversations, or whether those are separate populations. |

### What the reply/mention data unlocks

Since reply-to and @mention data is already recorded, the social graph tile moves from "most engineering-heavy addition" to "batch job that runs on data you already have." Specifically:

Every reply-to creates a directed edge: replier → author of the parent message. Every @mention creates a directed edge: mentioner → mentioned user. Aggregate these over a 30-day rolling window, weight by frequency, and you have the full interaction graph.

From this graph you can compute: clustering coefficient (do friends of friends also talk?), betweenness centrality (who bridges subgroups?), reciprocity (are conversations two-way?), community detection (what natural clusters exist?), network density, average path length, and the small-world quotient.

The "number of people talked to" ranking you already track is degree centrality with a friendlier name. With the directional reply data, you can split it into out-degree (people you reach out to) and in-degree (people who reach out to you). A member with high out-degree but low in-degree is trying to connect but not getting responses — that's an isolation risk signal for the churn tile. A member with high in-degree but low out-degree is a community anchor people gravitate toward — valuable but also a bus-factor risk if they leave.

The "conversation starters" metric becomes even more powerful in this context. A conversation starter who triggers replies from people who don't otherwise talk to each other is functioning as a bridge user — they're creating connections between clusters through content. You can identify these members by checking whether the people who reply to their threads also reply to each other (high clustering = friend group reinforcement) or whether they're drawn from disconnected parts of the graph (the starter is bridging).

---

## The six health dimensions

The composite health score is built from six weighted dimensions. The weights reflect how much each dimension contributes to overall community health based on the academic research. Activity and engagement carry the most weight because they're the foundation everything else depends on. The remaining four dimensions are weighted equally because they're interdependent — network health affects retention, sentiment affects engagement, distribution affects network health.

| Dimension | Weight | What it measures | Why it matters |
|---|---|---|---|
| Activity | 20% | How many people show up and how often | The baseline pulse. Without activity, nothing else exists to measure. |
| Engagement depth | 20% | Whether people have real conversations or just broadcast | Activity without depth is a chatroom, not a community. Thread depth, cross-channel breadth, and conversation-starting quality distinguish genuine connection from drive-by posting. The existing "conversation starters" metric feeds directly here — a member who initiates threads that generate 10+ replies is contributing more to engagement depth than someone who posts 50 messages nobody responds to. |
| Participation distribution | 15% | Whether many people talk or just a few dominate | Concentrated participation creates fragility. If your top 3 posters go on vacation, does the server go silent? |
| Network health | 15% | Whether people form actual relationships with each other | The social graph is the community's skeleton. Clusters, bridges, and reciprocal ties determine whether the community holds together or fragments. |
| Growth and retention | 15% | Whether new members stick around and old members stay | A community that can't retain is on a treadmill — running to stay in place. Onboarding quality and churn prevention are the highest-leverage investments. |
| Sentiment and tone | 15% | Whether the vibe is positive, stable, and resilient | Sentiment is a lagging indicator of everything else. When other dimensions degrade, sentiment follows. But sentiment spikes are leading indicators of specific incidents. |

---

## Dashboard structure

The dashboard has 12 tiles organized into the four categories below. Each tile has two views: a compact tile for the main dashboard grid (answers "do I need to worry?" in 2 seconds) and a full-page deep dive (answers "what exactly is happening and what should I do?").

Above the tile grid, a live status bar provides real-time situational awareness using existing data: current online count, active users (1h), active channels (1h), in voice now, and recent joins. This bar uses data streams that already exist and gives the admin an "at a glance right now" before they look at any tile.

Three user tiers see different views: admins see everything, moderators see operational tiles without individual member data, and users see personal stats and community milestones without moderation or network internals.

---

## Tile 1: DAU/MAU stickiness ratio

### What it measures

The percentage of monthly active members who show up on any given day. This is the most universally cited engagement metric in community management and product analytics.

### Why it matters

DAU/MAU tells you how "sticky" your community is — how strong the daily pull is. A server with 200 monthly actives but only 10 daily actives (5%) has a very different character than one with 50 daily actives (25%). The first is a place people check occasionally. The second is a daily habit, a place people want to be.

### What healthy looks like

Above 20% is healthy for a social Discord server. Above 30% is excellent. Below 10% means most members aren't finding daily reasons to come back. For context, the highest-performing social apps (like messaging platforms people use daily) hit 50%+. Discord communities typically sit between 15-30%.

### Tile view

Show the DAU/MAU percentage as the big number with a health badge. Include a 30-day sparkline showing whether stickiness is trending up or down. Below, show WAU/MAU (weekly stickiness — smoother, less noisy) and the raw DAU count against MAU with the current online presence count for real-time context ("38 DAU of 156 MAU, 67 online now"). The presence data already exists — adding it here gives the ratio a real-time heartbeat.

### Full-page view

The full page adds four things the tile can't show:

First, a 90-day trend of both DAU/MAU and WAU/MAU together, with a green zone floor at 20%. This shows trajectory — is stickiness improving, declining, or flat?

Second, the raw active user counts (DAU line against MAU envelope). This prevents a misleading ratio: if DAU/MAU stays flat at 24% but MAU doubled, that means DAU also doubled, which is very different from a stagnant community. The ratio alone hides growth.

Third, an engagement depth funnel showing total members → monthly active → weekly active → daily active → voice active. This reveals the conversion rates at each depth level. A community with 312 members, 156 MAU, 81 WAU, 38 DAU, and 14 voice active has clear conversion percentages at each stage. The lurker activation rate at the bottom (what percentage of never-posters sent their first message this month) is one of the highest-leverage onboarding metrics.

Fourth, the active user composition breakdown: what percentage of today's active users are returning regulars vs reactivated (came back after a lapse) vs brand new (first 7 days). The "returned after break" metric that already exists maps directly to the "reactivated" segment here. If your daily activity is mostly new members, you're churning the back as fast as you fill the front — a treadmill. If it's mostly returning, your core is healthy. The spec enriches the existing "returned after break" count by also tracking what brought them back — did they return after an event, after a personal DM, or spontaneously? This feeds the churn tile's intervention effectiveness data.

Also include a day-of-week breakdown (average DAU per weekday) to show the weekend vs weekday pattern, which informs event scheduling.

### Tooltips

| Element | Hover text |
|---|---|
| DAU/MAU ratio | "The percentage of your monthly active members who show up on any given day. Above 20% is healthy for a social Discord server. Higher means people come back more often." |
| WAU/MAU ratio | "The percentage of monthly active members who show up at least once per week. Smoother than daily — shows your true weekly engagement rhythm." |
| DAU | "Unique members who sent a message or joined voice today. Counts each person once regardless of message count." |
| MAU | "Unique members active at least once in the past 30 days. Your 'real' community size — people who actually participate, not just names on the list." |
| Lurker activation rate | "Percentage of previously-silent members who sent their first message in the last 30 days. Measures how well your community converts readers into participants." |
| Active user composition | "Breaks daily actives into returning (loyal regulars), reactivated (came back after a break), and new (first 7 days). A healthy community has a large returning base." |
| Online now | "Members currently showing as online, idle, or do-not-disturb in Discord. Compared to DAU, this shows how many people are present but not yet participating — your potential conversation pool." |
| Reactivated | "Members who returned after a period of inactivity. Tracking what brought them back (event, DM, spontaneous) feeds the churn tile's intervention effectiveness data." |
| Weekend/weekday ratio | "How much more active weekends are vs weekdays. Helps you understand your community's natural schedule and where mid-week events could have impact." |
| Engagement depth funnel | "Shows how many members reach each level: total → monthly → weekly → daily → voice. Each step represents deeper commitment." |

---

## Tile 2: Activity heatmap

### What it measures

Message density across every hour of every day of the week, averaged over a 30-day window.

### Why it matters

Communities have biological rhythms. Knowing when your server is alive, when it's dead, and when it's active but unmoderated lets you make three operational decisions: when to schedule events (amplify existing energy), when to staff moderators (cover high-activity windows), and which channels to check on (some channels have different rhythms than the server average).

### What healthy looks like

A clear daily cycle with evening peaks and overnight troughs is normal. What matters is whether the pattern is consistent week-to-week (stable) or erratic (unstable — activity depends on a few individuals rather than community rhythm). Dead hours (below 1 msg/hr average) are expected overnight but concerning during evenings.

### Tile view

Show a compact 7×24 mini heatmap with a continuous color scale (light = quiet, saturated = busy). Call out the peak slot ("Sat 9–11pm") prominently. Below, show the quietest slot and the number of dead hours per week.

### Full-page view

The full page provides five layers:

The server-wide heatmap at full size with hover tooltips showing exact messages/hour for every cell. This is the "when is my community alive?" view.

An hourly volume comparison chart showing weekday vs weekend patterns stacked, with voice channel occupancy overlaid. Voice activity peaks later than text — people hop into voice after text conversation builds momentum. Showing both reveals the flow of a typical evening.

Per-channel mini heatmaps for major channels. Each channel has its own rhythm: #general follows the server pattern, #nsfw-chat shifts later and heavier on weekends, #game-night blazes only on Saturday, voice lights up Friday and Saturday evenings. These let the admin see whether a channel's activity is healthy and rhythmic or erratic and dying.

A mod coverage analysis showing ranked time windows where community activity exceeds moderator presence, with message rates and severity indicators. This directly answers "when do I need another mod online?"

Event impact analysis showing the percentage lift from scheduled events vs non-event baseline, plus a recommendation for the optimal new event slot — the time with the highest existing baseline that hasn't been programmed yet.

### Tooltips

| Element | Hover text |
|---|---|
| Heatmap cell | "[Day] [Hour]: [X] msgs/hr average over past 30 days" |
| Dead hours | "Hours where the average message rate drops below 1 per hour. Incidents during these times go unnoticed longer." |
| Mod coverage gap | "Time windows where activity is high (5+ msgs/hr) but no moderator is online. Your highest-risk windows for unaddressed incidents." |
| Event lift | "Percentage increase in activity during a scheduled event vs the same time slot on a non-event week. Shows how much each event amplifies engagement." |
| Optimal event slot | "The time with the highest existing activity that doesn't already have an event. Adding one here amplifies what's naturally happening." |

---

## Tile 3: Channel health

### What it measures

A composite score for each channel based on message volume, unique participants, conversation depth, sentiment, and participation equity.

### Why it matters

Channels are the "rooms" of your community. Some are vibrant gathering places. Some are quiet corners where a few enthusiasts talk. Some are dead weight that clutter navigation and fragment attention. This tile helps you decide which channels to celebrate, which to boost, which to merge, and which to archive. Research from Discord's own community team found that too many channels spreads conversation thin and lowers engagement across the board.

### What healthy looks like

For a community of 150 MAU, 10–14 active channels is the sweet spot. Each active channel should have a clear purpose, multiple regular participants (not just 1–2 people talking to themselves), and conversation depth appropriate to its purpose (a #memes channel will naturally have shallow threads, but a #general chat should average 3+ replies per thread).

### Tile view

Show the count of active channels with a flagged count badge. List the top 5 channels ranked by composite score, each with tiny multi-metric bars showing relative volume, depth, and sentiment. Below, show dormant channels (14+ days inactive) and archive candidates (30+ days inactive).

### Full-page view

The full page has four sections:

A complete channel roster table — every channel scored, ranked, and tagged with a status (healthy, flagged, dormant, archive candidate). Columns show the composite score, messages per day, unique weekly users, average thread depth, sentiment, participation Gini, and 30-day trend direction. This is the admin's channel management command center.

For NSFW channels specifically, the channel health score should also factor in the media-to-conversation ratio. NSFW media volume is already tracked — comparing it to conversation message volume reveals whether a channel is building community through discussion or functioning as a content dump. A channel with high media posts but shallow threads (under 1.5 average replies) has volume without connection value. The ratio also feeds the SFW/NSFW bridge health section of the social graph tile: are the people posting media also the ones having conversations, or are those separate populations?

A 90-day trend chart tracking composite scores for key channels. Rising, stable, and declining trajectories are immediately visible. A channel that dropped from 52 to 38 in 3 months needs attention now.

A thread depth analysis showing average replies per thread by channel as a horizontal bar chart. This connects to Jenny Preece's research framework: deep threads (5+) indicate substantive relationship-building, shallow threads (under 1.5) indicate broadcast-style posting. The most important insight: a channel can have high sentiment and decent volume but zero community-building value if nobody replies to each other.

Channel management recommendations as actionable cards: archive dead channels, decide whether declining channels should be revitalized or merged, boost niche channels with structured challenges, and celebrate the channels that embody what you want the community to be.

### Tooltips

| Element | Hover text |
|---|---|
| Channel score | "A 0–100 composite based on volume, unique participants, thread depth, sentiment, and participation equity. Below 50 may need intervention or archiving." |
| Thread depth | "Average replies per conversation thread. 3+ indicates real conversation. Below 1.5 means people post but nobody responds — broadcasting, not community." |
| Channel Gini | "How evenly participation is distributed. 0.42 = many people contribute (healthy). 0.89 = 1-2 people dominate (fragile)." |
| Dormant | "No messages in 14+ days. Adds navigation clutter without contributing value." |
| Archive candidate | "No messages in 30+ days. Archiving declutters the server without losing anything." |
| Message concentration | "Percentage of all server messages from your top 3 channels. High concentration means smaller channels may struggle to sustain themselves." |
| Media-to-conversation ratio | "For NSFW channels: the ratio of media posts to conversation messages. A channel that's all media dumps with no replies has volume without community value. Healthy NSFW channels have a mix — media sparks conversation, conversation builds trust." |

---

## Tile 4: Participation Gini coefficient

### What it measures

The inequality of who does the talking, using the same mathematical tool economists use to measure wealth inequality. Ranges from 0 (everyone posts equally) to 1 (one person posts everything).

### Why it matters

This is the single best "bullshit detector" for vanity metrics. Total messages can go up while community health goes down — if 50 new members join but don't post while 3 power users post more, messages/day increases but the community is actually more fragile. The Gini forces you to look at the shape of participation, not just the volume.

A Gini above 0.85 means a handful of people are performing for an audience. If those people burn out, take a break, or have a falling out, visible activity collapses overnight. The research calls this "bus factor" risk.

### What healthy looks like

0.50–0.70 is the healthy range for a Discord community. Some concentration is natural and expected (every community has more-active and less-active members), but it shouldn't be extreme. The "fat middle" — many moderate contributors — is what you want to see in the participation tier breakdown.

### Tile view

Show the Gini value as the big number with a color-coded badge (green below 0.70, amber 0.70–0.85, red above 0.85). Include a 30-day sparkline and a top-5% contribution share bar that shows what fraction of all messages comes from the most active 5% of members.

### Full-page view

The Lorenz curve is the centerpiece — it visually shows "the bottom X% of members produce Y% of messages." The shaded area between the equality diagonal and the actual curve IS the Gini coefficient, visualized. On hover, each point should say something like "Bottom 80% of members produce 20% of messages."

The 90-day trend line shows whether inequality is growing, shrinking, or stable, with green/amber/red zone shading for context.

Per-channel Gini bars reveal which channels have equitable participation vs dominated conversations. Event channels like #game-night and #truth-or-dare typically have the best (lowest) Gini because games encourage equal turns. Niche interest channels often have high Gini, which may be acceptable in context.

Participation tier tracking breaks members into lurker (0 msgs), light (1–5/week), moderate (6–20), active (21–50), and power (50+), showing the percentage in each tier. A healthy community has a fat middle. An unhealthy one has a barbell (lots of lurkers, a few power users, nothing between).

The Palma ratio (top 10% share divided by bottom 40% share) provides an alternative inequality view that's more sensitive to changes in the extremes.

A weighted variant of the Gini should also be available that counts reactions (at reduced weight) and voice channel minutes alongside messages, so members who engage through listening, reacting, and voice rather than typing aren't invisible.

An XP distribution Gini should also be computed using the existing XP data. This checks whether the leveling system itself is concentrating or distributing engagement. If the XP Gini is significantly lower than the message Gini, the XP formula is doing a good job of recognizing diverse contribution types. If they're similar, the XP system is essentially just rewarding message volume and should be reweighted.

### Tooltips

| Element | Hover text |
|---|---|
| Gini coefficient | "Measures inequality in who's doing the talking. 0 = everyone posts equally, 1 = one person posts everything. 0.50–0.70 is healthy for Discord. Above 0.85 means a handful of people carry the whole conversation — fragile, because losing them would collapse visible activity." |
| Lorenz curve | "Shows cumulative messages (y) produced by cumulative members (x), sorted least to most active. The further the curve bows below the diagonal, the more concentrated participation is. The shaded area IS the Gini, visualized." |
| Top N% share | "Percentage of all messages produced by the most active 1%, 5%, or 10%. When your top 5% produce 60%+ of messages, losing a couple of power users would dramatically reduce visible activity." |
| Palma ratio | "Messages by the top 10% divided by messages from the bottom 40%. A Palma of 6.2 means your top 10% produce 6.2x as many messages as your bottom 40% combined. Target: below 4.0." |
| Participation tiers | "Members grouped by weekly activity: Lurker (0), Light (1–5), Moderate (6–20), Active (21–50), Power (50+). A healthy community has a 'fat middle.' An unhealthy one has lots of lurkers and a few power users with nothing in between." |
| Weighted Gini | "Counts reactions (at 0.25x) and voice minutes (0.5 min = 1 message) alongside text, so members who engage through listening and reacting aren't invisible." |
| XP distribution Gini | "Applies the Gini measure to the existing XP system. If this is much lower than the message Gini, the XP formula successfully rewards diverse contribution types. If they're similar, XP is basically just counting messages and should be reweighted." |

---

## Tile 5: Social graph health

### What it measures

The structure of relationships in the community — who talks to whom, whether friend groups have formed, whether subgroups are connected or siloed, and which individuals hold the community together. Built from reply-to and @mention data that is already being collected — every reply is a directed edge, every mention is a directed edge. The graph is already implicit in the existing data; this tile makes it visible.

### Why it matters

This is the deepest layer of community health. Activity, engagement, and sentiment are all surface signals. The social graph is the skeleton underneath. A community with high activity but a fragmented graph (isolated cliques that don't interact) is one drama away from splitting. A community with moderate activity but a dense, well-connected graph is resilient — it can absorb shocks because relationships hold it together.

The most important concept here is "bridge users" — members who participate across different subgroups and connect them. In TGM, these are often people active in both SFW and NSFW channels, or people who attend game nights and also participate in deep late-night conversations. If too few people serve this bridge function, losing them disconnects the community.

The existing "conversation starters" metric becomes especially powerful in this context. A conversation starter who triggers replies from people who don't otherwise talk to each other is functioning as a bridge user — they're creating connections between clusters through content, not just participation. You can identify these members by checking whether the people who reply to their threads also reply to each other (high clustering in the starter's neighborhood = friend group reinforcement) or whether they're drawn from disconnected parts of the graph (the starter is bridging clusters).

The existing "number of people talked to" ranking is degree centrality with a friendlier name. With the directional reply data, it splits into out-degree (people you reach out to) and in-degree (people who reach out to you). A member with high out-degree but low in-degree is trying to connect but not getting responses — that's an isolation risk signal for the churn tile. A member with high in-degree but low out-degree is a community anchor people gravitate toward — valuable but also a bus-factor risk if they leave.

### What healthy looks like

Clustering coefficient between 0.25 and 0.55 means real friend groups have formed but the community isn't so cliquish that outsiders can't break in. Average path length under 3.0 means small-world structure — any two members are connected through at most 2–3 hops. Reciprocity above 0.35 means conversations go both ways. At least 6 bridge users, with no single person holding more than 25% of total betweenness centrality.

### Tile view

Show the clustering coefficient as the big number with a health badge. Include a mini sociogram — a small preview of the actual interaction graph with nodes colored by cluster and bridge users highlighted in a distinct color. Below, show network density, bridge user count, and isolate count (members with zero reciprocal ties).

### Full-page view

The interactive network visualization is the centerpiece — a force-directed layout where node size represents how many people someone interacts with, node color represents which community cluster they belong to, edge opacity represents interaction frequency, and bridge users are highlighted. The admin can visually see the community's social structure — tight clusters, bridge connections, peripheral isolates.

The 90-day trend chart tracks clustering coefficient, reciprocity, and density together. Clustering and reciprocity trending upward together is the ideal — both local cohesion and two-way engagement are growing.

The betweenness centrality ranking shows which members serve as bridges, ranked by how much of the community's social connectivity passes through them. This doubles as a bus-factor risk dashboard — if the top 2 users hold 37% of total betweenness, that's dangerously concentrated.

Detected clusters shows the results of community detection analysis — which subgroups exist, how large they are, how internally dense they are. The cross-cluster interaction matrix (a small heatmap showing average daily interactions between each pair of clusters) reveals which subgroups talk to each other and which are siloed.

The SFW/NSFW bridge health section tracks the specific metric most relevant to TGM's dual nature: what percentage of members are active in both SFW and NSFW spaces, and whether that cross-boundary interaction is growing or shrinking.

### Tooltips

| Element | Hover text |
|---|---|
| Clustering coefficient | "Do your members' friends also talk to each other? High clustering (0.25–0.55) means real friend groups have formed. Low means people interact with the community but haven't built tight bonds." |
| Network density | "The fraction of all possible member-to-member connections that actually exist. Density naturally drops as communities grow — track it within subgroups rather than server-wide." |
| Average path length | "How many 'hops' to connect any two members through their interaction chains. Under 3.0 = small-world structure — information and culture spread efficiently." |
| Reciprocity | "Percentage of interactions that go both ways. If Alice replies to Bob, does Bob reply to Alice? Above 0.35 indicates genuine two-way relationships rather than one-sided broadcasting." |
| Betweenness centrality | "How many shortest paths between other members pass through this person. High-betweenness users connect different subgroups — losing them could disconnect the community." |
| Bridge user | "A member who connects otherwise separate subgroups. Target: 6+ bridge users, none holding more than 25% of total betweenness." |
| Bus factor | "How many bridge users you could lose before the social graph fragments. Named after 'what if this person got hit by a bus?' Low bus factor = dangerously concentrated." |
| Isolates | "Members with zero reciprocal ties — they haven't formed any mutual relationships. These members are at the highest churn risk. Research shows 3+ reciprocal ties in the first 60 days predicts 87% retention vs 34% without." |
| Cross-cluster interaction | "Average daily interactions between members of different subgroups. High = integrated community. Low = siloed subgroups that could drift apart." |
| Small-world quotient | "Clustering divided by density. Above 3.0 = small-world structure — tight local friend groups connected by short paths. The ideal social topology." |
| Conversation starter bridging | "Members whose threads attract replies from people who don't otherwise talk to each other. These people create connections between clusters through content — a different kind of bridge user from someone who simply participates everywhere." |
| Out-degree vs in-degree | "Out-degree = people you reach out to. In-degree = people who reach out to you. High out / low in = trying to connect but not getting responses (isolation risk). High in / low out = community anchor others gravitate toward (bus-factor risk if they leave)." |

---

## Tile 6: Sentiment and tone

### What it measures

The emotional temperature of the community — average message sentiment on a -1.0 to +1.0 scale, the mix of emotion categories (joy, playful, neutral, frustration, anger), and the pattern of negative spikes.

### Why it matters

Sentiment is both a lagging and leading indicator. As a lagging indicator, declining average sentiment over weeks reflects degradation in other dimensions (engagement dropping, conflicts increasing, key members drifting away). As a leading indicator, sudden negative spikes flag specific incidents that need immediate attention — arguments, drama, boundary violations.

The emotion category breakdown adds texture that the average can't capture. A community that's 38% joy and 26% playful feels very different from one that's 60% neutral and 4% joy, even if their sentiment averages are similar.

### What healthy looks like

Average sentiment above +0.20 is healthy. The positive-to-negative ratio should be above 3:1 — research from relationship psychology (Gottman) suggests roughly 5:1 is the threshold for healthy interpersonal dynamics, and this transfers to community dynamics. Negative spikes should be brief (under 10 minutes), rare (fewer than 2 per week), and recover quickly (back to baseline within 20 minutes). A 33%+ peer de-escalation rate (spikes resolved by community members before mod intervention) is a strong signal of a mature, self-regulating community.

### Tile view

Show the average sentiment with a health badge. Include emotion category distribution bars and two companion metrics: negative spike count (7d) and positive/negative ratio.

### Full-page view

The 90-day sentiment trend shows daily average sentiment with a 7-day rolling average smoothing the noise, and red dots marking negative spike events on the timeline. The spike dots immediately draw the eye to trouble spots.

The emotion category stacked area chart shows how the balance of joy, playful, neutral, frustration, and anger evolves week-to-week. Stable composition means consistent culture. Shifting composition (frustration growing, playfulness shrinking) signals culture drift.

Per-channel sentiment as diverging bars from a center line (negative left, positive right) reveals which channels are most positive and which are struggling. The delta column shows 30-day direction. A channel with positive sentiment but a negative delta is one to watch.

The negative spike log provides narrative cards for each recent spike: when it happened, which channel, what triggered it, how deep the sentiment dropped, how long it lasted, and how it was resolved. These are the community's incident case studies.

Sentiment-event correlations connect mood to causes: how much do game nights lift sentiment (+0.18 in sample data), how much does a moderation action dip it (-0.12, recovering in 45 minutes), how much higher is weekend sentiment than weekday (+0.08), and how much does a newcomer welcome burst boost the room (+0.22).

Spike pattern analysis at the bottom shows average duration, recovery time, peer de-escalation rate, and spike-to-churn correlation (what percentage of members involved in a negative spike enter the churn watch list within 14 days).

### Tooltips

| Element | Hover text |
|---|---|
| Average sentiment | "Mean emotional tone of all messages, -1.0 (very negative) to +1.0 (very positive). Above +0.20 is healthy. The number matters less than its trend over time." |
| Positive/negative ratio | "Positive messages for every negative one. 4.6:1 means ~5 positive per 1 negative. Relationship research suggests 5:1 is the threshold for healthy dynamics — applies to communities too." |
| Emotion categories | "Messages classified into joy, playful, neutral, frustration, anger. Healthy communities are majority joy + playful, with frustration and anger under 15% combined." |
| Negative spike | "A period where sentiment drops below -0.3 for 5+ minutes. Often corresponds to arguments, drama, or external events. Logged with duration, depth, and recovery time." |
| Sentiment stability | "How much daily sentiment varies from average. Low variance (high stability) = consistent tone. High variance = mood swings driven by events or individuals." |
| Recovery time | "Minutes for sentiment to return to baseline after a spike. Fast recovery (under 15 min) indicates resilience — the community self-corrects." |
| Peer de-escalation rate | "Percentage of spikes resolved by community members before a mod intervenes. Higher = more mature, self-regulating culture. One of the strongest health signals." |

---

## Tile 7: Newcomer activation funnel

### What it measures

How effectively the community converts a new join into a retained, connected member, tracked as a multi-step funnel: join → first message → first reply received → 3+ channels visited → D7 return.

### Why it matters

Onboarding is the single highest-leverage investment in community health. Research found that members who rated onboarding "very easy" reported 95% engagement and 93% five-year renewal intent, versus 18% engagement for those who found it "difficult." That's a 5x gap. Everything else on this dashboard — gamification, events, analytics — amplifies or squanders the foundation that onboarding establishes.

The single most predictive metric: members who form 3 or more reciprocal relationships within their first 60 days retain at 87%. Those who don't retain at 34%. The funnel tracks progress toward that threshold.

### What healthy looks like

Activation rate (completing the full funnel) above 40% is strong. Time-to-first-message under 4 hours. First-response latency (how fast someone replies to a newcomer) under 5 minutes — this is the community's "welcome speed."

### Tile view

Show activation rate as the big number with badge. Include a mini funnel (5 narrowing bars) and companion cards for time-to-first-message and first-response latency.

### Full-page view

The funnel visualization with conversion rates at each stage, revealing exactly where newcomers drop off. A time-to-first-message distribution chart showing how many newcomers post within 1hr, 4hr, 24hr, 48hr, or never. Cohort comparison showing whether recent cohorts activate better than older ones. The 3-connection threshold as a prominent display. Channel first-touch analysis showing which channels newcomers visit first and how that correlates with their long-term retention.

### Tooltips

| Element | Hover text |
|---|---|
| Activation rate | "Percentage of new members completing the full sequence: join → first message → get a reply → visit 3+ channels → return after 7 days. The single highest-leverage metric for community growth." |
| Time-to-first-message | "How quickly new members send their first message. Shorter = better. Members who post within the first hour are significantly more likely to stay." |
| First-response latency | "How quickly someone replies to a newcomer's first message. This is the community's 'welcome speed.' Under 5 minutes is excellent." |
| 3-connection threshold | "Members who form 3+ mutual relationships in their first 60 days retain at 87%. Those who don't: 34%. This is the single most predictive onboarding metric." |

---

## Tile 8: Cohort retention curves

### What it measures

What percentage of each joining cohort (usually grouped by week) is still active at D1, D7, D14, D30, D60, and D90 after joining.

### Why it matters

Retention curves are the only way to tell whether your community is getting better or worse at keeping new members over time. A rising DAU/MAU ratio could mask declining retention if you're growing fast enough — new members replace churned ones, so the aggregate looks fine while the underlying health degrades. Cohort analysis separates these signals.

The shape of the curve matters as much as the numbers. A curve that drops steeply in the first week then flattens (steep drop, then plateau) is healthier than one that declines gradually forever. The plateau represents your "sticky core" — members who've integrated and will likely stay long-term.

### What healthy looks like

D7 retention above 60%. D30 above 40%. D90 above 25%. Recent cohorts should retain equal to or better than older ones — if newer cohorts are retaining worse, something changed (onboarding, community culture, moderation approach) and needs investigation.

### Tile view

Show D7 retention for the most recent cohort as the big number with badge. Include a mini retention decay curve and companion cards for D30 retention and cohort size.

### Full-page view

A family of retention curves — each weekly cohort gets its own line, recent cohorts in stronger colors, older in faded. This reveals at a glance whether retention is improving over time. A color-coded retention table (cohort rows × time columns) with cell shading from green to red provides the classic SaaS-style heatmap view. Channel-correlated retention shows which channels newcomers engage with first and how that correlates with long-term retention.

### Tooltips

| Element | Hover text |
|---|---|
| D7 retention | "Percentage of a joining cohort still active 7–14 days after joining. Discord's primary retention metric. Above 60% is healthy." |
| D30 retention | "Percentage still active after 30 days. Separates 'tried it out' from 'integrating.' Above 40% is strong." |
| Retention curve | "Shows what percentage of a cohort is still active over time. All curves decline — what matters is the shape. A curve that flattens early means your sticky core forms fast." |
| Cohort | "A group of members who joined the same week. Tracking cohorts separately shows whether you're getting better or worse at keeping new members." |

---

## Tile 9: Churn risk early warning

### What it measures

A composite risk score (0–100) for each member predicting how likely they are to disengage and leave, based on five behavioral signals.

### Why it matters

Churn is rarely sudden. Research found an average of 38 days between detectable decline and actual departure. That's a 5-week intervention window — enough time to reach out, reconnect, and potentially save the member. The system makes this invisible decline visible before it's too late.

The five signals, weighted by predictive power: declining message frequency (30%), narrowing channel breadth (25%), loss of reciprocal replies (20%), negative sentiment trend (15%), and growing gaps between visits (10%). Gradual frequency decline is more predictive than sudden drops — sudden stops are often vacations, but slow fading is genuine disengagement.

### What healthy looks like

At-risk population (score 30+) below 10% of MAU. Save rate (flagged members who re-engage) above 25%. Personal DMs from an admin or mod achieve a 62% re-engagement rate — by far the most effective intervention. Event invites achieve 45%. No intervention at all: only 8% re-engage naturally.

### Tile view

Show the at-risk count as the big number with a weekly change badge. Include a sparkline showing the at-risk population trend and three tier indicators: critical (score 80+, estimated 7–14 days to departure), declining (50–79, 30–60 days), and watch (30–49, early signals only).

### Full-page view

The signal weights panel shows the five-factor model transparently — what the score is made of and how each factor is weighted.

The at-risk member roster table shows each flagged member with their composite score, severity tier, individual signal strength bars (so you can see WHY they're flagged — is it frequency? breadth? reciprocity?), and last-seen timestamp for urgency context.

The disengagement timeline plots weekly message counts for critical members over 12 weeks, visualizing the gradual decline pattern. This makes the abstract score visceral — you can see someone fading away in real time.

A risk score distribution histogram shows the full MAU population, with the healthy left-skewed bulk and the flagged tail extending right. This contextualizes the at-risk count against the whole community.

Prediction accuracy metrics (true positives, false positives, lead time, save rate) let the admin calibrate trust in the system over time. Intervention effectiveness ranking shows which outreach methods work best, grounded in actual outcome data. Churn trigger analysis breaks departures into three categories: conflict-preceded (31%), social isolation (44%), and natural fade (25%), with the 3-connection threshold validation prominently displayed.

### Tooltips

| Element | Hover text |
|---|---|
| Churn risk score | "0–100 prediction of how likely a member is to leave. Based on 5 signals: declining messages (30%), narrowing channels (25%), lost reciprocity (20%), negative sentiment (15%), growing visit gaps (10%)." |
| Critical (80+) | "Very high risk — estimated departure in 7–14 days without intervention. These need immediate personal outreach." |
| Declining (50–79) | "Clear disengagement over the past 2–4 weeks. Est. departure in 30–60 days. Early intervention has the best success at this stage." |
| Watch (30–49) | "Early warning signals only. Not urgent, but worth monitoring. Many stabilize on their own." |
| Save rate | "Percentage of flagged members who re-engaged. Personal DMs from a mod: 62%. Event invites: 45%. No intervention: 8%." |
| Lead time | "Average days between first flag and actual departure. Currently ~38 days — that's your intervention window." |
| Conflict-preceded churn | "Percentage of departures with a mod action within 14 days before leaving. At 31%, post-conflict follow-up DMs can reduce this." |
| Social isolation churn | "Percentage who left with fewer than 3 reciprocal ties. At 44%, this is the biggest category. Buddy programs address this." |

---

## Tile 10: Moderator workload

### What it measures

How moderation work is distributed across the team, how quickly incidents are addressed, and whether any moderator is approaching burnout.

### Why it matters

Moderation quality degrades when moderators burn out, and burnout is driven by volume, concentration, and sustained coverage demands. A mod handling 44% of all actions while covering 18 hours a day is on an unsustainable trajectory — even if response times look fine today, the quality of their responses (measured by declining response length and tone) will degrade, and eventually they'll step back entirely, leaving a coverage crater.

The workload Gini coefficient (the same inequality measure used for member participation, applied to moderator actions) captures this concentration risk in a single number.

### What healthy looks like

Median response time under 5 minutes. Workload Gini below 0.45 (no single mod carrying more than 30% of actions). Average actions per mod per day under 8 for sustainability. Burnout risk scores below 50% for all moderators. Escalation rate (warnings that escalate to timeouts) under 25%. Recidivism rate (warned members who re-offend within 14 days) under 15%.

### Tile view

Show median response time as the big number with a target badge. Include per-mod action bars showing the distribution visually. Companion cards for workload Gini and total actions (7d).

### Full-page view

Moderator profile cards for each mod showing their action count, response time, actions/day pace, active hours per day, and a composite burnout risk score. The burnout model weighs action volume (30%), coverage hours (25%), declining response quality measured by shortening response lengths (25%), and concentration of workload (20%).

The response time distribution chart shows both the median (solid line) and the 95th percentile (dashed/filled). The median might be 3 minutes, but if the P95 spikes to 15 on weekends, that reveals specific unmoderated windows — connecting back to the activity heatmap's coverage gaps.

Action type breakdown shows the enforcement pyramid: most issues resolve as message deletes or verbal warnings, fewer escalate to timeouts, even fewer to mutes, kicks, or bans. A healthy pyramid is wide at the base and narrow at the top. If kicks and bans are disproportionately large, the community may have a rules communication problem.

Repeat offender tracking, escalation rates, and recidivism rates complete the picture. The recommendation to follow up warnings with personal DMs connects to the churn tile's finding that personal DMs have a 62% re-engagement rate — applicable to moderated members too, not just drifting ones.

### Tooltips

| Element | Hover text |
|---|---|
| Median response time | "Middle value of all mod response times. Median is used instead of average because a few slow overnight responses would skew the average. Target: under 5 minutes." |
| 95th percentile | "The time 95% of incidents are resolved faster than. If median is 3 min but P95 is 15 min, 5% of incidents take over 15 minutes — usually during unmoderated hours." |
| Workload Gini | "Same inequality measure as participation Gini, applied to mod actions. 0.61 means one or two mods carry most of the load. Target: below 0.45." |
| Burnout risk | "0–100% composite: volume vs team (30%), daily coverage hours (25%), declining response quality (25%), workload concentration (20%). Above 70% = high risk." |
| Escalation rate | "Percentage of cases that escalate from warning to timeout or stronger. Under 25% means most issues resolve at first intervention." |
| Recidivism rate | "Percentage of warned members who re-offend within 14 days. Above 15% suggests warnings alone aren't changing behavior." |
| Actions per mod per day | "Below 8 is sustainable. Above 12 sustained correlates with burnout and declining moderation quality." |

---

## Tile 11: Incident detection

### What it measures

Real-time anomaly detection across six signal types, with an incident log, post-incident analysis, and detection accuracy tracking.

### Why it matters

This is the only real-time tile on the dashboard. Everything else looks backward (trends) or forward (predictions). This one looks at right now. When a raid hits at 2am, when a conversation derails at 11pm, when a coordinated harassment campaign starts — the system needs to detect it within seconds and alert the on-call moderator.

The six signals come directly from academic research. The most important insight from the UAlbany/Rutgers study: message timing is often more predictive than message content. Rapidly arriving messages with even mild negative cues reliably predict escalation. And from the conversational derailment research: bitter frustration appears approximately 3 comments before overt toxicity.

### What healthy looks like

Zero active incidents most of the time. Average detection time under 60 seconds (automated). Average resolution time under 5 minutes (human response). False positive rate under 20% — some false alarms are acceptable (better to check than miss), but too many cause alert fatigue.

### Tile view

Show the active incident count with a status badge (green "clear" when zero, red "active" when incidents are live). Include a 7-day timeline with resolved incident dots colored by type, and four alert category indicators (velocity spikes, report clusters, raid attempts, sentiment storms) with green/red status dots.

### Full-page view

The real-time velocity monitor shows a rolling 6-hour window of messages per 5-minute window with a dynamically computed 2-sigma alert threshold. When the line crosses the threshold, it changes color. This is the heartbeat monitor.

The incident log table documents all incidents over 30 days: date, type (with a colored type indicator), channel, what triggered it, how long it took to resolve, and the outcome.

The six anomaly detection signal definitions are displayed as cards with their threshold, detection window, and a plain-language description of what they detect and why. Two of them (sentiment storm and thread depth anomaly) are marked as "tuning" because they currently produce more false positives than the others.

Post-incident analysis cards provide narrative summaries of recent incidents — what happened, how it was detected, what action was taken, and what was learned.

Detection performance metrics close the feedback loop: true positives, false positives, missed incidents, and precision rate over a 90-day lookback. An incident timing pattern card shows that most incidents cluster during weekend evenings — connecting to the heatmap's mod coverage gaps.

### Tooltips

| Element | Hover text |
|---|---|
| Velocity spike | "Fires when message volume exceeds 2 standard deviations above its rolling 30-minute average within a 5-minute window. Most spikes are benign (game events, exciting news), some indicate conflicts or raids." |
| New account clustering | "Fires when 3+ accounts under 7 days old join within 2 minutes. The primary raid detection signal. Combined with default avatars and immediate posting, it's highly reliable." |
| Report clustering | "Fires when 2+ independent reports target the same user or channel within 5 minutes. Independent reports converging almost always indicate a real problem." |
| Sentiment storm | "Based on the UAlbany/Rutgers Comment Storm Severity model. Fires when channel sentiment stays below -0.3 for 5+ minutes. Research found comment TIMING is more predictive than actual words." |
| Thread depth anomaly | "Fires when 15+ rapid replies occur between only 2–3 users in a quiet channel. Research on conversational derailment found this pattern precedes overt toxicity by ~3 messages." |
| False positive rate | "Percentage of alerts that turn out to be benign. Some false positives are acceptable — better to check a false alarm than miss a real problem. Above 25% causes alert fatigue." |
| Detection precision | "True positives / (true positives + false positives). 86% means 86% of alerts are real incidents." |

---

## Tile 12: Composite health score

### What it measures

A single 0–100 number that rolls up all six health dimensions into one answer to "how is my community doing?"

### Why it matters

The admin needs a headline. Six dimensions with multiple sub-metrics each creates information overload when you just want to check in. The composite score provides the glanceable answer, and the dimension breakdown tells you where to dig deeper. The recommendations section closes the loop by connecting the weakest sub-metrics to specific actions.

### What healthy looks like

0–39 = Critical (immediate intervention needed). 40–59 = Needs work. 60–79 = Good (healthy with room to improve). 80–100 = Excellent (maintain current practices).

### Why a flower chart instead of a radar chart

The research found that radar charts are the least effective visualization for composite indicators. They distort area perception — dimensions at the top of the chart appear more prominent than those at the bottom, and the enclosed area doesn't scale linearly with the underlying values. A flower/polar chart uses petal LENGTH as the encoding channel instead of enclosed area, giving equal visual weight to all dimensions.

### Tile view

A half-circle gauge with the 0–100 score, color-coded badge, and 30-day sparkline. Six dimension mini-bars below showing individual dimension scores — immediately surfacing which are strong and which are dragging the composite down.

### Full-page view

The flower chart is the visual centerpiece: six petals representing the dimensions, petal length proportional to score, current snapshot overlaid on a faded 30-day-ago ghost so improvement direction is visible at a glance.

The 90-day dimension trend shows the composite line (thick, prominent) with all six dimension lines (thin, dashed) behind it. This reveals which dimensions drive composite movement — and which are lagging.

The full dimension breakdown expands each dimension into its 2–3 sub-metrics with individual score bars, weight labels, and 30-day deltas. This is where the admin traces from "health score is 72" down to "because our Gini is 0.73 and our D30 retention is only 60%."

The period comparison table (now vs 30d vs 90d for each dimension) gives the tabular trajectory view.

The recommendations section is the most important part. Each card connects a specific weak sub-metric to a concrete intervention with an estimated composite score impact. Recommendations are ranked by impact. The estimates are computed from the weighted formula — improving a sub-metric in a 20%-weighted dimension has a different composite effect than the same improvement in a 15%-weighted dimension.

### Tooltips

| Element | Hover text |
|---|---|
| Community health score | "A single 0–100 number combining all six dimensions: activity (20%), engagement (20%), distribution (15%), network (15%), retention (15%), sentiment (15%). Answers 'how is my community doing?' at a glance." |
| Dimension score | "Each dimension's own 0–100 score. Shows WHERE the community is strong or weak. The composite tells you overall; dimension scores tell you where to focus." |
| Flower chart | "Each petal = one dimension, petal length = score. Used instead of radar charts because research showed radar charts distort perception — top dimensions look more important than bottom ones." |
| Score interpretation | "0–39 = Critical (act now). 40–59 = Needs work. 60–79 = Good. 80–100 = Excellent." |
| Estimated impact | "Predicted change to composite score if a recommendation is implemented. Calculated from the weighted formula plus estimated second-order effects." |

---

## User-facing tier design principles

The user dashboard follows specific gamification research to motivate without creating toxic competition. Several existing metrics feed the user tier directly, reframed for individual motivation rather than admin analysis.

**Private by default.** Only show personal stats unless the member opts into public visibility. Nobody should be surprised to find their activity data displayed to others.

**Impact over volume.** "Your messages got 48 reactions this week" is motivating. "You sent 127 messages" is not — it rewards spam, not quality. The existing "conversation starters" metric is a perfect impact metric for the user tier: "You started 3 conversations this week that got 28 total replies" shows influence, not just noise.

**Reframe XP as progress.** The existing XP system should be displayed as progress toward the next level or toward a weekly goal, not as a raw count. Show the XP bar filling, not the XP number growing. If the XP formula weights reactions, voice time, and thread participation alongside messages, surface that: "12 XP from messages, 8 XP from reactions received, 5 XP from voice" teaches members that diverse participation is valued.

**Unique people talked to as personal growth.** The existing "number of people talked to" ranking is valuable, but frame it as personal progress ("you talked to 12 people this week, up from 8 last week") rather than a competitive ranking ("you're #47 of 156"). Individual leaderboards on social metrics can make lower-ranked members feel excluded.

**Team competition over individual ranking.** Small teams competing together fosters cooperation within and healthy competition between. Individual leaderboards risk implying zero-sum dynamics — research found they satisfy competence needs for top performers but actively harm motivation for everyone else.

**Time-bounded resets.** Weekly or monthly leaderboard resets prevent permanent hierarchies that discourage newcomers from even trying.

**Multiple recognition categories.** Most helpful, most creative, best newcomer mentor, best game-night energy, top conversation starter — not just "most messages." This ensures different contribution styles are valued.

**Community milestones over individual stats.** Collective progress bars ("Community goal: 10,000 messages this month — we're at 7,200!") build shared identity without individual pressure.

**Moderate gamification.** Research found a U-shaped relationship — moderate gamification helps, but excessive gamification causes exhaustion. 25% of participants in one study reported increased stress from competitive elements. Less is more.
