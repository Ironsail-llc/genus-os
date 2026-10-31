import { describe, it, expect, vi, afterEach } from "vitest";
import { serverLog } from "@/lib/server-log";

describe("serverLog", () => {
  afterEach(() => vi.restoreAllMocks());

  it("writes one JSON object per line to standard output", () => {
    const write = vi.spyOn(process.stdout, "write").mockImplementation(() => true);

    serverLog({ event: "web_vital", name: "LCP", value: 1.5 });

    expect(write).toHaveBeenCalledTimes(1);
    const line = String(write.mock.calls[0][0]);
    expect(line.endsWith("\n")).toBe(true);
    expect(line.slice(0, -1)).not.toContain("\n");
    expect(JSON.parse(line)).toEqual({ event: "web_vital", name: "LCP", value: 1.5 });
  });
});
