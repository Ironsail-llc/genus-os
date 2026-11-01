/**
 * A small tokenizer for code blocks in chat: comments, strings, numbers and
 * keywords, enough for the eye to find its way through a reply's snippet.
 *
 * Deliberately not shiki or highlight.js. Either would put a grammar engine
 * (and, for shiki, a WASM regex engine) on the dashboard's first load for a
 * feature that only needs the four token kinds every language shares, against
 * a bundle budget that leaves about 5% headroom. Colour comes from CSS
 * classes, so it follows the theme. The tokens always join back to the input:
 * highlighting may be wrong, but it never changes the code a reader copies.
 */

export type TokenType = "keyword" | "string" | "comment" | "number" | "plain";
export interface Token {
  type: TokenType;
  text: string;
}

interface Grammar {
  /** Line comment openers, e.g. `#` or `//`. */
  line: string[];
  /** Block comments as [open, close]. */
  block: Array<[string, string]>;
  quotes: string[];
  keywords: ReadonlySet<string>;
}

const words = (s: string) => new Set(s.split(/\s+/).filter(Boolean));

const C_LIKE = { line: ["//"], block: [["/*", "*/"]] as Array<[string, string]> };

const PYTHON: Grammar = {
  line: ["#"],
  block: [],
  quotes: ['"""', "'''", '"', "'"],
  keywords: words(
    "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield match case",
  ),
};
const SHELL: Grammar = {
  line: ["#"],
  block: [],
  quotes: ['"', "'"],
  keywords: words("if then else elif fi for while until do done case esac in function return local export echo exit"),
};
const JS: Grammar = {
  ...C_LIKE,
  quotes: ['"', "'", "`"],
  keywords: words(
    "async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof interface let new null of return static super switch this throw true try type typeof undefined var void while with yield enum implements private protected public readonly as",
  ),
};
const GO: Grammar = {
  ...C_LIKE,
  quotes: ['"', "'", "`"],
  keywords: words(
    "break case chan const continue default defer else fallthrough for func go goto if import interface map nil package range return select struct switch type var true false",
  ),
};
const RUST: Grammar = {
  ...C_LIKE,
  quotes: ['"'],
  keywords: words(
    "as async await break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while",
  ),
};
const C_FAMILY: Grammar = {
  ...C_LIKE,
  quotes: ['"', "'"],
  keywords: words(
    "auto break case catch char class const continue default delete do double else enum extends false final finally float for if implements import int interface long namespace new null private protected public return short static struct super switch this throw throws true try typedef unsigned using var virtual void volatile while",
  ),
};
const SQL: Grammar = {
  line: ["--"],
  block: [["/*", "*/"]],
  quotes: ["'", '"'],
  keywords: words(
    "select from where and or not insert into values update set delete create table index on join left right inner outer group by order having limit offset as distinct null is in like between case when then else end returning with union all SELECT FROM WHERE AND OR NOT INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE INDEX ON JOIN LEFT RIGHT INNER OUTER GROUP BY ORDER HAVING LIMIT OFFSET AS DISTINCT NULL IS IN LIKE BETWEEN CASE WHEN THEN ELSE END RETURNING WITH UNION ALL",
  ),
};
const YAML: Grammar = { line: ["#"], block: [], quotes: ['"', "'"], keywords: words("true false null yes no") };
const JSON_: Grammar = { line: [], block: [], quotes: ['"'], keywords: words("true false null") };
const PLAIN: Grammar = { line: [], block: [], quotes: ['"', "'"], keywords: new Set() };

const GRAMMARS: Record<string, Grammar> = {};
const alias = (g: Grammar, names: string) => names.split(" ").forEach((n) => (GRAMMARS[n] = g));
alias(PYTHON, "python py");
alias(SHELL, "bash sh shell zsh console");
alias(JS, "js jsx javascript ts tsx typescript mjs cjs");
alias(GO, "go golang");
alias(RUST, "rust rs");
alias(C_FAMILY, "c cpp c++ h hpp java cs csharp kotlin kt swift php scala");
alias(SQL, "sql postgres postgresql psql");
alias(YAML, "yaml yml toml");
alias(JSON_, "json jsonc");

// Sticky: matched at an index, so a long block is scanned once, not re-sliced per character.
const NUMBER = /0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
const WORD = /[A-Za-z_$][\w$]*/y;

function matchAt(re: RegExp, code: string, i: number): string | null {
  re.lastIndex = i;
  const m = re.exec(code);
  return m ? m[0] : null;
}

/** Where a string opened at `i` with quote `q` ends (exclusive). */
function stringEnd(code: string, i: number, q: string): number {
  // Single-character quotes stop at the line's end; triple and backtick
  // strings may span lines. An unterminated string runs to where it stops.
  const multiline = q.length > 1 || q === "`";
  let j = i + q.length;
  while (j < code.length) {
    if (code[j] === "\\") {
      j += 2;
      continue;
    }
    if (code.startsWith(q, j)) return j + q.length;
    if (!multiline && code[j] === "\n") return j;
    j += 1;
  }
  return code.length;
}

function commentEnd(code: string, i: number, g: Grammar): number {
  for (const opener of g.line) {
    if (code.startsWith(opener, i)) {
      const end = code.indexOf("\n", i);
      return end === -1 ? code.length : end;
    }
  }
  for (const [open, close] of g.block) {
    if (code.startsWith(open, i)) {
      const end = code.indexOf(close, i + open.length);
      return end === -1 ? code.length : end + close.length;
    }
  }
  return -1;
}

export function tokenize(code: string, language?: string): Token[] {
  const g = GRAMMARS[(language || "").toLowerCase()] ?? PLAIN;
  const tokens: Token[] = [];
  const push = (type: TokenType, text: string) => {
    const last = tokens[tokens.length - 1];
    if (type === "plain" && last?.type === "plain") last.text += text;
    else tokens.push({ type, text });
  };

  let i = 0;
  while (i < code.length) {
    const comment = commentEnd(code, i, g);
    if (comment !== -1) {
      push("comment", code.slice(i, comment));
      i = comment;
      continue;
    }
    const quote = g.quotes.find((q) => code.startsWith(q, i));
    if (quote) {
      const end = stringEnd(code, i, quote);
      push("string", code.slice(i, end));
      i = end;
      continue;
    }
    const word = matchAt(WORD, code, i);
    if (word) {
      push(g.keywords.has(word) ? "keyword" : "plain", word);
      i += word.length;
      continue;
    }
    const num = matchAt(NUMBER, code, i);
    if (num) {
      push("number", num);
      i += num.length;
      continue;
    }
    push("plain", code[i]);
    i += 1;
  }
  return tokens;
}
