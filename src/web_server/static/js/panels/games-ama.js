import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ama", gameName: "Anonymous AMA", gameIcon: "🎙️", hasBank: true,
    optSchema: [
      { key: "screened", label: "Questions screened by host before posting", type: "bool", default: true },
      { key: "min_players", label: "Min players", type: "number", default: 2, min: 2, max: 50 },
      { key: "max_players", label: "Max players (0 = unlimited)", type: "number", default: 0, min: 0, max: 200 },
    ],
  });
}
