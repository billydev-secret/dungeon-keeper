import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "wyr", gameName: "Would You Rather", gameIcon: "🤔", hasBank: true,
    bankHint: "Each question is <strong>two options separated by a bar</strong> — <code>fly | be invisible</code> — shown as 🅰️ and 🅱️. A question typed as prose (<em>Would you rather X, or Y?</em>) is split on its last <em>or</em> and stored in that shape; anything the bot can't split into two options is refused. The reserved <strong>nsfw</strong> tag marks adult content, served only in age-restricted channels.",
    optSchema: [
      { key: "round_seconds", label: "Seconds per Round", type: "number", default: 0, min: 0, max: 300,
        hint: "A round advances itself after this many seconds; the host can still press Next early. 0 leaves pacing to the host. A host can override this per game with the round_seconds option." },
      { key: "max_rounds", label: "Rounds per Game", type: "number", default: 10, min: 0, max: 50,
        hint: "The game posts its recap and pays the room after this many rounds. 0 runs until the host presses End Game. A host can override this per game with the rounds option." },
    ],
  });
}
