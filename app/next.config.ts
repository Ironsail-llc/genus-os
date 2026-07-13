import type { NextConfig } from "next";

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
  async headers() {
    return [{ source: "/:path*", headers: [...SECURITY_HEADERS] }];
  },
};

export default nextConfig;
