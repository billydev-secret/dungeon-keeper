import { mountGamePanel } from "./games-panel-shared.js";

// Traditional Truth or Dare. Every bank question carries exactly one of the
// four categories (enforced by the required dropdown + the server), so the
// in-game "Ask Question" and "Bank Round" buttons can hand each player a
// question in a category they opted into.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "traditional", gameName: "Traditional Truth or Dare", gameIcon: "🎲", hasBank: true,
    bankHint: "Every question must be filed under one of the four categories below — the in-game <strong>Ask Question</strong> button pre-fills the host's box with a bank question in a category the chosen player opted into (the host can edit or replace it), and <strong>Bank Round</strong> deals everyone one at once. <strong>NSFW</strong> questions only reach players who picked an NSFW category.",
    bankCategories: [
      { value: "sfw_truth", label: "SFW Truth" },
      { value: "sfw_dare", label: "SFW Dare" },
      { value: "nsfw_truth", label: "NSFW Truth" },
      { value: "nsfw_dare", label: "NSFW Dare" },
    ],
    optSchema: [
      { key: "idle_minutes", label: "Quiet Minutes Before Auto-Close", type: "number", default: 20, min: 0, max: 1440,
        hint: "A game with nothing pressed for this long ends itself: the recap is posted and everyone who opted in is paid, the same as the host pressing End Game. 0 leaves the game open until the host ends it or the 24-hour sweep does." },
    ],
  });
}
