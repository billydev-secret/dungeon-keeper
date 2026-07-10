import { mountGamePanel } from "./games-panel-shared.js";
export function mount(container) {
  mountGamePanel(container, {
    gameType: "pen_pals", gameName: "Pen Pals", gameIcon: "🖊️", hasBank: true, hasStatus: false,
    bankHint: "Conversation starters posted in pen pal channels. Tag adult prompts <strong>nsfw</strong> — they are only served when the guild's Pen Pals config includes NSFW, and pen pal channels are then created age-restricted. Enable/disable Pen Pals itself in its Config panel.",
  });
}
