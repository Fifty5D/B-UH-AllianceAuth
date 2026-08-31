import {expect, Locator, Page, test} from "@playwright/test";

type TestRole = "director" | "member" | "restricted" | "separate" | "none";

async function loginAs(page: Page, role: TestRole) {
  const response = await page.request.post("/__test__/login/", {form: {role}});
  expect(response.ok()).toBeTruthy();
}

function tableUnder(page: Page, heading: string): Locator {
  return page
    .getByRole("heading", {name: heading, exact: true})
    .locator("xpath=ancestor::section[1]")
    .locator("table[data-sortable-table]");
}

function primaryRows(table: Locator): Locator {
  return table.locator("tbody > tr:not([data-sort-child])");
}

async function sortValues(table: Locator, column: number): Promise<string[]> {
  return primaryRows(table).evaluateAll((rows, columnIndex) => {
    return rows.map((element) => {
      const row = element as HTMLTableRowElement;
      return row.cells[columnIndex]?.dataset.sortValue ?? "";
    });
  }, column);
}

async function expectSummaryValue(summary: Locator, label: string, value: string) {
  const item = summary.locator("article").filter({hasText: label});
  await expect(item).toHaveCount(1);
  await expect(item.locator("strong")).toHaveText(value);
}

test("director sees exact combined totals and linked-character identity", async ({page}) => {
  await loginAs(page, "director");
  await page.goto("/moon-tax/");

  const totals = tableUnder(page, "Totals by billing account");
  const rows = primaryRows(totals);
  await expect(rows).toHaveCount(4);

  const member = rows.filter({hasText: "Member Main"});
  await expect(member).toHaveCount(1);
  await expect(member.locator("td").nth(0)).toHaveText("Member Main");
  await expect(member.locator("td").nth(1)).toHaveText("Combined account");
  await expect(member.locator("td").nth(2)).toHaveText("2");
  await expect(member.locator("td").nth(3)).toHaveText("517,569,153");
  await expect(member.locator("td").nth(4)).toHaveText("25,878,458");
  await expect(member.locator("td").nth(5)).toHaveText("12,000,000");
  await expect(member.locator("td").nth(6)).toHaveText("13,878,458");

  const separate = rows.filter({hasText: /Separate (Main|Alt)/});
  await expect(separate).toHaveCount(2);
  await expect(separate.locator("td:nth-child(2)")).toHaveText([
    "Individual character",
    "Individual character",
  ]);
  await expect(separate.locator("td:nth-child(3)")).toHaveText(["1", "1"]);

  await member.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/moon-tax\/people\/accounts\/\d+\/$/);
  await expect(page.getByRole("heading", {name: "Member Main", exact: true})).toBeVisible();
  await expect(page.getByText("Combined Auth account", {exact: true})).toBeVisible();

  const characters = page.locator(".tax-character-list > span");
  await expect(characters).toHaveCount(2);
  await expect(characters).toContainText(["Member Alt", "Member Main"]);

  const summary = page.locator('section[aria-label="Person tax summary"]');
  await expectSummaryValue(summary, "Extraction bills", "2");
  await expectSummaryValue(summary, "Mined value", "517,569,153");
  await expectSummaryValue(summary, "Approved paid", "12,000,000");
  await expectSummaryValue(summary, "Outstanding", "13,878,458");
});

test("text, numeric, and date sorting preserve deterministic row navigation", async ({page}) => {
  await loginAs(page, "director");
  await page.goto("/moon-tax/");

  const totals = tableUnder(page, "Totals by billing account");
  await totals.getByRole("button", {name: "Account / character: sort ascending"}).click();
  await expect(totals.locator("thead th").nth(0)).toHaveAttribute(
    "aria-sort",
    "ascending",
  );
  expect(await sortValues(totals, 0)).toEqual([
    "Member Main",
    "Separate Alt",
    "Separate Main",
    "Unlinked Test Miner",
  ]);

  await totals.getByRole("button", {name: "Outstanding: sort ascending"}).click();
  await expect(totals.locator("thead th").nth(6)).toHaveAttribute(
    "aria-sort",
    "ascending",
  );
  expect((await sortValues(totals, 6)).map(Number)).toEqual([
    445747,
    3132345,
    5928412,
    13878458,
  ]);

  const periods = tableUnder(page, "Extraction periods");
  await periods.getByRole("button", {name: "Popped: sort ascending"}).click();
  await expect(periods.locator("thead th").nth(1)).toHaveAttribute(
    "aria-sort",
    "ascending",
  );
  expect((await sortValues(periods, 1)).map((value) => value.slice(0, 10))).toEqual([
    "2026-08-29",
    "2026-08-30",
  ]);

  const firstPeriod = primaryRows(periods).first();
  await firstPeriod.focus();
  await page.keyboard.press(" ");
  await expect(page).toHaveURL(/\/moon-tax\/periods\/\d+\/$/);
  await expect(page.getByRole("heading", {name: "Talidal IV - 2", exact: true})).toBeVisible();
});

