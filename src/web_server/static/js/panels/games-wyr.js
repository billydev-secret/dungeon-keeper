import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "wyr", gameName: "Would You Rather", gameIcon: "🤔", hasBank: true,
    optSchema: [
      { key: "anonymous", label: "Anonymous voting", type: "bool", default: true },
      { key: "min_players", label: "Min players", type: "number", default: 2, min: 2, max: 50 },
      { key: "max_players", label: "Max players (0 = unlimited)", type: "number", default: 0, min: 0, max: 200 },
    ],
  });
}
