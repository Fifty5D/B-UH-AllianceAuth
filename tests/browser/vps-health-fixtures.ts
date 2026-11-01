import {Page, expect} from "@playwright/test";

const gib = 1024 ** 3;

export type VpsFilesystem = {
  id: "system" | "data";
  label: string;
  mountpoint: string;
  available: boolean;
  status: "mounted" | "not_mounted" | "duplicate" | "unavailable";
  total_bytes: number | null;
  used_bytes: number | null;
  free_bytes: number | null;
  available_bytes: number | null;
  percent: number | null;
};

export function syntheticVpsMetrics(systemPercent = 60, dataPercent = 25) {
  const filesystem = (id: "system" | "data", total: number, percent: number): VpsFilesystem => ({
    id,
    label: id === "system" ? "System disk" : "Auth data",
    mountpoint: id === "system" ? "/" : "/srv/buh-data",
    available: true,
    status: "mounted",
    total_bytes: total * gib,
    used_bytes: total * percent / 100 * gib,
    free_bytes: total * (100 - percent) / 100 * gib,
    available_bytes: total * (100 - percent) / 100 * gib,
    percent,
  });
  return {
    ok: true,
    generated_at: "2026-10-31T12:00:00Z",
    agent_error: "",
    host: {
      cpu_percent: 12.4,
      cpu_count: 4,
      load_1: 0.11,
      load_5: 0.36,
      load_15: 0.36,
      memory_percent: 48.7,
      memory_used_bytes: 4 * gib,
      memory_total_bytes: 8.2 * gib,
      disk_percent: systemPercent,
      disk_used_bytes: 240 * systemPercent / 100 * gib,
      disk_total_bytes: 240 * gib,
      network_rx_bytes_per_second: 1024,
      network_tx_bytes_per_second: 2048,
      uptime_seconds: 440000,
      filesystems: [filesystem("system", 240, systemPercent), filesystem("data", 500, dataPercent)] as VpsFilesystem[] | undefined,
    },
    workload: {queued: 0, active_total: 0, reserved_total: 0, scheduled_total: 0, nodes: [], error: ""},
    docker: {total: 0, healthy: 0, unhealthy: 0, containers: []},
    workers: {desired: 2, online: 2},
    alerts: [],
    actions: [],
    latest_update: null,
  };
}

export type VpsMetricSample = ReturnType<typeof syntheticVpsMetrics>;
export type VpsMetricSource = VpsMetricSample | (() => VpsMetricSample);

export async function openVpsHealth(page: Page, metrics: VpsMetricSource = syntheticVpsMetrics()) {
  // Exercise the source-built view and assets. Only synthetic telemetry is
  // intercepted; this never contacts a real host agent or changes a service.
  await page.route("**/vps-health/api/**", async (route) => {
    expect(route.request().method()).toBe("GET");
    const path = new URL(route.request().url()).pathname;
    if (path === "/vps-health/api/live/") {
      await route.fulfill({json: typeof metrics === "function" ? metrics() : metrics});
    } else if (path === "/vps-health/api/history/") {
      await route.fulfill({json: {points: [
        {captured_at: "2026-10-31T11:00:00Z", cpu_percent: 8, memory_percent: 45, disk_percent: 59},
        {captured_at: "2026-10-31T12:00:00Z", cpu_percent: 12.4, memory_percent: 48.7, disk_percent: 60},
      ]}});
    } else {
      throw new Error(`Unexpected synthetic VPS API request: ${path}`);
    }
  });
  const response = await page.goto("/vps-health/");
  expect(response?.status()).toBe(200);
  await expect(page.locator("#buh-vps-health")).toBeVisible();
  await expect(page.locator("#vh-connection-label")).toHaveText("Live");
  await page.waitForFunction(() => {
    const canvas = document.getElementById("vh-history-chart") as HTMLCanvasElement | null;
    const context = canvas?.getContext("2d");
    return Boolean(context && context.getImageData(0, 0, canvas!.width, canvas!.height).data
      .some((value, index) => index % 4 === 3 && value > 0));
  });
  // The real Auth base renders managed CustomCSS after application styles.
  // Do not inject a stylesheet missing from the rendered application.
  const customStyles = await page.locator("style").allTextContents();
  expect(customStyles.some((css) => css.includes("--console-teal"))).toBeTruthy();
  return "#buh-vps-health";
}
