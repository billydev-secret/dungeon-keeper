import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "risky_roller", gameName: "Risky Roller", gameIcon: "🎲", hasBank: false,
    optSchema: [
      { key: "auto_close_minutes", label: "Auto-close after (minutes, 0 = off)", type: "number", default: 0, min: 0, max: 120 },
      { key: "min_game_seconds", label: "Minimum game time (seconds, 0 = off)", type: "number", default: 0, min: 0, max: 300 },
      { key: "max_games_per_channel", label: "Max concurrent games per channel", type: "number", default: 1, min: 1, max: 5 },
    ],
  });
}
