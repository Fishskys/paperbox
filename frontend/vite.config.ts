import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target =
    process.env.PAPERBOX_API_TARGET ||
    env.PAPERBOX_API_TARGET ||
    "http://127.0.0.1:8077";
  const proxy = { "/api": { target }, "/health": { target } };
  return {
    plugins: [react()],
    server: { port: 5173, strictPort: true, proxy },
    preview: { port: 4173, strictPort: true, proxy },
  };
});
