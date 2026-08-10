import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendTarget = env.VITE_BACKEND_PROXY_TARGET || "http://127.0.0.1:8000";

  return {
  build: {
    outDir: "dist/client",
  },
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    proxy: {
      "/api": { target: backendTarget, changeOrigin: true },
      "/health": { target: backendTarget, changeOrigin: true },
    },
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
  },
  plugins: [react()],
  };
});
