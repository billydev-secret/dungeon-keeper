import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "rushmore", gameName: "Mt. Rushmore Draft", gameIcon: "🗿", hasBank: true,
    optSchema: [
      { key: "min_players", label: "Minimum Players", type: "number", default: 3, min: 2, max: 25,
        hint: "The draft won't start until this many people have joined." },
      { key: "max_players", label: "Maximum Players", type: "number", default: 25, min: 2, max: 25,
        hint: "Latecomers are turned away once the lobby is this full. The vote is a Discord dropdown, which holds 25 options at most." },
      { key: "timer", label: "Seconds to Make a Pick", type: "number", default: 30, min: 10, max: 120 },
      { key: "vote_timer", label: "Seconds to Vote", type: "number", default: 30, min: 10, max: 60 },
      { key: "mode", label: "Draft Mode", type: "select", default: "snake",
        choices: [
          { value: "snake", label: "Snake draft — one pick at a time" },
          { value: "blitz", label: "Blitz — everyone picks at once" },
        ],
        hint: "Used when the host (or a schedule) doesn't choose a mode for the round." },
    ],
  });
}
