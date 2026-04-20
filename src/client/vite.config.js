import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev UI :5173 → proxy API/WebSocket to uvicorn :8080 (same-origin; avoids CORS issues).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    host: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8080",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
