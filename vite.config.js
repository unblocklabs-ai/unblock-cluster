import { defineConfig } from "vite";

export default defineConfig({
  server: {
    allowedHosts: [".ngrok-free.app"],
    proxy: {
      "/api": "http://127.0.0.1:8080",
    },
  },
});
