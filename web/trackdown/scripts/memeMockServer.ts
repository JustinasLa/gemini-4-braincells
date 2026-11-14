import { readFileSync } from "node:fs";
import type { Plugin } from "vite";

export const memeMockServer = (): Plugin => ({
  name: "meme-mock-image-errors",
  apply: "serve",
  configureServer(server) {
    // the mock's submitted image: served by the dev server only, so it never lands in dist/
    server.middlewares.use("/__meme_mock__/upload.svg", (_request, response) => {
      response.setHeader("Content-Type", "image/svg+xml");
      response.end(readFileSync(new URL("./fixtures/meme-upload.svg", import.meta.url)));
    });
    server.middlewares.use("/__meme_missing__", (_request, response) => {
      response.statusCode = 404;
      response.setHeader("Content-Type", "text/plain");
      response.end("MOCK: image unavailable");
    });
  },
});
