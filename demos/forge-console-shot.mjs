// forge-console-shot.mjs — screenshot the destroy-approval dialog.
//
// Loads the static console UI directly from disk (file://), then injects a
// MOCK `destroy_approval_required` gate payload and invokes the page's own
// destroyGate() renderer — so we capture the real red destructive-apply
// dialog without a running server, Azure creds, or a session token.
//
// Run with:  node demos/forge-console-shot.mjs
// Output:    docs/assets/destroy-approval-dialog.png
//
// Requires Chromium:  npx playwright install chromium
//
// SANITIZATION: the page renders only the mock payload below (generic Azure
// resource addresses) — no home path, no personal data. Eyeball the PNG.

import { chromium } from 'playwright';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..');
const indexHtml = resolve(repoRoot, 'installer', 'static', 'index.html');
const outPng = resolve(repoRoot, 'docs', 'assets', 'destroy-approval-dialog.png');

// A representative destroy-aware gate payload — the exact shape app.py returns
// (error: "destroy_approval_required") and index.html's destroyGate() renders.
const GATE = {
  error: 'destroy_approval_required',
  message:
    'The saved plan would DELETE or REPLACE 3 resources in environment "dev". ' +
    'This is irreversible. Approve only if you mean to tear these down.',
  approval_token: 'approve-destroy',
  destroyed: [
    'azurerm_postgresql_flexible_server.agent_memory',
    'azurerm_key_vault.platform',
    'azurerm_container_app.model_router',
  ],
};

const browser = await chromium.launch();
try {
  const page = await browser.newPage({
    viewport: { width: 1200, height: 800 },
    deviceScaleFactor: 2,
  });

  // Stub fetch BEFORE the page's bootstrap IIFE runs. Without a server every
  // request would 401 and the page would swap its body for an "Unauthorized"
  // message — neutering fetch (it never resolves) keeps the real console chrome
  // so we can drive the dialog over it.
  await page.addInitScript(() => {
    window.fetch = () => new Promise(() => {});
    window.EventSource = function () { return { close() {}, addEventListener() {} }; };
  });

  await page.goto(pathToFileURL(indexHtml).href, { waitUntil: 'domcontentloaded' });

  await page.evaluate((gate) => {
    document.querySelectorAll('dialog').forEach((d) => { try { d.close(); } catch {} });
    // Pre-fill a plausible (but inert) approval token to make the dialog read
    // as a real moment of decision in the screenshot.
    destroyGate('dev', gate);
    const input = document.getElementById('destroyInput');
    if (input) input.value = 'approve-destroy';
  }, GATE);

  await page.waitForSelector('#destroyDialog[open]', { timeout: 5000 });
  await page.waitForTimeout(250);

  await page.screenshot({ path: outPng });
  console.log('wrote ' + outPng.replace(repoRoot + '/', ''));
} finally {
  await browser.close();
}
