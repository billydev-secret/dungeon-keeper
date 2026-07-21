# Dungeon Keeper — Role Menus Product Spec (v1)

**Status:** Built — the cog and the `role_menus/` package implement all five modes
(toggle / unique / verify / drop / binding), with the dashboard surface in
`routes/role_menus.py` + `panels/role-menus.js`. As-built deltas from this spec
are recorded in `docs/plans/role-menus.md`.
**Owner:** Ben
**One-liner:** Members grant and remove roles on themselves by clicking buttons or picking from a dropdown on a DK-posted embed. Admins build, preview, and manage everything from the dashboard Role Menus panel — no commands, no reactions.

---

## 1. Why

Self-service roles are table stakes for any community server: color roles, pronoun roles, ping opt-ins, interest channels, verification gates. Today this either doesn't exist in DK or requires manual mod work. Reaction-based systems (the old standard) fail silently, confuse mobile users, and look dated. Component-based menus give members instant private feedback and give admins a clean, visual way to shape how the community self-organizes — which is very Hearth: people choosing their own corner of the meadow without asking permission.

**Goals**

- Members can self-assign roles in one click/tap with clear confirmation, zero learning curve.
- Admins can build and maintain menus entirely from the web, with a live preview, without ever touching a command.
- The system quietly enforces community rules (one color at a time, verified-only menus, permanent choices) so mods don't have to.

**Non-goals (v1)**

- Reaction-based menus (explicitly excluded)
- Paid/Petal-priced roles (parked for the economy launch)
- Temporary/expiring roles

---

## 2. The Two Experiences

### 2.1 Member experience (in Discord)

A member sees a DK embed in a channel — title, description, optional thumbnail and accent color, matching the existing docs-page look. Below it, either:

- **Buttons** — up to 25 labeled buttons, each optionally with an emoji and one of four colors (gray, blurple, green, red). One tap toggles or grants the role depending on the menu's rules.
- **A dropdown** — one select menu with up to 25 options, each with a label, optional emoji, and optional short description. The member opens it, checks/unchecks what they want, and their roles update to match their selection.

Every interaction gets an **immediate private (ephemeral) response**: "✅ You now have @Night Owl," "✅ Updated your colors: +Oxblood, −Forest," "❌ This menu requires the @Verified role." No public noise, no silent failures, no "is the bot broken?" tickets.

**Guardrails members can hit (and what they see):**

| Situation | Member sees |
|---|---|
| Menu requires a role they don't have | "This menu requires the @X role." |
| At the max-roles cap for the menu | "You can hold at most N roles from this menu — remove one first." |
| Clicking too fast (menu has a cooldown) | "Slow down — try again in a few seconds." |
| Trying to change a permanent choice | "Your choice here is permanent." |
| Something genuinely broke | A polite apology; mods are alerted automatically. |

### 2.2 Admin experience (dashboard Role Menus panel)

A **Role Menus** page in the dashboard, gated to the existing mod/admin tier.

**List view.** Every menu for the guild at a glance: title, channel (or *Draft*), button-vs-dropdown badge, mode badge, number of roles, an on/off toggle, and last-edited info. "New Menu" button to start fresh.

**Editor.** Two panes:

- **Left — the form.** Everything about the menu in one place (detailed in §3).
- **Right — live preview.** A Discord-styled render of the embed and its buttons/dropdown that updates as you type. What you see is exactly what publishes. Same visual language as the docs-page embed builder.

**Publish bar.** Pick a channel and hit **Publish**. After that: **Update live message** (pushes edits to the existing post in place — no delete/repost churn), **Unpublish** (turns the menu off but leaves the post as a visual), or **Delete** (removes post and menu, with a confirm step).

Drafts are first-class: build the whole booster color menu from the couch, publish when it's ready.

---

## 3. Configuring a Menu

### 3.1 Menu-level settings

