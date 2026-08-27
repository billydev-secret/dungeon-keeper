import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "nhie", gameName: "Never Have I Ever", gameIcon: "⛔", hasBank: true,
  });
}
