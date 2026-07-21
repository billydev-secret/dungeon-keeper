import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "clapback", gameName: "Clapback", gameIcon: "⚔️", hasBank: true,
    optSchema: [
      { key: "rounds", label: "Rounds", type: "number", default: 5, min: 1, max: 15 },
      { key: "timer", label: "Answer timer (seconds)", type: "number", default: 120, min: 15, max: 180 },
      { key: "vote_timer", label: "Vote timer (seconds)", type: "number", default: 40, min: 10, max: 60 },
      { key: "anonymous", label: "Hide authors until recap", type: "bool", default: false },
      { key: "tags", label: "Prompt tags (comma-separated, optional)", type: "text", default: "" },
      { key: "allow_nsfw", label: "Include NSFW prompts", type: "bool", default: true },
    ],
  });
}
