import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs on :8000; the dev server proxies /api and /health to it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/ready": "http://localhost:8000",
      "/metrics": "http://localhost:8000",
    },
  },
});
