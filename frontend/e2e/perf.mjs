import { chromium } from "@playwright/test";
const BASE = process.env.BASE ?? "http://127.0.0.1:8000";
// Chromium inherits the system proxy, which does not reach the service.
const browser = await chromium.launch({ args: ["--no-proxy-server"] });
const page = await browser.newPage({ viewportSize: { width: 1280, height: 900 } });

const requests = [];
page.on("request", (r) => r.url().includes("/api/") && requests.push({ url: new URL(r.url()).pathname, t: Date.now() }));

const t0 = Date.now();
await page.goto(BASE, { waitUntil: "networkidle" });
const loaded = Date.now() - t0;
await page.getByLabel("用户名").fill(process.env.WEB_USER ?? "alice");
await page.getByLabel("密码").fill(process.env.WEB_PASSWORD ?? "");
await page.getByRole("button", { name: "登录" }).click();
await page.getByRole("link", { name: "我的模型" }).waitFor();

const nav = {};
for (const [name, link] of [["新建评测","新建评测"],["我的任务","我的任务"],["共享数据集","共享数据集"],["我的模型","我的模型"]]) {
  const start = Date.now();
  await page.getByRole("link", { name: link }).click();
  await page.waitForLoadState("networkidle");
  nav[name] = Date.now() - start;
}

// How chatty is a page that is just sitting there?
requests.length = 0;
const idleStart = Date.now();
await page.getByRole("link", { name: "共享数据集" }).click();
await page.waitForTimeout(10000);
const idleDatasets = requests.filter((r) => r.t > idleStart).length;

requests.length = 0;
const jobsStart = Date.now();
await page.getByRole("link", { name: "我的任务" }).click();
await page.waitForTimeout(10000);
const idleJobs = requests.filter((r) => r.t > jobsStart).length;

const timing = await page.evaluate(() => {
  const n = performance.getEntriesByType("navigation")[0];
  const js = performance.getEntriesByType("resource").filter((r) => r.name.endsWith(".js"));
  return {
    ttfbMs: Math.round(n.responseStart - n.requestStart),
    domContentLoadedMs: Math.round(n.domContentLoadedEventEnd - n.startTime),
    jsBytes: js.reduce((a, r) => a + (r.encodedBodySize || 0), 0),
    jsLoadMs: Math.round(js.reduce((a, r) => a + r.duration, 0)),
  };
});

console.log(JSON.stringify({
  firstLoadMs: loaded, navigationMs: nav,
  requestsWhileIdle10s: { datasetsPage: idleDatasets, jobsPage: idleJobs },
  timing,
}, null, 2));
await browser.close();
