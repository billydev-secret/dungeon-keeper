import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "wyr", gameName: "Would You Rather", gameIcon: "🤔", hasBank: true,
    optSchema: [
      { key: "anonymous", label: "Hide Who Voted for What", type: "bool", default: true,
        hint: "When on, only the vote totals are shown — never who picked which side." },
      { key: "min_players", label: "Minimum Players", type: "number", default: 2, min: 2, max: 50,
        hint: "A round won't start until this many people have joined." },
      { key: "max_players", label: "Maximum Players", type: "number", default: 0, min: 0, max: 200,
        hint: "Latecomers are turned away once the round is this full. Set 0 for no limit." },
    ],
  });
}
