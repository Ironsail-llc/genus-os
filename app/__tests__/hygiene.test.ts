/**
 * Leftovers from the project template: the create-next-app SVGs nothing
 * references, and a lint config pinned to a different Next than the app runs.
 */
import { describe, expect, it } from "vitest";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");

describe("app hygiene", () => {
  it("ships none of the create-next-app boilerplate SVGs", () => {
    for (const name of ["file.svg", "globe.svg", "next.svg", "vercel.svg", "window.svg"]) {
      expect(existsSync(path.join(root, "public", name)), name).toBe(false);
    }
  });

  it("pins eslint-config-next to the Next version the app runs", () => {
    const pkg = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
    expect(pkg.devDependencies["eslint-config-next"]).toBe(pkg.dependencies.next);
  });

  it("leaves no console.log in the dashboard source (warn and error are kept)", () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name !== "__tests__") walk(full);
        } else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
          readFileSync(full, "utf8")
            .split("\n")
            .forEach((line, i) => {
              if (/\bconsole\.log\(/.test(line)) offenders.push(`${path.relative(root, full)}:${i + 1}`);
            });
        }
      }
    };
    walk(path.join(root, "src"));
    expect(offenders).toEqual([]);
  });
});
