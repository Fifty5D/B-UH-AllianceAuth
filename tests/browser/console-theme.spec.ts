import {expect, test} from "./offline-ui";
import {registerThemeChecks} from "./theme-checks";
import {openVpsHealth, syntheticVpsMetrics} from "./vps-health-fixtures";

// Preview UI runs the same matrix through console-preview.spec.ts so it can
// retain screenshots. Avoid executing the expensive matrix twice while still
// keeping the control/link regression below in both lanes.
if (!process.env.BUH_PREVIEW_OUTPUT_DIR) {
  registerThemeChecks(false);
}

test("source VPS storage keeps the system and Auth data disks distinct", async ({page}) => {
  const login = await page.request.post("/__test__/login/", {form: {role: "director"}});
  expect(login.ok()).toBeTruthy();
  await openVpsHealth(page);
  await expect(page.getByRole("heading", {name: "Storage", exact: true})).toBeVisible();
  await expect(page.locator('[data-metric="disk"]')).toContainText("System disk");
  await expect(page.locator("#vh-disk")).toHaveText("60.0%");
  await expect(page.locator("#vh-disk-detail")).toContainText(/144(?:\.0)? GiB\s*\/\s*240(?:\.0)? GiB/);
  await expect(page.locator("#vh-disk-free")).toContainText(/96(?:\.0)? GiB/);
  await expect(page.locator("#vh-data-disk")).toHaveText("25.0%");
  await expect(page.locator("#vh-data-disk-detail")).toContainText(/125(?:\.0)? GiB\s*\/\s*500(?:\.0)? GiB/);
  await expect(page.locator("#vh-data-disk-free")).toContainText(/375(?:\.0)? GiB/);
  await expect(page.locator("#vh-disk-bar")).toHaveAttribute("style", /width:\s*60%/);
  await expect(page.locator("#vh-data-disk-bar")).toHaveAttribute("style", /width:\s*25%/);
});

for (const status of ["not_mounted", "duplicate", "unavailable"] as const) {
  test(`source VPS storage never substitutes root readings for ${status} Auth data`, async ({page}) => {
    const login = await page.request.post("/__test__/login/", {form: {role: "director"}});
    expect(login.ok()).toBeTruthy();
    const sample = syntheticVpsMetrics();
    const data = sample.host.filesystems!.find((filesystem) => filesystem.id === "data")!;
    Object.assign(data, {available: false, status, total_bytes: null, used_bytes: null,
      free_bytes: null, available_bytes: null, percent: null});
    await openVpsHealth(page, sample);
    await expect(page.locator("#vh-disk")).toHaveText("60.0%");
    await expect(page.locator("#vh-data-disk")).toHaveText("Unavailable");
    await expect(page.locator("#vh-data-disk-detail")).toContainText("Storage reading unavailable");
    await expect(page.locator("#vh-data-disk-bar")).not.toHaveClass(/warning|danger/);
    await expect(page.locator("#vh-data-disk-bar")).toHaveAttribute("style", /width:\s*0%/);
  });
}

for (const legacy of [false, true]) {
  test(`source VPS storage handles ${legacy ? "an older agent" : "a missing data row"} without a data disk fallback`, async ({page}) => {
    const login = await page.request.post("/__test__/login/", {form: {role: "director"}});
    expect(login.ok()).toBeTruthy();
    const sample = syntheticVpsMetrics();
    sample.host.filesystems = legacy ? undefined
      : sample.host.filesystems!.filter((filesystem) => filesystem.id === "system");
    await openVpsHealth(page, sample);
    await expect(page.locator("#vh-disk")).toHaveText("60.0%");
    await expect(page.locator("#vh-disk-detail")).toContainText(/144(?:\.0)? GiB\s*\/\s*240(?:\.0)? GiB/);
    await expect(page.locator("#vh-data-disk")).toHaveText("Unavailable");
    await expect(page.locator("#vh-data-disk-detail")).toContainText("Storage reading unavailable");
  });
}

