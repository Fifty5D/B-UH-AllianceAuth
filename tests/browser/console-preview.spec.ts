import {mkdirSync, readFileSync} from "node:fs";
import {join} from "node:path";
import {devices} from "@playwright/test";
import {expect, test} from "./offline-ui";

const fixtures = join(__dirname, "fixtures", "legacy-consoles");
const outputRoot = process.env.BUH_PREVIEW_OUTPUT_DIR || "preview-output";
const metrics = JSON.parse(readFileSync(join(fixtures, "metrics.json"), "utf8"));

for (const viewport of [
  {name: "desktop", width: 1440, height: 1000},
  {name: "mobile", width: 390, height: 844},
]) {
  for (const app of ["structure-operations", "schedule", "archive", "vps"] as const) {
    test(`${app} console preview at ${viewport.name}`, async ({page}) => {
      await page.setViewportSize(viewport);
      if (viewport.name === "mobile") {
        // Auth chooses the initial sidebar state from the request's device type.
        await page.setExtraHTTPHeaders({"User-Agent": devices["iPhone 13"].userAgent});
      }
      const response = await page.request.post("/__test__/login/", {form: {role: "director"}});
      expect(response.ok()).toBeTruthy();
      const legacy = app === "archive" || app === "vps";
      await page.goto(app === "schedule" ? "/structure-operations/schedule/" : "/structure-operations/");
      if (app === "structure-operations") {
        await page.locator("#buh-structure-ops").evaluate((element, html) => {
          element.outerHTML = html;
        }, readFileSync(join(fixtures, "structure-operations.html"), "utf8"));
      }
      if (legacy) {
        // Use actual released legacy markup and scripts with synthetic data.
        // They are not installed in, or connected to, production by these tests.
        const theme = await page.locator("style").allTextContents();
        const shared = theme.find((css) => css.includes("--console-teal"));
        expect(shared).toBeTruthy();
        await page.locator("#buh-structure-ops").evaluate((element, html) => {
          element.outerHTML = html;
        }, readFileSync(join(fixtures, `${app}.html`), "utf8"));
        await page.addStyleTag({content: readFileSync(join(fixtures, `${app}.css`), "utf8") + shared});
        await page.route("**/mock/**", async (route) => {
          expect(route.request().method()).toBe("GET");
          const name = new URL(route.request().url()).pathname.split("/mock/")[1];
          await route.fulfill({json: metrics[name] || {ok: true, jobs: []}});
        });
        await page.addScriptTag({content: readFileSync(join(fixtures, `${app}.js`), "utf8")});
      }
      const root = app === "archive" ? "#buh-archive" : app === "vps" ? "#buh-vps-health"
        : app === "schedule" ? "#buh-ops-schedule" : "#buh-structure-ops";
      await expect(page.locator(root)).toBeVisible();
      if (app === "vps") {
        await expect(page.locator("#vh-connection-label")).toHaveText("Live");
        await page.getByRole("button", {name: /Restart Auth services$/}).first().click();
        await expect(page.locator("#vh-restart-submit")).toBeDisabled();
        await expect(page.locator(".vh-modal")).toHaveCSS("background-color", "rgb(25, 29, 38)");
        await page.getByRole("button", {name: "Cancel", exact: true}).click();
        await expect(page.locator("#vh-restart-modal")).toBeHidden();
        await expect(page.locator(".modal-backdrop")).toHaveCount(0);
      }
      await page.emulateMedia({reducedMotion: "reduce"});
      await page.evaluate(async () => { await document.fonts.ready; });
      const directory = join(outputRoot, viewport.name);
      mkdirSync(directory, {recursive: true});
      if (app === "vps") {
        await page.screenshot({path: join(directory, "vps-controls.png"), fullPage: true, animations: "disabled"});
      }
      // Auth scrolls its content column independently of the document.
      await page.locator(".nav-padding.overflow-auto").evaluate((element) => { element.scrollTop = 0; });
      const screenshotPath = join(directory, `${app}-console.png`);
      await page.screenshot({path: screenshotPath, fullPage: true, animations: "disabled"});
      await test.info().attach(`${app}-${viewport.name}`, {path: screenshotPath, contentType: "image/png"});
    });
  }
}
