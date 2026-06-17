import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "photo", gameName: "Photo Challenge", gameIcon: "📸", hasBank: true,
  });
}
