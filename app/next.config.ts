import type { NextConfig } from "next";
// Relative, not `@/`: this file is loaded before the app's path aliases exist.
import { MAX_SEND_BODY_BYTES } from "./src/lib/chat/attachments";

export const SECURITY_HEADERS = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  },
  {
    key: "Content-Security-Policy",
    value: "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'",
  },
] as const;

const nextConfig: NextConfig = {
  output: "standalone",
  // Keep local/CI/Docker standalone layouts identical even when this app is
  // checked out beneath a repository that has its own package lock.
  outputFileTracingRoot: process.cwd(),
  turbopack: { root: process.cwd() },
  serverExternalPackages: ["ws", "dompurify"],
  experimental: {
    // `/api/chat/send` runs behind `src/proxy.ts`, and Next buffers a proxied
    // body only up to this limit (10 MB by default), handing the route the
    // rest truncated. A turn carrying three files at the limit is larger, so
    // the proxy takes exactly what the send route itself will read.
    proxyClientMaxBodySize: MAX_SEND_BODY_BYTES,
  },
  async headers() {
    return [{ source: "/:path*", headers: [...SECURITY_HEADERS] }];
  },
};

export default nextConfig;
