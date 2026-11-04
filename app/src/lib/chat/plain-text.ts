/**
 * A chat message's markdown as plain text, for "Copy as plain text".
 *
 * A handful of line-level rules rather than a parse: chat replies use a small
 * part of markdown, and a copy that keeps a stray `*` is a smaller harm than
 * shipping a second markdown pipeline to the browser for one button. Code in
 * fences is kept exactly; only the fences go.
 */

const FENCE = /^\s*(```|~~~)/;

function inline(line: string): string {
  return (
    line
      // images, then links: keep the alt text and the link text
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      // inline code
      .replace(/`([^`]+)`/g, "$1")
      // bold and italic, only where they wrap text (not `2 * 3` or snake_case)
      .replace(/(\*\*|__)(?=\S)(.+?)(?<=\S)\1/g, "$2")
      .replace(/(^|[^\w*])\*(?=\S)(.+?)(?<=\S)\*(?!\w)/g, "$1$2")
      .replace(/(^|[^\w_])_(?=\S)(.+?)(?<=\S)_(?!\w)/g, "$1$2")
      .replace(/~~(?=\S)(.+?)(?<=\S)~~/g, "$1")
  );
}

export function markdownToPlainText(markdown: string): string {
  const out: string[] = [];
  let inFence = false;
  for (const line of markdown.split("\n")) {
    if (FENCE.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) {
      out.push(line);
      continue;
    }
    out.push(inline(line.replace(/^\s{0,3}#{1,6}\s+/, "").replace(/^\s{0,3}>\s?/, "")));
  }
  return out.join("\n").trim();
}
