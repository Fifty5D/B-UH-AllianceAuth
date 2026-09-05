import {devices, Page} from "@playwright/test";
import {expect, test} from "./offline-ui";
import {mkdirSync} from "node:fs";
import {join} from "node:path";

type PreviewRole = "director" | "member";

const outputRoot = process.env.BUH_PREVIEW_OUTPUT_DIR || "preview-output";

const viewports = [
  {name: "desktop", width: 1440, height: 1000},
  {name: "mobile", width: 390, height: 844},
] as const;

const pages = [
  {
    name: "moon-tax-overview-director",
    role: "director" as PreviewRole,
    path: "/moon-tax/",
    heading: "Moon Tax",
  },
  {
    name: "moon-tax-payments-director",
    role: "director" as PreviewRole,
    path: "/moon-tax/payments/",
    heading: "Payment review",
  },
  {
    name: "moon-tax-overview-member",
    role: "member" as PreviewRole,
    path: "/moon-tax/",
    heading: "Moon Tax",
  },
] as const;

async function loginAs(page: Page, role: PreviewRole) {
  const response = await page.request.post("/__test__/login/", {form: {role}});
  expect(response.ok()).toBeTruthy();
}

async function settleForScreenshot(page: Page) {
  await page.emulateMedia({reducedMotion: "reduce"});
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation-delay: 0s !important;
        animation-duration: 0s !important;
        caret-color: transparent !important;
        transition-delay: 0s !important;
        transition-duration: 0s !important;
      }
      html { scroll-behavior: auto !important; }
    `,
  });
  await page.evaluate(async () => {
    if (document.fonts) {
      await document.fonts.ready;
    }
  });
}

test.describe("synthetic visual preview", () => {
  test.use({
    colorScheme: "dark",
    locale: "en-US",
    timezoneId: "UTC",
  });

  for (const viewport of viewports) {
    for (const previewPage of pages) {
      test(`${previewPage.name} at ${viewport.name}`, async ({page}) => {
        await page.setViewportSize(viewport);
        if (viewport.name === "mobile") {
          await page.setExtraHTTPHeaders({"User-Agent": devices["iPhone 13"].userAgent});
        }
        await loginAs(page, previewPage.role);
        await page.goto(previewPage.path, {waitUntil: "networkidle"});
        await expect(page.locator("#moon-tax-app")).toBeVisible();
        await expect(
          page.getByRole("heading", {name: previewPage.heading, exact: true}),
        ).toBeVisible();
        await settleForScreenshot(page);

        const directory = join(outputRoot, viewport.name);
        mkdirSync(directory, {recursive: true});
        const screenshotPath = join(directory, `${previewPage.name}.png`);
        await page.screenshot({
          path: screenshotPath,
          fullPage: true,
          animations: "disabled",
          caret: "hide",
          scale: "css",
        });
        await test.info().attach(`${previewPage.name}-${viewport.name}`, {
          path: screenshotPath,
          contentType: "image/png",
        });
      });
    }
  }
});
