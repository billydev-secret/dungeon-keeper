import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ffa", gameName: "FFA / Truth or Dare", gameIcon: "🎲", hasBank: true,
  });
}
