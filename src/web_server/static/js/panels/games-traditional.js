import { mountGamePanel } from "./games-panel-shared.js";

// Traditional Truth or Dare. Every bank question carries exactly one of the
// four categories (enforced by the required dropdown + the server), so the
// in-game "Bank Round" button can hand each player a question in a category
// they opted into.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "traditional", gameName: "Traditional Truth or Dare", gameIcon: "🎲", hasBank: true,
    bankHint: "Every question must be filed under one of the four categories below — the in-game <strong>Bank Round</strong> button serves each player a question from a category they opted into. <strong>NSFW</strong> questions only reach players who picked an NSFW category.",
    bankCategories: [
      { value: "sfw_truth", label: "SFW Truth" },
      { value: "sfw_dare", label: "SFW Dare" },
      { value: "nsfw_truth", label: "NSFW Truth" },
      { value: "nsfw_dare", label: "NSFW Dare" },
    ],
  });
}
