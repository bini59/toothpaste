import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { prerender } from "react-dom/static";
import { createServer } from "vite";

const routes = ["metrics", "dispatch", "reviews", "deploy", "cleanup", "log", "jobs", "history"];
const compactionRoutes = ["chat-compactions", "metrics/chat-compactions"];
const hrefs = (html) => [...html.matchAll(/href="#([^"]+)"/g)].map(([, href]) => href);

// Render the actual hash routes without starting a dashboard or fetching state.
globalThis.location = { hash: "#monitoring" };
globalThis.localStorage = { getItem: () => null };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });

const vite = await createServer({ appType: "custom", server: { middlewareMode: true, ws: false, watch: null } });
try {
  const { parseHash, parseProjectSub, projectHref, routeReplacement, PALETTE_TABS } = await vite.ssrLoadModule("/src/app/navigation.ts");
  const { default: App } = await vite.ssrLoadModule("/src/App.tsx");
  for (const hash of ["", "#unknown", "#toString", "#__proto__"]) {
    assert.equal(parseHash(hash).tab, "board");
  }
  assert.equal(parseHash("#jobs/%").sub, "%");
  for (const section of ["chat", "board", "milestones", "tickets", "settings"]) {
    const hash = projectHref("테스트 프로젝트", section, section === "settings" ? "repo 1" : "", "deploy");
    const { tab, sub, view } = parseHash(hash);
    assert.equal(tab, "projects");
    assert.equal(view, hash.slice(1).split("/").slice(0, 2).join("/"));
    assert.deepEqual(parseProjectSub(sub), {
      id: "테스트 프로젝트", section, conv: section === "settings" ? "repo 1" : "", part: section === "settings" ? "deploy" : "",
    });
  }
  assert.equal(parseHash("#projects/demo/settings/repo/deploy").scrollScope, "#projects/demo/settings");
  const projects = [{ id: "demo", unsorted: false, repos: [{ name: "demo-repo" }] }];
  assert.equal(routeReplacement(parseHash("#projects/demo-repo/settings"), projects), "#projects/demo/settings/demo-repo");
  assert.equal(routeReplacement(parseHash("#projects/demo-repo/chat/c1"), projects), "#projects/demo/chat/c1");
  assert.equal(routeReplacement(parseHash("#projects/demo/settings"), projects), undefined);
  assert.equal(routeReplacement(parseHash("#settings/history"), null), "#history");
  assert.equal(routeReplacement(parseHash(""), []), "#start");
  const compaction = PALETTE_TABS.find(({ label }) => label === "채팅 압축");
  assert.equal(compaction?.key, "metrics/chat-compactions");
  for (const route of compactionRoutes) {
    const { tab, sub, view } = parseHash(`#${route}`);
    assert.deepEqual({ tab, sub, view }, { tab: "metrics", sub: "chat-compactions", view: compaction.key });
  }

  const render = (hash, collapsed) => {
    location.hash = hash;
    localStorage.getItem = (key) => key === "tp.collapsed" && collapsed ? "1" : null;
    return renderToStaticMarkup(createElement(App, { me: {} }));
  };
  for (const collapsed of [false, true]) {
    const html = render("#monitoring", collapsed);
    const sidebar = html.match(/<aside\b.*?<\/aside>/s)[0];
    const main = html.match(/<main\b.*?<\/main>/s)[0];
    assert.deepEqual(hrefs(main), routes);
    const [expanded, rail, pinned] = [...sidebar.matchAll(/<nav\b.*?<\/nav>/gs)].map(([nav]) => nav);
    for (const nav of [expanded, rail]) {
      const links = hrefs(nav);
      assert.deepEqual(links.slice(0, 6), ["inbox", "board", "milestones", "office", "release", "projects"]);
      assert(links.every((href) => !["monitoring", ...routes, ...compactionRoutes].includes(href)));
    }
    assert.deepEqual(hrefs(pinned), ["monitoring", "settings", "help"]);
    assert.match(pinned, /<a\b[^>]+href="#monitoring"[^>]+aria-current="page"/);
  }
  for (const route of [...routes, ...compactionRoutes]) {
    const html = render(`#${route}`, false);
    assert.match(html, /<a\b[^>]+href="#monitoring"[^>]+aria-current="location"/);
    assert.match(html, /<header\b[^]*?href="#monitoring"[^>]*>모니터링<\/a>/);
  }
  for (const route of compactionRoutes) {
    location.hash = `#${route}`;
    const { prelude } = await prerender(createElement(App, { me: {} }));
    const html = await new Response(prelude).text();
    assert.match(html, /<main\b[^]*?<a\b[^>]+href="#metrics\/chat-compactions"[^>]+aria-current="page"/);
  }
  console.log("monitoring navigation check ok");
} finally {
  await vite.close();
}
