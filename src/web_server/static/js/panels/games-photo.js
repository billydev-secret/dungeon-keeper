import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "photo", gameName: "Photo Challenge", gameIcon: "📸", hasBank: true,
    optSchema: [
      {
        key: "ping_role_id", label: "Ping role on post", type: "role", default: "",
        hint: "Mentioned with every challenge card (manual and scheduled). Scheduled announces can also ping — avoid setting both.",
      },
    ],
  });
}
