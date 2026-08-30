import { mountGamePanel } from "./games-panel-shared.js";

// No question bank here, deliberately. Every AMA question is typed by a member
// during the game and asked anonymously; no draw function has ever read an AMA
// bank, so the bank UI this page used to show curated questions that could
// never be asked.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ama", gameName: "Anonymous AMA", gameIcon: "🎙️",
    intro: "Questions come from the room: members ask the guest anonymously while the AMA is running, so there's nothing to curate here in advance.",
  });
}
