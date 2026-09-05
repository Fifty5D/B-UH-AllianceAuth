import {test as base, expect} from "@playwright/test";
import {readFileSync} from "node:fs";
import {join} from "node:path";

const assets: {url: string; file: string}[] = JSON.parse(
  readFileSync(join(__dirname, "preview-assets.json"), "utf8"),
);

export const test = base.extend({
  page: async ({page}, use) => {
    await page.route("https://cdnjs.cloudflare.com/**", async (route) => {
      const asset = assets.find((entry) => entry.url === route.request().url());
      if (!asset) return route.abort();
      const contentType = asset.file.endsWith(".css") ? "text/css"
        : asset.file.endsWith(".js") ? "application/javascript" : "font/woff2";
      await route.fulfill({
        body: readFileSync(join(__dirname, "preview-assets", asset.file)),
        contentType,
        headers: {"access-control-allow-origin": "*"},
      });
    });
    await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
    await use(page);
  },
});
export {expect};
