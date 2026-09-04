import { mountGamePanel } from "./games-panel-shared.js";

// Hot Takes has no question bank — every take is typed by a member in the
// lobby — so this page is the enable switch plus the one pacing dial.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "hottakes", gameName: "Hot Takes", gameIcon: "🔥", hasBank: false,
    intro: "Takes are written by members during the game, so there is no bank to fill here. The dial below sets how long each take stays open for votes.",
    optSchema: [
      { key: "round_seconds", label: "Seconds per Take", type: "number", default: 45, min: 0, max: 300,
        hint: "Each take closes for voting after this many seconds — or as soon as everyone in the room has voted; the host can still press Next Take early. 0 leaves pacing to the host. A host can override this per game with the take_seconds option." },
    ],
  });
}
