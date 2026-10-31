import { describe, it, expect } from "vitest";
import { tokenize } from "../highlight";

function joined(tokens: { text: string }[]) {
  return tokens.map((t) => t.text).join("");
}

function ofType(tokens: { type: string; text: string }[], type: string) {
  return tokens.filter((t) => t.type === type).map((t) => t.text);
}

describe("tokenize", () => {
  it("never changes the text: the tokens join back to the input", () => {
    const code = 'def f(x):\n    # say hi\n    return "hi" + str(42)\n';
    expect(joined(tokenize(code, "python"))).toBe(code);
  });

  it("marks python keywords, comments, strings and numbers", () => {
    const tokens = tokenize('def f():\n    # note\n    return "x" + 1', "python");
    expect(ofType(tokens, "keyword")).toEqual(["def", "return"]);
    expect(ofType(tokens, "comment")).toEqual(["# note"]);
    expect(ofType(tokens, "string")).toEqual(['"x"']);
    expect(ofType(tokens, "number")).toEqual(["1"]);
  });

  it("knows a JavaScript line comment, block comment and template string", () => {
    const tokens = tokenize("const a = `t`; // end\n/* block */ let b = 0x1f;", "ts");
    expect(ofType(tokens, "keyword")).toEqual(["const", "let"]);
    expect(ofType(tokens, "comment")).toEqual(["// end", "/* block */"]);
    expect(ofType(tokens, "string")).toEqual(["`t`"]);
    expect(ofType(tokens, "number")).toEqual(["0x1f"]);
  });

  it("does not read a keyword inside a longer identifier", () => {
    const tokens = tokenize("defined = imports", "python");
    expect(ofType(tokens, "keyword")).toEqual([]);
  });

  it("does not treat a # in JavaScript as a comment", () => {
    const tokens = tokenize("a.#priv = 1", "js");
    expect(ofType(tokens, "comment")).toEqual([]);
  });

  it("tokenizes an unknown language without keywords and without throwing", () => {
    const code = 'whatever "quoted" 12';
    const tokens = tokenize(code, "brainfreeze");
    expect(joined(tokens)).toBe(code);
    expect(ofType(tokens, "keyword")).toEqual([]);
    expect(ofType(tokens, "string")).toEqual(['"quoted"']);
  });

  it("survives an unterminated string", () => {
    const code = 'x = "open';
    expect(joined(tokenize(code, "python"))).toBe(code);
  });
});
