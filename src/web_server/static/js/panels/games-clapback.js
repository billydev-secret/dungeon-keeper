import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "clapback", gameName: "Clapback", gameIcon: "⚔️", hasBank: true,
    optSchema: [
      { key: "rounds", label: "Rounds Per Game", type: "number", default: 5, min: 1, max: 15 },
      { key: "timer", label: "Seconds to Write an Answer", type: "number", default: 120, min: 15, max: 180 },
      { key: "vote_timer", label: "Seconds to Vote", type: "number", default: 40, min: 10, max: 60 },
      { key: "anonymous", label: "Hide Authors Until the Recap", type: "bool", default: false,
        hint: "When on, answers are voted on blind and names are revealed only at the end." },
      { key: "tags", label: "Only Use Prompts Tagged", type: "text", default: "",
        placeholder: "e.g. spicy, holiday",
        hint: "Comma-separated. Leave blank to draw from the whole bank." },
      { key: "allow_nsfw", label: "Include NSFW Prompts", type: "bool", default: true,
        hint: "Prompts tagged nsfw are only ever served in age-restricted channels, even when this is on." },
    ],
  });
}
