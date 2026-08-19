/**
 * Capture the main pages and check the things unit tests cannot see.
 *
 * jsdom has no layout, so alignment and spacing can only be verified in a real browser.
 *   BASE=http://host:port WEB_USER=alice WEB_PASSWORD=... OUT=/tmp/aisbench npm run screenshot
 */
import { chromium } from "@playwright/test";

const BASE = process.env.BASE ?? "http://127.0.0.1:8000";
const OUT = process.env.OUT ?? "/tmp/aisbench";
const USER = process.env.WEB_USER ?? "alice";
const PASSWORD = process.env.WEB_PASSWORD ?? "";

// Chromium inherits the system proxy, which does not reach the service.
const browser = await chromium.launch({ args: ["--no-proxy-server"] });
const page = await browser.newPage({ viewportSize: { width: 1280, height: 900 } });

// Recorded only after sign-in: a 401 from /api/me before it is the normal signed-out path.
let watching = false;
const problems = [];
const note = (text) => watching && problems.push(text);
page.on("console", (m) => m.type() === "error" && note(`console: ${m.text()}`));
page.on("pageerror", (e) => note(`pageerror: ${e.message}`));
page.on("requestfailed", (r) => note(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));
page.on("response", (r) => {
  if (r.url().includes("/api/") && r.status() >= 400) {
    note(`http ${r.status()}: ${new URL(r.url()).pathname}`);
  }
});

await page.goto(BASE, { waitUntil: "networkidle" });
await page.getByLabel("用户名").fill(USER);
await page.getByLabel("密码").fill(PASSWORD);
await page.getByRole("button", { name: "登录" }).click();
await page.getByRole("link", { name: "我的模型" }).waitFor();
watching = true;

/** Each measurement runs on the page that actually contains the elements. */
const measurements = {};
// Wait for the page to have settled on real content. A fixed timeout over a slow link
// measures an empty page and reports a confident zero.
const SETTLED = {
  "new-job": "#job-dataset option:nth-child(2), .form-error",
  jobs: ".data-table tbody tr, .empty-state",
  comparison: ".checkbox-option, .empty-state",
  models: ".resource-row, .empty-state",
  datasets: ".data-table tbody tr, .empty-state",
};

const pages = [
  ["new-job", "新建评测", () => ({
    modelOptions: [...document.querySelectorAll("#job-model option")].length - 1,
    datasetOptions: [...document.querySelectorAll("#job-dataset option")].length - 1,
  })],
  ["jobs", "我的任务", () => ({ jobRows: document.querySelectorAll(".data-table tbody tr").length })],
  ["comparison", "对比分析", () => ({})],
  ["models", "我的模型", () => ({ endpoints: document.querySelectorAll(".resource-row").length })],
  ["datasets", "共享数据集", () => ({
    datasetRows: document.querySelectorAll(".data-table tbody tr").length,
  })],
];

for (const [name, link, measure] of pages) {
  await page.getByRole("link", { name: link }).click();
  await page.waitForLoadState("networkidle");
  try {
    // "attached", not the default "visible": an <option> inside a closed <select> is
    // never visible, which would report a rendered page as empty.
    await page.waitForSelector(SETTLED[name], { state: "attached", timeout: 20000 });
  } catch {
    problems.push(`${name} never showed content or an empty state`);
  }
  await page.screenshot({ path: `${OUT}-${name}.png` });
  Object.assign(measurements, await page.evaluate(measure));
}

await page.getByRole("link", { name: "我的模型" }).click();
await page.getByRole("button", { name: "新建模型端点" }).click();
await page.waitForSelector(".modal");
await page.screenshot({ path: `${OUT}-model-modal.png` });

// A row of buttons must share a top edge and a height, whatever their styling.
const buttons = await page.evaluate(() =>
  [...document.querySelectorAll(".modal-actions button")].map((el) => {
    const r = el.getBoundingClientRect();
    return { label: el.textContent.trim(), top: +r.top.toFixed(1), height: +r.height.toFixed(1) };
  }),
);
const aligned =
  buttons.length > 1 &&
  buttons.every((b) => b.top === buttons[0].top && b.height === buttons[0].height);
if (!aligned) {
  problems.push(`modal buttons are not aligned: ${JSON.stringify(buttons)}`);
}

console.log(JSON.stringify({ measurements, buttonsAligned: aligned, problems }, null, 2));
await browser.close();
process.exit(problems.length === 0 ? 0 : 1);
