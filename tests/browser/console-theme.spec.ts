import {expect, test} from "./offline-ui";

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
  await expect(page.getByRole("link", {name: "Export", exact: true})).toHaveAttribute(
    "href", /\/export\.csv/,
  );
  await page.getByLabel("Fuel state", {exact: true}).selectOption("warning");
  await page.getByRole("button", {name: "Apply", exact: true}).click();
  await expect(page).toHaveURL(/fuel=warning/);
  await expect(page.getByLabel("Fuel state", {exact: true})).toHaveValue("warning");

  await page.getByRole("link", {name: "Schedule", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Operations Schedule"})).toBeVisible();
  expect(await page.locator(".schedule-hero").evaluate(
    (element) => getComputedStyle(element).backgroundImage,
  )).toBe(reference.background);
  await page.getByRole("button", {name: "Add event", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Add to the schedule"})).toBeVisible();
  await expect(page.getByLabel("Title", {exact: true})).toBeEditable();
  await page.getByRole("button", {name: "Cancel", exact: true}).click();
  await expect(page.locator("#schedule-event-editor")).toBeHidden();
});
