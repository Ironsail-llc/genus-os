import { NextResponse } from "next/server";

/** Process-only liveness. Never reaches a dependency. */
export async function GET() {
  return NextResponse.json({
    status: "ok",
    service: "dashboard",
    timestamp: new Date().toISOString(),
  });
}
