import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "", "");
  // The dev launcher injects VITE_API_PROXY for the selected backend port.
  // Prefer that process value over a stale .env.local entry so restarting on
  // a non-default port cannot silently proxy the workbench to an old server.
  const processEnv = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env;
  const apiProxy = processEnv?.VITE_API_PROXY ?? env.VITE_API_PROXY ?? "http://localhost:8000";
  return ({
  plugins: [react()],
  test: {
    environment: "jsdom",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": apiProxy,
    },
  },
  });
});
