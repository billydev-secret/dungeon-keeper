import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "mlt", gameName: "Most Likely To", gameIcon: "👑", hasBank: true,
    optSchema: [
      { key: "min_players", label: "Minimum Players", type: "number", default: 3, min: 2, max: 50,
        hint: "A round won't start until this many people have joined." },
      { key: "max_players", label: "Maximum Players", type: "number", default: 0, min: 0, max: 200,
        hint: "Latecomers are turned away once the round is this full. Set 0 for no limit." },
    ],
  });
}
