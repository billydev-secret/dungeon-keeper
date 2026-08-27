import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "wyr", gameName: "Would You Rather", gameIcon: "🤔", hasBank: true,
  });
}
