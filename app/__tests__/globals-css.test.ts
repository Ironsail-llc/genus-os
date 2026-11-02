import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

// Without the typography plugin `prose` is an unknown class and Tailwind's
// preflight strips paragraph margins, list markers and heading sizes, so a
// structured markdown reply renders as one dense block.
const appRoot = path.resolve(__dirname, "..");

describe("typography plugin", () => {
  it("is loaded by the global stylesheet", () => {
    const css = readFileSync(path.join(appRoot, "src/app/globals.css"), "utf8");
    expect(css).toContain('@plugin "@tailwindcss/typography"');
  });

  it("is a declared dev dependency", () => {
    const pkg = JSON.parse(readFileSync(path.join(appRoot, "package.json"), "utf8"));
    expect(pkg.devDependencies).toHaveProperty("@tailwindcss/typography");
  });
});
