import { mountGamePanel } from "./games-panel-shared.js";

// Fantasies & Dealbreakers has no question bank — every entry is typed by a
// member during a round — so this page is the enable switch plus the one
// pacing dial.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "fantasies", gameName: "Fantasies & Dealbreakers", gameIcon: "✨", hasBank: false,
    intro: "Entries are written by members during each round, so there is no bank to fill here. The dial below sets how long each entry stays open for votes.",
    optSchema: [
      { key: "round_seconds", label: "Seconds per Entry", type: "number", default: 45, min: 0, max: 300,
        hint: "Each entry closes for voting after this many seconds — or as soon as everyone in the room has voted; the host can still press Next early. 0 leaves pacing to the host. A host can override this per game with the entry_seconds option." },
    ],
  });
}
