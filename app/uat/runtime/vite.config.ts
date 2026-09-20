import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("../../src", import.meta.url)) } },
  css: { postcss: fileURLToPath(new URL("../..", import.meta.url)) },
  server: {
    host: "127.0.0.1", port: Number(process.env.RUNTIME_UAT_UI_PORT || "5321"), strictPort: true,
    fs: { allow: [fileURLToPath(new URL("../..", import.meta.url))] },
    proxy: {
      "/api/bridge": { target: `http://127.0.0.1:${process.env.RUNTIME_UAT_API_PORT || "5322"}`, rewrite: path => path.replace(/^\/api\/bridge/, "") },
      "/uat": { target: `http://127.0.0.1:${process.env.RUNTIME_UAT_API_PORT || "5322"}` },
    },
  },
});
