// tenant-console-shot.mjs — screenshot the DEMO tenant console.
//
// Loads the static demo console from disk (file://) and captures it with the
// detail drawer open on a sample tenant. No server, no creds, no network — the
// page ships its own inline fixtures.
//
// Run with:  node demos/tenant-console-shot.mjs
// Output:    demos/tenant-console/console-shot.png
//
// Requires Chromium:  npx playwright install chromium
//
// SANITIZATION: the page renders only fictional inline fixtures (invented
// tenants, all feature flags OFF) — no home path, no personal data. Still,
// eyeball the PNG before committing.

import { chromium } from 'playwright';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..');
const indexHtml = resolve(here, 'tenant-console', 'index.html');
const outPng = resolve(here, 'tenant-console', 'console-shot.png');

const browser = await chromium.launch();
try {
  const page = await browser.newPage({
    viewport: { width: 1280, height: 860 },
    deviceScaleFactor: 2,
  });

  await page.goto(pathToFileURL(indexHtml).href, { waitUntil: 'domcontentloaded' });

  // Open the detail drawer on a sample tenant so the shot shows the packs,
  // read-only feature flags, and budget view — the point of the demo.
  await page.evaluate(() => openDrawer('cascade-hvac'));
  await page.waitForSelector('#drawer.open', { timeout: 5000 });
  await page.waitForTimeout(300);

  await page.screenshot({ path: outPng });
  console.log('wrote ' + outPng.replace(repoRoot + '/', ''));
} finally {
  await browser.close();
}
