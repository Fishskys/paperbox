import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { apiProxy } from "./proxy-auth.ts";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target =
    process.env.PAPERBOX_API_TARGET ||
    env.PAPERBOX_API_TARGET ||
    "http://127.0.0.1:8077";
  const key = (
    process.env.PAPERBOX_API_KEY ||
    env.PAPERBOX_API_KEY ||
    ""
  ).trim();
  const proxy = { "/api": apiProxy(target, key), "/health": { target } };
  return {
    plugins: [react()],
    server: { port: 5173, strictPort: true, proxy },
    preview: { port: 4173, strictPort: true, proxy },
  };
});
