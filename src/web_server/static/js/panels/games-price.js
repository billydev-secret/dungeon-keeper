import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "price", gameName: "Name Your Price", gameIcon: "💰", hasBank: true,
    optSchema: [
      { key: "min_players", label: "Min players", type: "number", default: 2, min: 2, max: 50 },
      { key: "max_players", label: "Max players (0 = unlimited)", type: "number", default: 0, min: 0, max: 200 },
      { key: "rounds", label: "Rounds", type: "number", default: 5, min: 1, max: 20 },
      { key: "timer", label: "Price timer (seconds)", type: "number", default: 30, min: 10, max: 120 },
      { key: "vote_timer", label: "Vote timer (seconds)", type: "number", default: 20, min: 10, max: 60 },
    ],
  });
}
