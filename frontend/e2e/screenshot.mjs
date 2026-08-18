/**
 * Take screenshots of the main pages for visual review.
 *
 * Unit tests cannot see layout, so this is the loop that catches alignment and spacing.
 *   BASE=http://host:port OUT=/tmp/aisbench node e2e/screenshot.mjs
 */
import { chromium } from "@playwright/test";

const BASE = process.env.BASE ?? "http://127.0.0.1:8077";
const OUT = process.env.OUT ?? "/tmp/aisbench";
const USER = process.env.WEB_USER ?? "alice";
const PASSWORD = process.env.WEB_PASSWORD ?? "";

const browser = await chromium.launch();
const page = await browser.newPage({ viewportSize: { width: 1280, height: 820 } });

// A blank list is either a slow fetch or a real failure; record enough to tell them apart.
const problems = [];
page.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text()}`));
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("requestfailed", (r) => problems.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));
page.on("response", (r) => {
  if (r.url().includes("/api/") && r.status() >= 400) {
    problems.push(`http ${r.status()}: ${r.url()}`);
  }
});

await page.goto(BASE, { waitUntil: "networkidle" });
await page.getByLabel("用户名").fill(USER);
await page.getByLabel("密码").fill(PASSWORD);
await page.getByRole("button", { name: "登录" }).click();
await page.getByRole("link", { name: "我的模型" }).waitFor();

for (const [name, link] of [
  ["new-job", "新建评测"],
  ["jobs", "我的任务"],
  ["comparison", "对比分析"],
  ["models", "我的模型"],
  ["datasets", "共享数据集"],
]) {
  await page.getByRole("link", { name: link }).click();
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}-${name}.png` });
}

await page.getByRole("link", { name: "新建评测" }).click();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${OUT}-new-job.png` });
await page.getByRole("link", { name: "我的模型" }).click();
await page.getByRole("button", { name: "新建模型端点" }).click();
await page.waitForSelector(".modal");
await page.screenshot({ path: `${OUT}-model-modal.png` });

// Measure rather than only look: a row of buttons must share a top edge and a height.
const rows = await page.evaluate(() => {
  const measure = (selector) =>
    [...document.querySelectorAll(selector)].map((el) => {
      const r = el.getBoundingClientRect();
      return { label: el.textContent.trim(), top: +r.top.toFixed(1), height: +r.height.toFixed(1) };
    });
  return { modalActions: measure(".modal-actions button") };
});
const counts = await page.evaluate(() => ({
  modelOptions: document.querySelectorAll("#job-model option").length,
  datasetRows: document.querySelectorAll(".data-table tbody tr").length,
}));
console.log(JSON.stringify({ ...rows, counts, problems }, null, 2));
await browser.close();
