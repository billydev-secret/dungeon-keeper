import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "rushmore", gameName: "Mt. Rushmore Draft", gameIcon: "🗿", hasBank: true,
    optSchema: [
      { key: "min_players", label: "Minimum Players", type: "number", default: 2, min: 2, max: 50,
        hint: "A round won't start until this many people have joined." },
      { key: "max_players", label: "Maximum Players", type: "number", default: 0, min: 0, max: 200,
        hint: "Latecomers are turned away once the round is this full. Set 0 for no limit." },
      { key: "draft_rounds", label: "Draft Rounds", type: "number", default: 4, min: 1, max: 20,
        hint: "How many picks each player makes before the vote." },
      { key: "timer", label: "Seconds to Make a Pick", type: "number", default: 30, min: 10, max: 120 },
      { key: "vote_timer", label: "Seconds to Vote", type: "number", default: 30, min: 10, max: 60 },
    ],
  });
}
