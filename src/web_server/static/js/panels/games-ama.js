import { mountGamePanel } from "./games-panel-shared.js";

// No question bank here, deliberately. Every AMA question is typed by a member
// during the game and asked anonymously; nothing in games_ama_cog ever reads
// games_question_bank, so the bank UI this page used to show curated rows the
// game could never serve.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ama", gameName: "Anonymous AMA", gameIcon: "🎙️", hasBank: false,
    intro: "Questions come from the room: members ask the guest anonymously while the AMA is running, so there's nothing to curate here in advance.",
  });
}
