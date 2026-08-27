import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ama", gameName: "Anonymous AMA", gameIcon: "🎙️", hasBank: true,
  });
}
