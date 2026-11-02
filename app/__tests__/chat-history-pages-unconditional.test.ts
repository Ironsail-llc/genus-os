import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

// Stored history pages (real times, "Load earlier") are the only behaviour:
// nothing in the dashboard reads a setting for them or describes a mode where
// they are switched off. The button shows whenever the engine says `hasMore`.
const appRoot = path.resolve(__dirname, "..");
const retiredFlag = "ROBOTHOR_" + "CHAT_HISTORY_PAGES";
const offMode = /pages (switched )?off|history pages off/i;

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sources(full);
    return /\.(ts|tsx)$/.test(name) ? [full] : [];
  });
}

describe("chat history pages", () => {
  const files = [...sources(path.join(appRoot, "src")), ...sources(path.join(appRoot, "e2e"))];

  it("are not behind a setting", () => {
    const readers = files.filter((f) => readFileSync(f, "utf8").includes(retiredFlag));
    expect(readers.map((f) => path.relative(appRoot, f))).toEqual([]);
  });

  it("have no switched-off mode described anywhere in the dashboard", () => {
    const mentions = files.filter((f) => offMode.test(readFileSync(f, "utf8")));
    expect(mentions.map((f) => path.relative(appRoot, f))).toEqual([]);
  });
});
