import { mountGamePanel } from "./games-panel-shared.js";
// No question bank: AMA runs entirely on questions members submit during a
// round, and nothing in games_ama_cog ever reads games_question_bank. The bank
// UI that used to sit here curated rows the game could never serve.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ama", gameName: "Anonymous AMA", gameIcon: "🎙️", hasBank: false,
  });
}
