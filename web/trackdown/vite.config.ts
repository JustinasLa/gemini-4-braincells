import { defineConfig } from "vite";
import { memeMockServer } from "./scripts/memeMockServer";

// base "./" so the built app works from any path (/ and /trackdown/ behind Caddy).
export default defineConfig({
  plugins: [memeMockServer()],
  base: "./",
  server: { proxy: { "/api": process.env.API_ORIGIN ?? "http://127.0.0.1:8000" } },
  build: { outDir: "dist", assetsInlineLimit: 0 },
});
