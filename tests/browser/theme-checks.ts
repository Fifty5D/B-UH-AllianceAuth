import {mkdirSync} from "node:fs";
import {join} from "node:path";
import {devices, Page} from "@playwright/test";
import {expect, test} from "./offline-ui";
import {authThemes, ConsoleApp, openConsole, selectAuthTheme} from "./console-fixtures";

async function checkReadableText(page: Page, root: string) {
  const result = await page.locator(root).evaluate((scope) => {
    type Color = [number, number, number, number];
    const color = (value: string): Color => {
      const values = value.match(/[\d.]+/g)?.map(Number) || [];
      const scale = value.startsWith("color(srgb ") ? 255 : 1;
      return [(values[0] || 0) * scale, (values[1] || 0) * scale, (values[2] || 0) * scale, values[3] ?? 1];
    };
    const over = (front: Color, back: Color): Color => [
      front[0] * front[3] + back[0] * (1 - front[3]),
      front[1] * front[3] + back[1] * (1 - front[3]),
      front[2] * front[3] + back[2] * (1 - front[3]), 1,
    ];
    const luminance = (c: Color) => c.slice(0, 3).map((v) => {
      v /= 255;
      return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
    }).reduce((n, v, i) => n + v * [.2126, .7152, .0722][i], 0);
    const background = (element: Element): Color | null => {
      const chain: Element[] = [];
      for (let node: Element | null = element; node; node = node.parentElement) chain.push(node);
      let value: Color = [255, 255, 255, 1];
      for (const node of chain.reverse()) {
        const style = getComputedStyle(node);
        // Gradient banners and primary buttons receive separate color checks
        // and visual previews; inspect actual solid panel/control pairs here.
        if (style.backgroundImage !== "none" && !style.backgroundImage.startsWith("url(")) return null;
        value = over(color(style.backgroundColor), value);
      }
      return value;
    };
    const failures: string[] = [];
    let checked = 0;
    const selectors = 'h1,h2,h3,p,small,em,summary,label,th,td,a,button,input,select,.badge,[class*="-status"],[class*="-state"],[class*="-kicker"],[class*="-tag"],.ar-stats strong,.ops-kpi strong';
    for (const element of scope.querySelectorAll(selectors)) {
      const style = getComputedStyle(element);
      if (!element.getClientRects().length || style.visibility !== "visible" || style.opacity === "0"
          || element.matches(':disabled, input[type="checkbox"], input[type="radio"]')
          || element.closest('[aria-hidden="true"], [hidden]')) continue;
      const label = element instanceof HTMLInputElement ? element.value || element.placeholder : element.textContent?.trim();
      if (!label) continue;
      const bg = background(element);
      if (!bg) continue;
      const fg = over(color(style.color), bg);
      const [light, dark] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
      const ratio = (light + .05) / (dark + .05);
      const size = parseFloat(style.fontSize);
      const large = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700);
      const minimum = large ? 3 : 4.5;
      checked++;
      if (ratio + .01 < minimum) failures.push(`${element.tagName}.${element.className}: ${label.slice(0, 55)} (${ratio.toFixed(2)}:1, ${style.color})`);
    }
    return {checked, failures: failures.slice(0, 15)};
  });
  expect(result.checked, `${root} readable text coverage`).toBeGreaterThan(10);
  expect(result.failures, `${root} text contrast`).toEqual([]);
}

