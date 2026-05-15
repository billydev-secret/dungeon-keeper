import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "price", gameName: "Name Your Price", gameIcon: "💰", hasBank: true,
    optSchema: [
      { key: "min_players", label: "Min players", type: "number", default: 2, min: 2, max: 50 },
      { key: "max_players", label: "Max players (0 = unlimited)", type: "number", default: 0, min: 0, max: 200 },
      { key: "total_rounds", label: "Default rounds", type: "number", default: 5, min: 1, max: 30 },
    ],
  });
}
