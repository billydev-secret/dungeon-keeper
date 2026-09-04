import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "mlt", gameName: "Most Likely To", gameIcon: "👑", hasBank: true,
    optSchema: [
      { key: "min_players", label: "Minimum Players", type: "number", default: 3, min: 2, max: 50,
        hint: "A round won't start until this many people have joined." },
      { key: "max_players", label: "Maximum Players", type: "number", default: 0, min: 0, max: 200,
        hint: "Latecomers are turned away once the round is this full. Set 0 for no limit." },
      { key: "round_seconds", label: "Seconds per Round", type: "number", default: 0, min: 0, max: 300,
        hint: "A round advances itself after this many seconds; the host can still press Next early. 0 leaves pacing to the host. A host can override this per game with the round_seconds option." },
      { key: "max_rounds", label: "Rounds per Game", type: "number", default: 10, min: 0, max: 50,
        hint: "The game posts its recap and pays the room after this many rounds. 0 runs until the host presses End Game. A host can override this per game with the rounds option." },
    ],
  });
}