async function checkStickyHeaders(page: Page, root: string) {
  const loading = page.locator(`${root} #mining-loading`);
  if (await loading.count()) {
    await expect(loading, `${root} dashboard data settled`).toHaveClass(/is-hidden/);
  }
  const wrapper = page.locator(`${root} .table-responsive:has(> table > thead)`).first();
  if (!await wrapper.count()) return;
  const body = wrapper.locator("tbody").first();
  await expect(body, `${root} sticky table body`).toHaveCount(1);
  const columns = await wrapper.locator("thead th").count();
  expect(columns, `${root} sticky table columns`).toBeGreaterThan(0);
  // Use a structured row even when the seeded page is empty so sticky and
  // horizontal alignment checks cover every table instead of silently skipping.
  await body.evaluate((element, columnCount) => {
    const row = document.createElement("tr");
    row.dataset.testSynthetic = "sticky";
    for (let index = 0; index < columnCount; index++) {
      const cell = document.createElement("td");
      cell.textContent = `Synthetic ${index + 1}`;
      row.append(cell);
    }
    element.replaceChildren(row);
  }, columns);
  await body.evaluate((element) => {
    const rows = [...element.children];
    for (let n = 0; n < 40 && rows.length; n++) {
      const row = rows[n % rows.length].cloneNode(true) as Element;
      row.querySelectorAll("[id]").forEach((element) => element.removeAttribute("id"));
      row.removeAttribute("id");
      element.append(row);
    }
  });
  await wrapper.scrollIntoViewIfNeeded();
  const first = await wrapper.locator("thead").boundingBox();
  await wrapper.evaluate((element) => { element.scrollTop = 240; });
  await expect.poll(() => wrapper.evaluate((element) => element.scrollTop)).toBeGreaterThan(100);
  const after = await wrapper.locator("thead").boundingBox();
  expect(Math.abs(after!.y - first!.y), "header stays fixed while rows scroll").toBeLessThan(2);
  await expect(wrapper.locator("thead")).toHaveCSS("position", "sticky");
  const dimensions = await wrapper.evaluate((element) => ({width: element.clientWidth, scrollWidth: element.scrollWidth}));
  if (dimensions.scrollWidth > dimensions.width + 5) {
    await wrapper.evaluate((element) => { element.scrollLeft = 100; });
    await expect.poll(() => wrapper.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
    const alignment = await wrapper.evaluate((element) => {
      const head = element.querySelector("th")!.getBoundingClientRect();
      const cell = element.querySelector("tbody td")!.getBoundingClientRect();
      return Math.abs(head.left - cell.left);
    });
    expect(alignment, "horizontal scrolling keeps columns aligned").toBeLessThan(2);
  }
}

async function checkChartLabels(page: Page) {
  // The legacy chart uses canvas text. Inspect the rendered pixels so the check
  // covers CSS filters and antialiasing, which DOM text colors cannot describe.
  await expect(page.locator("#vh-chart-empty")).toBeHidden();
  const pixels = await page.locator("#vh-history-chart").screenshot();
  const contrast = await page.evaluate(async (encoded) => {
    const bytes = Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], {type: "image/png"}));
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d")!;
    context.drawImage(bitmap, 0, 0);
    const image = context.getImageData(0, 0, canvas.width, canvas.height);
    const luminance = (offset: number) => [0, 1, 2].reduce((sum, channel) => {
      const value = image.data[offset + channel] / 255;
      const linear = value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
      return sum + linear * [.2126, .7152, .0722][channel];
    }, 0);
    const background = luminance(image.data.length - 4);
    let strongest = 1;
    // The left 38 pixels contain percentage labels; plotted data starts at 42.
    for (let y = 0; y < canvas.height; y++) {
      for (let x = 0; x < Math.min(38, canvas.width); x++) {
        const text = luminance((y * canvas.width + x) * 4);
        strongest = Math.max(strongest, (Math.max(text, background) + .05) / (Math.min(text, background) + .05));
      }
    }
    bitmap.close();
    return strongest;
  }, pixels.toString("base64"));
  expect(contrast, "rendered chart axis text remains readable").toBeGreaterThanOrEqual(4.5);
}

export function registerThemeChecks(capture: boolean) {
  for (const theme of authThemes) {
    test(`console readability and sticky tables in ${theme.name}`, async ({page}) => {
      test.setTimeout(120_000);
      await page.setViewportSize({width: 1440, height: 1000});
      const response = await page.request.post("/__test__/login/", {form: {role: "director"}});
      expect(response.ok()).toBeTruthy();
      await selectAuthTheme(page, theme);
      await page.emulateMedia({reducedMotion: "reduce"});
      for (const viewport of ["desktop", "mobile"] as const) {
        if (viewport === "mobile") {
          await page.setViewportSize({width: 390, height: 844});
          await page.setExtraHTTPHeaders({"User-Agent": devices["iPhone 13"].userAgent});
        }
        for (const app of ["moon-tax", "moon-tax-period", "moon-tax-person", "moon-tax-payments", "moon-tax-policy", "mining-analytics", "structure-operations", "schedule", "archive", "vps"] as ConsoleApp[]) {
          const root = await openConsole(page, app);
          await expect(page.locator(root)).toHaveCSS("color-scheme", theme.light ? "light" : "dark");
          await checkReadableText(page, root);
          if (app === "vps") {
            await checkChartLabels(page);
            await page.getByRole("button", {name: /Restart Auth services$/}).first().click();
            await expect(page.locator("#vh-restart-submit")).toBeDisabled();
            await expect(page.locator(".vh-modal")).toHaveCSS("background-color", theme.light ? "rgb(255, 255, 255)" : "rgb(25, 29, 38)");
            await page.getByRole("button", {name: "Cancel", exact: true}).click();
            await expect(page.locator("#vh-restart-modal")).toBeHidden();
            await expect(page.locator(".modal-backdrop")).toHaveCount(0);
          }
          await page.locator(".nav-padding.overflow-auto").evaluate((element) => { element.scrollTop = 0; });
          if (capture) {
            await page.evaluate(async () => { await document.fonts.ready; });
            const directory = join(process.env.BUH_PREVIEW_OUTPUT_DIR || "preview-output", "themes", theme.tag, viewport);
            mkdirSync(directory, {recursive: true});
            await page.screenshot({path: join(directory, `${app}.png`), fullPage: true, animations: "disabled"});
          }
          await checkStickyHeaders(page, root);
          if (capture && (app === "moon-tax" || app === "structure-operations")) {
            const directory = join(process.env.BUH_PREVIEW_OUTPUT_DIR || "preview-output", "themes", theme.tag, viewport);
            await page.screenshot({path: join(directory, `${app}-scrolled.png`), fullPage: true, animations: "disabled"});
          }
        }
      }
    });
  }
}
