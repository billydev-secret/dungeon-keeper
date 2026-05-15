import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "clapback", gameName: "Clapback", gameIcon: "⚔️", hasBank: true,
    optSchema: [
      { key: "min_players", label: "Min players", type: "number", default: 3, min: 2, max: 50 },
      { key: "max_players", label: "Max players", type: "number", default: 16, min: 2, max: 50 },
    ],
  });
}
