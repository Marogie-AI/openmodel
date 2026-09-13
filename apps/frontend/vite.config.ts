import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// The API has no CORS middleware and deliberately never will: in production the
// console is served same-origin from the API pod at /app. This dev proxy exists
// precisely so `bun dev` reproduces that same-origin shape without anyone being
// tempted to add CORS to the backend.
const backend = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  base: "/app/", // must equal the StaticFiles mount path in api/app/main.py
  build: { outDir: "dist" }, // the Dockerfile's bun stage copies /web/dist
  server: {
    proxy: {
      "/v1": backend,
      "/api": backend,
      "/admin": backend,
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
