import type { Plugin } from "vite";

export const memeMockServer = (): Plugin => ({
  name: "meme-mock-image-errors",
  apply: "serve",
  configureServer(server) {
    server.middlewares.use("/__meme_missing__", (_request, response) => {
      response.statusCode = 404;
      response.setHeader("Content-Type", "text/plain");
      response.end("MOCK: image unavailable");
    });
  },
});
