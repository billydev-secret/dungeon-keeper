export function mount(container) {
  container.replaceChildren();

  const panel = document.createElement("div");
  panel.className = "panel";
  panel.style.cssText = "display:flex;flex-direction:column;height:100%;";

  const header = document.createElement("header");
  header.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:1rem;";

  const titleBox = document.createElement("div");
  const h2 = document.createElement("h2");
  h2.textContent = "Help & Reference";
  const subtitle = document.createElement("div");
  subtitle.className = "subtitle";
  subtitle.textContent = "All commands and switches, organised by functional block.";
  titleBox.appendChild(h2);
  titleBox.appendChild(subtitle);

  const openLink = document.createElement("a");
  openLink.className = "btn";
  openLink.href = "/static/manual.html";
  openLink.target = "_blank";
  openLink.rel = "noopener";
  openLink.style.whiteSpace = "nowrap";
  openLink.textContent = "Open in new tab ↗";

  header.appendChild(titleBox);
  header.appendChild(openLink);

  const iframe = document.createElement("iframe");
  iframe.src = "/static/manual.html";
  iframe.title = "DungeonKeeper Reference Guide";
  iframe.style.cssText =
    "flex:1;min-height:0;width:100%;border:1px solid var(--border,#d0d7de);border-radius:6px;background:#fff;";

  panel.appendChild(header);
  panel.appendChild(iframe);
  container.appendChild(panel);
}
