// Headless smoke test of the full workflow.
// Usage: npm run build && npx vite preview --port 4173 &
//        CHROMIUM=/path/to/chromium-or-headless_shell node tests/e2e.mjs [baseUrl]
import { chromium } from "playwright-core";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const base = process.argv[2] ?? "http://localhost:4173/";
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mc-e2e-"));
const fail = (m) => {
  console.error("FAIL:", m);
  process.exitCode = 1;
};

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM,
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 }, acceptDownloads: true });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));

await page.goto(base);
await page.waitForSelector("[data-group]");

// 1. presets + fidelity on every demo product
for (const id of ["executive-desk-x", "bench-workstation", "lounge-chair-tub", "lounge-chair-leather"]) {
  await page.click(`[data-product="${id}"]`);
  await page.waitForTimeout(1200);
  await page.click(".presets .pill >> nth=0");
  await page.waitForTimeout(500);
  await page.click("text=Fidelity test");
  await page.waitForSelector("[data-testid=fidelity]");
  await page.waitForTimeout(1500);
  const rows = await page.$$eval("[data-testid=fidelity] tbody tr", (els) => els.map((e) => [...e.children].map((c) => c.textContent.trim()).join(" ")));
  console.log(`fidelity ${id}:`, rows.join(" | "));
}

// 2. upload a photo, auto-detect, configure
await page.setInputFiles('input[type=file]', path.join(here, "..", "public", "products", "lounge-chair-leather.jpg"));
await page.waitForSelector("text=3 materials");
await page.click("text=3 materials");
await page.waitForTimeout(1000);
const kinds = await page.$$eval(".parts li select", (els) => els.map((e) => e.value));
console.log("auto-detected kinds:", kinds.join(", "));
if (kinds.length < 2) fail("auto-detect produced fewer than 2 parts");
await page.click("text=Done → configure");
await page.waitForTimeout(800);
for (const g of await page.$$eval("[data-group]", (els) => els.map((e) => e.getAttribute("data-group")))) {
  await page.click(`[data-group="${g}"]`);
  await page.click(".lib-scroll .grid .swatch >> nth=2");
}

// 3. export PNG with embedded configuration
await page.click("[data-testid=open-export]");
await page.waitForSelector("[data-testid=export-preview]");
await page.waitForTimeout(1500);
const [dl] = await Promise.all([page.waitForEvent("download"), page.click("[data-testid=download-image]")]);
const out = path.join(tmp, dl.suggestedFilename());
await dl.saveAs(out);
const png = fs.readFileSync(out);
const meta = png.indexOf("material-configuration");
if (meta < 0) fail("exported PNG has no embedded configuration");
else console.log("export ok:", dl.suggestedFilename(), `${(png.length / 1024).toFixed(0)} KB, codes embedded`);
await page.click(".modal header .icon-btn");

// 4. restore the configuration from the exported PNG
const before = (await page.textContent("[data-testid=config-code]")).replace(/\s+/g, " ");
await page.click('[data-product="executive-desk-x"]');
await page.waitForTimeout(800);
await page.setInputFiles('input[type=file]', out);
await page.waitForTimeout(2000);
const after = (await page.textContent("[data-testid=config-code]")).replace(/\s+/g, " ");
if (before !== after) fail(`restore mismatch: ${before} vs ${after}`);
else console.log("restore ok:", after);

if (errors.length) fail("page errors: " + errors.join("; "));
await browser.close();
console.log(process.exitCode ? "E2E FAILED" : "E2E PASSED");
