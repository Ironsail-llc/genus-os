import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "happy-dom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/__tests__/**/*.test.{ts,tsx}"],
    globals: true,
    server: {
      deps: {
        // next-auth (Auth.js v5) is ESM that imports `next/server`; under
        // pnpm's nested store vitest's default resolver can't follow that
        // subpath export. Inlining lets vite resolve it via next's exports map.
        inline: ["next-auth", "@auth/core"],
      },
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