| Setting | What it does |
|---|---|
| **Title / Description / Color / Thumbnail** | The embed itself. Description supports markdown. |
| **Style: Buttons or Dropdown** | How the menu renders. Switchable any time — same roles, different presentation. Buttons suit small, high-visibility sets; the dropdown suits big lists (pronouns, 20 colors) and keeps the channel visually quiet. |
| **Mode** | The behavioral rules — see §3.3. |
| **Max roles** | Optional cap on how many roles from this menu one member can hold. |
| **Required role** | Gate the whole menu (booster-only cosmetics, verified-only opt-ins). |
| **Cooldown** | Optional per-member rate limit to stop rapid-fire toggling. |
| **Dropdown placeholder** | The hint text in a collapsed dropdown ("Pick your colors…"). Dropdown style only. |

### 3.2 The role list (pairs)

A sortable list of rows, each defining one choice:

- **Emoji** (optional) — typed in or picked from the guild's emoji via a picker
- **Label** (required) — the button text / dropdown option name
- **Role** — chosen from a dropdown that **only shows roles DK can actually manage**. Dangerous roles (admin, manage-server, etc.) are hidden unless an explicit "allow elevated roles" override is checked, which is logged loudly. This makes misconfiguration nearly impossible rather than merely handled.
- **Button color** (button style only) or **Description** (dropdown style only, short)
- Drag to reorder; order controls layout.

Limit: **25 choices per menu** (platform ceiling). The editor counts down and blocks past the cap. Need more? Make a second menu.

### 3.3 Modes

Modes are the product's behavioral vocabulary — each maps to a real community pattern:

| Mode | Behavior | Use it for |
|---|---|---|
| **Toggle** (default) | Click to get the role, click again to drop it. | Ping opt-ins, interest channels. |
| **Unique** | Picking one automatically drops any other role from this menu. Only ever one at a time. | Color roles, team picks, "choose your region." |
| **Verify** | Roles can only be gained here, never removed here. | Verification gates, rules-acknowledged roles. |
| **Drop** | Roles can only be removed here, never gained. | Opt-out stations, "remove my pings" menus. |
| **Binding** | First choice is permanent — one pick, ever, per member. | One-time allegiance picks, event factions, anything with dramatic weight. |

With the dropdown style, Unique naturally limits selection to one option, and a member's submitted selection simply *becomes* their set of roles from that menu — check and uncheck freely, roles follow.

---

## 4. Trust, Safety & Visibility

- **Nothing public, ever, from a member's click.** All feedback is private to the member.
- **Mod-log integration.** Every role grant/removal flows into the existing mod-log stream in compact form (`🎭 @user +Night Owl −Early Bird (Colors)`), so Rules Watch and mods see role churn alongside everything else.
- **Config audit.** Every admin action in the panel (edits, publishes, deletes, elevated-role overrides) is logged with who and what changed.
- **Graceful degradation.** If a role gets deleted or permissions shift after publish, members get a polite failure message, and mods get **one** alert — not one per click. The panel flags affected menus with a clear fix path ("role missing," "message missing — republish?").
- **Self-healing.** After downtime or restarts, menus just keep working; the panel surfaces anything that needs human attention rather than silently breaking.

---

## 5. Decided Edge Behaviors

- **Same role in two menus:** fine. Unique mode only polices its own menu.
- **Lowering max-roles after people are over the new cap:** existing holders keep theirs; the cap applies to new grants. The panel notes this.
- **Editing a menu someone is mid-interaction with:** their action applies to whatever still exists; removed options are ignored gracefully.
- **Unpublish vs Delete:** Unpublish = menu off, post remains as decor. Delete = post and menu gone. History of who got what is kept either way.
- **Permanent (Binding) menus:** the editor constrains the dropdown style to single-pick, because "permanent multi-select" is a confusing promise.

---

## 6. Success Criteria

- A brand-new member can self-assign a role with zero instruction and know it worked.
- Ben can build, preview, and publish a complete color-role menu from the dashboard Role Menus panel in under five minutes without opening Discord.
- Zero "the role thing is broken" tickets attributable to silent failures.
- Mods can answer "who picked what, when" from existing logs without SQL spelunking.

## 7. Later (parked)

- Petal-priced roles via the perk shop (design the menu UI to leave room for a price tag on a choice)
- Roles that expire after a set time
- Menu templates / cloning
- An adoption analytics card on the dashboard (which choices are popular, over time)
- Bigger-than-25 menus via multiple components
