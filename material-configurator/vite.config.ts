import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Static build: `dist/` can be deployed as-is to Cloudflare Pages (no server code).
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { target: "es2022", chunkSizeWarningLimit: 800 },
});
