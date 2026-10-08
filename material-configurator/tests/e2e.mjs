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
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));

await page.goto(base);
await page.waitForSelector("[data-group]");

// 1. 3D studio: presets, then an instant high-quality render with embedded codes
await page.click('[data-product="3d-lounge-chair"]');
await page.waitForTimeout(5000);
await page.click(".ideas .pill >> nth=1");
await page.waitForTimeout(2000);
await page.click("[data-testid=open-export]");
await page.click('.opt:has-text("Excel")');
await page.click("[data-testid=render-3d]");
await page.waitForSelector("[data-testid=export-preview]", { timeout: 60000 });
const [dl3d] = await Promise.all([page.waitForEvent("download"), page.click("[data-testid=download-image]")]);
const out3d = path.join(tmp, dl3d.suggestedFilename());
await dl3d.saveAs(out3d);
if (fs.readFileSync(out3d).indexOf("material-configuration") < 0) fail("3D export has no embedded configuration");
else console.log("3D export ok:", dl3d.suggestedFilename());
await page.click(".modal header .icon-btn");

// 2. photo products: preset + accuracy check
for (const id of ["executive-desk-x", "bench-workstation", "lounge-chair-tub", "lounge-chair-leather"]) {
  await page.click(`[data-product="${id}"]`);
  await page.waitForTimeout(2500);
  await page.click(".ideas .pill >> nth=0");
  await page.waitForTimeout(500);
  await page.click("[data-testid=more]");
  await page.click("text=Accuracy check");
  await page.waitForSelector("[data-testid=fidelity]");
  await page.waitForTimeout(1500);
  const rows = await page.$$eval("[data-testid=fidelity] tbody tr", (els) => els.map((e) => [...e.children].map((c) => c.textContent.trim()).join(" ")));
  console.log(`accuracy ${id}:`, rows.join(" | "));
}

// 3. upload a photo: parts are detected automatically
await page.setInputFiles("input[type=file]", path.join(here, "..", "public", "products", "lounge-chair-leather.jpg"));
await page.waitForSelector(".parts li", { timeout: 30000 });
await page.waitForTimeout(1000);
const kinds = await page.$$eval(".parts li select", (els) => els.map((e) => e.value));
console.log("auto-detected kinds:", kinds.join(", "));
if (kinds.length < 2) fail("auto-detect produced fewer than 2 parts");
await page.click("text=Save and start customising");
await page.waitForTimeout(1500);
for (const g of await page.$$eval("[data-group]", (els) => els.map((e) => e.getAttribute("data-group")))) {
  await page.click(`[data-group="${g}"]`);
  await page.click(".library .grid .swatch >> nth=2");
}

// 4. photo export with embedded configuration, then restore it from the PNG
await page.click("[data-testid=open-export]");
await page.waitForSelector("[data-testid=export-preview]");
await page.waitForTimeout(1500);
const [dl] = await Promise.all([page.waitForEvent("download"), page.click("[data-testid=download-image]")]);
const out = path.join(tmp, dl.suggestedFilename());
await dl.saveAs(out);
if (fs.readFileSync(out).indexOf("material-configuration") < 0) fail("exported PNG has no embedded configuration");
else console.log("photo export ok:", dl.suggestedFilename());
await page.click(".modal header .icon-btn");
const before = (await page.$$eval(".part-tab small", (els) => els.map((e) => e.textContent))).join("|");
await page.click('[data-product="executive-desk-x"]');
await page.waitForTimeout(800);
await page.setInputFiles("input[type=file]", out);
await page.waitForTimeout(2500);
const after = (await page.$$eval(".part-tab small", (els) => els.map((e) => e.textContent))).join("|");
if (before !== after) fail(`restore mismatch: ${before} vs ${after}`);
else console.log("restore ok:", after);

if (errors.length) fail("page errors: " + errors.join("; "));
await browser.close();
console.log(process.exitCode ? "E2E FAILED" : "E2E PASSED");