for (const fullDisk of ["system", "data"] as const) {
  test(`source VPS storage marks only the full ${fullDisk} disk`, async ({page}) => {
    const login = await page.request.post("/__test__/login/", {form: {role: "director"}});
    expect(login.ok()).toBeTruthy();
    await openVpsHealth(page, syntheticVpsMetrics(fullDisk === "system" ? 95 : 60,
      fullDisk === "data" ? 95 : 25));
    await expect(page.locator("#vh-disk")).toHaveText(fullDisk === "system" ? "95.0%" : "60.0%");
    await expect(page.locator("#vh-data-disk")).toHaveText(fullDisk === "data" ? "95.0%" : "25.0%");
    await expect(page.locator(fullDisk === "system" ? "#vh-disk-bar" : "#vh-data-disk-bar")).toHaveClass(/danger/);
    await expect(page.locator(fullDisk === "system" ? "#vh-data-disk-bar" : "#vh-disk-bar")).not.toHaveClass(/danger/);
  });
}

test("source VPS storage clears a previously available data reading when its mount disappears", async ({page}) => {
  const login = await page.request.post("/__test__/login/", {form: {role: "director"}});
  expect(login.ok()).toBeTruthy();
  let sample = syntheticVpsMetrics(60, 95);
  await openVpsHealth(page, () => sample);
  await expect(page.locator("#vh-data-disk")).toHaveText("95.0%");
  sample = syntheticVpsMetrics();
  const data = sample.host.filesystems!.find((filesystem) => filesystem.id === "data")!;
  Object.assign(data, {available: false, status: "not_mounted", total_bytes: null, used_bytes: null,
    free_bytes: null, available_bytes: null, percent: null});
  await expect(page.locator("#vh-data-disk")).toHaveText("Unavailable", {timeout: 10_000});
  await expect(page.locator("#vh-data-disk-bar")).not.toHaveClass(/danger/);
  await expect(page.locator("#vh-data-disk-bar")).toHaveAttribute("style", /width:\s*0%/);
  await expect(page.locator("#vh-disk")).toHaveText("60.0%");
});

test("shared console style matches Moon Tax and leaves existing controls available", async ({page}) => {
  const response = await page.request.post("/__test__/login/", {form: {role: "director"}});
  expect(response.ok()).toBeTruthy();
  await page.goto("/moon-tax/");
  const reference = await page.locator(".tax-hero").evaluate((element) => ({
    background: getComputedStyle(element).backgroundImage,
    border: getComputedStyle(element).borderTopColor,
    radius: getComputedStyle(element).borderTopLeftRadius,
  }));
  const panelBackground = await page.locator(".tax-panel").first().evaluate(
    (element) => getComputedStyle(element).backgroundColor,
  );

  await page.goto("/structure-operations/");
  await expect(page.getByRole("heading", {name: "Structure Operations", exact: true})).toBeVisible();
  expect(await page.locator(".ops-hero").evaluate((element) => ({
    background: getComputedStyle(element).backgroundImage,
    border: getComputedStyle(element).borderTopColor,
    radius: getComputedStyle(element).borderTopLeftRadius,
  }))).toEqual(reference);
  expect(await page.locator(".ops-panel").first().evaluate(
    (element) => getComputedStyle(element).backgroundColor,
  )).toBe(panelBackground);
  await expect(page.getByRole("button", {name: "Sync now"})).toBeEnabled();
  await expect(page.getByRole("link", {name: /Export$/})).toHaveAttribute(
    "href", /\/export\.csv/,
  );
  await page.getByLabel("Fuel state", {exact: true}).selectOption("warning");
  await page.getByRole("button", {name: /Apply$/}).click();
  await expect(page).toHaveURL(/fuel=warning/);
  await expect(page.getByLabel("Fuel state", {exact: true})).toHaveValue("warning");

  await page.getByRole("link", {name: /Schedule$/}).click();
  await expect(page.getByRole("heading", {name: "Operations Schedule"})).toBeVisible();
  expect(await page.locator(".schedule-hero").evaluate(
    (element) => getComputedStyle(element).backgroundImage,
  )).toBe(reference.background);
  await page.getByRole("button", {name: /Add event$/}).click();
  await expect(page.getByRole("heading", {name: "Add to the schedule"})).toBeVisible();
  await expect(page.getByLabel("Title", {exact: true})).toBeEditable();
  await page.getByRole("button", {name: "Cancel", exact: true}).click();
  await expect(page.locator("#schedule-event-editor")).toBeHidden();
});
