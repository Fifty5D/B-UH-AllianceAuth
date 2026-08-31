import {defineConfig} from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts/,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  use: {
    baseURL: process.env.BUH_BROWSER_BASE_URL || "http://web:8000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  reporter: [["line"], ["html", {open: "never"}]],
  projects: [{name: "chromium", use: {browserName: "chromium"}}],
});
