// Lightweight markdown → HTML for editor previews (docs, role menus).
// Discord does the real rendering; this only needs to look close enough that
// the author can see structure. Escapes first, so raw HTML never executes.
import { esc } from "./api.js";

export function mdInline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/__([^_]+)__/g, "<b>$1</b>")
    .replace(/~~([^~]+)~~/g, "<s>$1</s>")
    // $1 is the link TEXT, $2 the URL. Putting $1 in the href sent the browser
    // to the label ("click here") AND threw away the https-only validation the
    // pattern does on $2 — a `[javascript:alert(1)](https://ok)` link became a
    // real javascript: href, stopped only by an onclick that a middle-click or
    // "open in new tab" walks straight past. The href is $2, always, and the
    // pattern still refuses to match anything but http(s).
    .replace(
      /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      (_m, text, url) => `<a href="${url}" rel="noopener noreferrer" onclick="return false">${text}</a>`,
    );
}

export function mdToHtml(text) {
  const lines = (text || "").split("\n");
  const out = [];
  let i = 0;
  let listOpen = null;
  const closeList = () => { if (listOpen) { out.push(`</${listOpen}>`); listOpen = null; } };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*(```|~~~)(.*)$/);
    if (fence) {
      closeList();
      const marker = fence[1];
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) { buf.push(lines[i]); i++; }
      i++;
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      out.push(`<div class="dp-h dp-h${level}">${mdInline(heading[2])}</div>`);
      i++;
      continue;
    }
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      closeList();
      out.push(`<blockquote>${mdInline(quote[1])}</blockquote>`);
      i++;
      continue;
    }
    const ul = line.match(/^\s*[-*]\s+(.+)$/);
    const ol = line.match(/^\s*\d+\.\s+(.+)$/);
    if (ul || ol) {
      const want = ul ? "ul" : "ol";
      if (listOpen && listOpen !== want) closeList();
      if (!listOpen) { listOpen = want; out.push(`<${want}>`); }
      out.push(`<li>${mdInline((ul || ol)[1])}</li>`);
      i++;
      continue;
    }
    if (line.trim() === "") { closeList(); out.push("<br>"); i++; continue; }
    closeList();
    out.push(`<div>${mdInline(line)}</div>`);
    i++;
  }
  closeList();
  return out.join("");
}
