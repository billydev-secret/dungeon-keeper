import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "nhie", gameName: "Never Have I Ever", gameIcon: "⛔", hasBank: true,
    optSchema: [
      { key: "lives", label: "Lives Per Player", type: "number", default: 3, min: 0, max: 20,
        hint: "Players are out once they run out of lives. Set 0 to let everyone play to the end." },
      { key: "min_players", label: "Minimum Players", type: "number", default: 3, min: 2, max: 50,
        hint: "A round won't start until this many people have joined." },
      { key: "max_players", label: "Maximum Players", type: "number", default: 0, min: 0, max: 200,
        hint: "Latecomers are turned away once the round is this full. Set 0 for no limit." },
    ],
  });
}
