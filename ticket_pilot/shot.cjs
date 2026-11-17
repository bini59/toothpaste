// Screenshot via the target repo's Playwright (Orca's device emulation renders blank/tiled frames).
// Web security is off so a local dev server can call a remote API that has no CORS rule for localhost.
// usage: NODE_PATH=<repo>/node_modules node shot.cjs <url> <out.png> <device> <wait_ms> [js]
const { chromium, devices } = require("playwright");
const [url, out, device, wait, js] = process.argv.slice(2);
(async () => {
  if (!devices[device]) throw new Error(`unknown device: ${device}`);
  // fall back to installed Chrome when this Playwright version's browser isn't downloaded
  const opts = { args: ["--disable-web-security"] };
  const browser = await chromium.launch(opts).catch(() => chromium.launch({ ...opts, channel: "chrome" }));
  try {
    const { defaultBrowserType, ...ctx } = devices[device];
    const page = await (await browser.newContext(ctx)).newPage();
    await page.goto(url, { waitUntil: "load" });
    await page.waitForTimeout(Number(wait));
    if (js) { await page.evaluate(js); await page.waitForTimeout(1000); }
    await page.screenshot({ path: out });
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e.message || e); process.exit(1); });
