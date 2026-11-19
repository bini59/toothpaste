// From web/: npx playwright-core install chromium && npm run check:lab-focus
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright-core";
import { preview } from "vite";

test("keyboard navigation continues after a lab control disappears", async (t) => {
  const server = await preview({ configFile: false, preview: { host: "127.0.0.1", port: 0 } });
  t.after(() => server.close());
  const browser = await chromium.launch({ args: ["--enable-unsafe-swiftshader"] });
  t.after(() => browser.close());
  const page = await browser.newPage({ reducedMotion: "no-preference" });
  const responses = {
    "/api/lan/me": { local: true },
    "/api/state": {
      tickets: [], runs: [], prs: [], reviews: [], releases: [], events: [], repos: [],
      project_map: [], staging_diffs: [], meta: { version: {}, rules: {} },
    },
    "/api/projects": { projects: [], notifications_unread: 0 },
    "/api/themes": { themes: [] },
    "/api/config": { agent_models: {} },
    "/api/agents/accounts": { tools: {} },
  };
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: responses[path] ?? {} });
  });
  await page.goto(new URL("#model-lab", server.resolvedUrls.local[0]).href);
  const lab = page.getByRole("region", { name: "모델 선택 실험실" });
  const button = (name) => lab.getByRole("button", { name, exact: true });
  const skip = button("이동 건너뛰기");
  const takeBook = button("책 꺼내기");
  const hasFocus = (control) => control.evaluate((el) => el === document.activeElement);
  await lab.locator('[data-renderer="ready"]').waitFor();

  await button("Anthropic · 도서관으로").press("Enter");
  await skip.press("Enter");
  await skip.waitFor({ state: "detached" });
  assert.ok(await hasFocus(button("Fable")), "skipping the flight focuses the current model");
  await page.keyboard.press("ArrowDown");
  assert.ok(await hasFocus(button("Opus")), "the next arrow key reaches Opus");
  await page.keyboard.press("Escape");
  await takeBook.press("Enter");
  await takeBook.waitFor({ state: "detached" });
  assert.ok(await hasFocus(button("Opus")), "opening the book keeps its model focused");
  await page.keyboard.press("Escape");
  await takeBook.waitFor();
  await lab.locator('[data-scene="shelf"][data-traveling="false"]').waitFor();

  // Let the book open while focus stays on Skip, then leave the lab during the return trip.
  await takeBook.press("Enter");
  await skip.focus();
  await skip.waitFor({ state: "detached" });
  assert.ok(await hasFocus(button("Opus")), "natural arrival also restores focus");
  await page.keyboard.press("Escape");
  await skip.waitFor();
  const search = page.getByRole("button", { name: /^검색/ });
  await search.focus();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await skip.waitFor({ state: "detached" });
  assert.ok(await hasFocus(search), "arrival preserves focus outside the lab");

  await button("글자를 따라 Gemini로").press("Enter");
  const next = button("다음 모델 또는 장면");
  await next.press("End");
  assert.ok(await next.isDisabled());
  assert.ok(await hasFocus(button("Ar Argon")), "disabling Next on the last atom focuses its model");
  await page.keyboard.press("ArrowUp");
  assert.ok(await hasFocus(button("Ne Neon")), "the next arrow key reaches Neon");
});