test("separate billing keeps each linked character as its own row", async ({page}) => {
  await loginAs(page, "separate");
  await page.goto("/moon-tax/");

  const totals = tableUnder(page, "Totals by billing account");
  const rows = primaryRows(totals);
  await expect(rows).toHaveCount(2);
  await expect(rows.locator("td:nth-child(1)")).toHaveText(["Separate Alt", "Separate Main"]);
  await expect(rows.locator("td:nth-child(2)")).toHaveText([
    "Individual character",
    "Individual character",
  ]);
  await expect(rows.locator("td:nth-child(3)")).toHaveText(["1", "1"]);
  await expect(page.getByText("Member Main", {exact: true})).toHaveCount(0);
  await expect(page.getByText("Unlinked Test Miner", {exact: true})).toHaveCount(0);

  await rows.filter({hasText: "Separate Main"}).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/moon-tax\/people\/accounts\/\d+\/$/);
  await expect(page.getByText("Separate bill per character", {exact: true})).toBeVisible();
  await expect(page.locator(".tax-character-list > span")).toHaveCount(2);
});

test("member and restricted permissions fail closed in the rendered UI", async ({page}) => {
  await loginAs(page, "member");
  await page.goto("/moon-tax/");
  await expect(page.getByText("Member Main", {exact: true}).first()).toBeVisible();
  await expect(page.getByText("Unlinked Test Miner", {exact: true})).toHaveCount(0);
  await expect(page.getByText("Separate Main", {exact: true})).toHaveCount(0);
  await expect(page.getByRole("button", {name: /Waive \/ adjust|History/})).toHaveCount(0);
  await expect(page.getByRole("link", {name: "Member POV"})).toHaveCount(0);

  await loginAs(page, "restricted");
  await page.goto("/moon-tax/");
  await expect(page.getByText("No visible billing accounts", {exact: true})).toBeVisible();
  await expect(page.getByText("Calculated tax", {exact: true})).toHaveCount(0);
  await expect(page.getByRole("button", {name: /Waive \/ adjust|History/})).toHaveCount(0);
});

test("server denies accounts without Moon Tax access", async ({page}) => {
  await loginAs(page, "none");
  const response = await page.goto("/moon-tax/");
  expect(response?.status()).toBe(403);
});

test.describe("stateful payment decision", () => {
  // The action intentionally commits a Director decision. Retrying against the
  // same disposable database would no longer exercise the PENDING -> APPROVED
  // transition and could turn a partial first attempt into a misleading result.
  test.describe.configure({retries: 0});

  test("director accepts one real payment and sees its persisted allocation", async ({page}) => {
    await loginAs(page, "director");
    await page.goto("/moon-tax/payments/?status=PENDING&q=Synthetic%20Moon%20Tax%20payment");

    await expect(page.locator("[data-payment-drag], [data-drop-decision]")).toHaveCount(0);
    const card = page.locator("[data-payment-card]").filter({
      hasText: "Synthetic Moon Tax payment",
    });
    await expect(card).toHaveCount(1);
    await expect(card.locator(".tax-payment-select")).toBeVisible();

    const decisionResponse = page.waitForResponse((response) => {
      const path = new URL(response.url()).pathname;
      return (
        response.request().method() === "POST" &&
        /\/moon-tax\/payments\/\d+\/decide\/$/.test(path)
      );
    });
    await card.getByRole("button", {name: "Accept", exact: true}).click();
    const response = await decisionResponse;
    expect(response.status()).toBe(200);
    expect(await response.json()).toMatchObject({
      ok: true,
      status: "APPROVED",
      status_label: "Approved and allocated",
    });

    await page.goto(
      "/moon-tax/payments/?status=APPROVED&q=Synthetic%20Moon%20Tax%20payment",
    );
    const approved = page.locator("[data-payment-card]").filter({
      hasText: "Synthetic Moon Tax payment",
    });
    await expect(approved).toHaveCount(1);
    await expect(approved.locator("em.tax-status")).toHaveText("Approved and allocated");
    const allocations = approved.locator(".tax-allocation-list > span");
    await expect(allocations).toHaveCount(2);
    await expect(allocations).toContainText(["3,013,350.00 ISK", "1,986,650.00 ISK"]);
  });
});
