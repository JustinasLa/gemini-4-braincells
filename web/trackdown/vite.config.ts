import { defineConfig } from "vite";

// base "./" so the built app works from any path (e.g. /trackdown/ behind Caddy).
export default defineConfig({
  base: "./",
  server: { proxy: { "/api": process.env.API_ORIGIN ?? "http://127.0.0.1:8000" } },
  build: { outDir: "dist", assetsInlineLimit: 0 },
});
