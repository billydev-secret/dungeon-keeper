import { mountGamePanel } from "./games-panel-shared.js";

// 'truth' and 'dare' are reserved tags here, not decoration: a host who starts
// a truth round draws only from rows tagged truth. Untagged rows are still
// valid — they simply only ever come up in a random round — so the contract is
// stated in the hint rather than enforced as a required category the way
// Traditional does it.
export function mount(container) {
  mountGamePanel(container, {
    gameType: "ffa", gameName: "FFA / Truth or Dare", gameIcon: "🎲", hasBank: true,
    bankHint: "Prompts for Free-for-All. Tag a prompt <strong>truth</strong> or <strong>dare</strong> to make it eligible for a truth or dare round — a prompt with neither tag is only ever drawn in a random round. The reserved <strong>nsfw</strong> tag marks adult content, which is served only in age-gated channels.",
  });
}
