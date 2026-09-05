import {readFileSync} from "node:fs";
import {join} from "node:path";
import {Page, expect} from "@playwright/test";

export type ConsoleApp = "moon-tax" | "moon-tax-period" | "moon-tax-payments" | "moon-tax-policy" | "structure-operations" | "schedule" | "archive" | "vps";
const fixtures = join(__dirname, "fixtures", "legacy-consoles");
const metrics = JSON.parse(readFileSync(join(fixtures, "metrics.json"), "utf8"));

export async function openConsole(page: Page, app: ConsoleApp) {
  const legacy = app === "archive" || app === "vps";
  const path = app === "moon-tax" || app === "moon-tax-period" ? "/moon-tax/" : app === "moon-tax-payments" ? "/moon-tax/payments/"
    : app === "moon-tax-policy" ? "/moon-tax/policy/" : app === "schedule"
    ? "/structure-operations/schedule/" : "/structure-operations/";
  await page.goto(path);
  if (app === "moon-tax-period") {
    const href = await page.locator('a[href^="/moon-tax/periods/"]').first().getAttribute("href");
    expect(href).toBeTruthy();
    await page.goto(href!);
  }
  if (app === "structure-operations") {
    await page.locator("#buh-structure-ops").evaluate((element, html) => {
      element.outerHTML = html;
    }, readFileSync(join(fixtures, "structure-operations.html"), "utf8"));
  }
  if (legacy) {
    // Original released markup/scripts, synthetic records and GET-only mocks.
    const shared = (await page.locator("style").allTextContents()).find((css) => css.includes("--console-teal"));
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
  const root = app.startsWith("moon-tax") ? "#moon-tax-app" : app === "archive" ? "#buh-archive"
    : app === "vps" ? "#buh-vps-health" : app === "schedule" ? "#buh-ops-schedule" : "#buh-structure-ops";
  await expect(page.locator(root)).toBeVisible();
  if (app === "vps") await expect(page.locator("#vh-connection-label")).toHaveText("Live");
  return root;
}

export const authThemes = [
  {name: "Darkly", tag: "darkly", hook: "allianceauth.theme.darkly.auth_hooks.DarklyThemeHook", light: false},
  {name: "Flatly", tag: "flatly", hook: "allianceauth.theme.flatly.auth_hooks.FlatlyThemeHook", light: true},
  {name: "Materia", tag: "materia", hook: "allianceauth.theme.materia.auth_hooks.MateriaThemeHook", light: true},
  {name: "Bootstrap", tag: "bootstrap", hook: "allianceauth.theme.bootstrap.auth_hooks.BootstrapThemeHook", light: true},
  {name: "Bootstrap Dark", tag: "bootstrap-dark", hook: "allianceauth.theme.bootstrap.auth_hooks.BootstrapDarkThemeHook", light: false},
] as const;

export async function selectAuthTheme(page: Page, theme: typeof authThemes[number]) {
  await page.goto("/moon-tax/");
  const form = page.locator('form:has(select[name="theme"])');
  await expect(form.locator(`option[value="${theme.hook}"]`)).toHaveCount(1);
  const response = await page.request.post((await form.getAttribute("action"))!, {
    form: {theme: theme.hook, csrfmiddlewaretoken: await form.locator('[name="csrfmiddlewaretoken"]').inputValue()},
  });
  expect(response.ok()).toBeTruthy();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme.tag);
}
