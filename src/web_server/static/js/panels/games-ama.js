import { mountGamePanel } from "./games-panel-shared.js";

// No question bank here, deliberately. Every AMA question is typed by a member
// during the game and asked anonymously; nothing in games_ama_cog ever reads
// games_question_bank, so the bank UI this page used to show curated rows the
// game could never serve.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ama", gameName: "Anonymous AMA", gameIcon: "🎙️", hasBank: false,
    intro: "Questions come from the room: members ask the guest anonymously while the AMA is running, so there's nothing to curate here in advance.",
    optSchema: [
      { key: "hot_seat_ping_role_id", label: "Hot-Seat Ping Role", type: "role",
        hint: "Mentioned in the channel each time someone new takes the hot seat. Leave it at (none) and only members who tapped Notify Me are pinged." },
      { key: "questions_per_turn", label: "Questions per Turn", type: "number", default: 4, min: 1, max: 20,
        hint: "How many questions the hot seat answers (or passes) before the seat rotates. A two- or three-person AMA usually wants more than the default." },
    ],
  });
}
