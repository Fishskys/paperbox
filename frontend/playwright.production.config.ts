import { defineConfig } from "@playwright/test";
import config from "./playwright.config";

export default defineConfig({
  ...config,
  webServer: {
    command: "npm run preview -- --port 5174",
    url: "http://127.0.0.1:5174",
    reuseExistingServer: false,
  },
});