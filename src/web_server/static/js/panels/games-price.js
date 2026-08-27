import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "price", gameName: "Name Your Price", gameIcon: "💰", hasBank: true,
    optSchema: [
      { key: "rounds", label: "Rounds Per Game", type: "number", default: 5, min: 1, max: 20 },
      { key: "timer", label: "Seconds to Name a Price", type: "number", default: 30, min: 10, max: 120 },
      { key: "vote_timer", label: "Seconds to Vote", type: "number", default: 20, min: 10, max: 60 },
    ],
  });
}
