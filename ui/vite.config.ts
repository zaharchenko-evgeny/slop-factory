import { readFileSync } from "node:fs";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

// Until `factory ui` (S2) serves `/api/snapshot`, `npm run dev` serves the S1 snapshot in dev-stub/snapshot.json.
const stub = (): Plugin => ({
  name: "snapshot-dev-stub",
  configureServer(server) {
    server.middlewares.use("/api/snapshot", (_req, res) => {
      res.setHeader("content-type", "application/json");
      res.end(readFileSync(path.resolve(__dirname, "dev-stub/snapshot.json")));
    });
  },
});

// The bundle is committed to the package assets path, so file names carry no hash: a rebuild of the same
// sources is byte-identical and tests/ui/test_ui_bundle_in_sync.py can compare it.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss(), stub()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: {
    outDir: "../factory/ui/assets",
    emptyOutDir: true,
    rollupOptions: { output: { entryFileNames: "app.js", chunkFileNames: "[name].js", assetFileNames: "[name][extname]" } },
  },
  test: { environment: "jsdom", globals: true, setupFiles: ["src/test/setup.ts"] },
});
