import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const routes = ["metrics", "dispatch", "reviews", "deploy", "cleanup", "log", "jobs", "history", "chat-compactions"];
const hrefs = (html) => [...html.matchAll(/href="#([^"]+)"/g)].map(([, href]) => href);

// Render the actual hash routes without starting a dashboard or fetching state.
globalThis.location = { hash: "#monitoring" };
globalThis.localStorage = { getItem: () => null };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });

const vite = await createServer({ appType: "custom", server: { middlewareMode: true, ws: false, watch: null } });
try {
  const { default: App } = await vite.ssrLoadModule("/src/App.tsx");
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
      assert(links.every((href) => !["monitoring", ...routes].includes(href)));
    }
    assert.deepEqual(hrefs(pinned), ["monitoring", "settings", "help"]);
    assert.match(pinned, /<a\b[^>]+href="#monitoring"[^>]+aria-current="page"/);
  }
  for (const route of routes) {
    const html = render(`#${route}`, false);
    assert.match(html, /<a\b[^>]+href="#monitoring"[^>]+aria-current="location"/);
    assert.match(html, /<header\b[^]*?href="#monitoring"[^>]*>모니터링<\/a>/);
  }
  console.log("monitoring navigation check ok");
} finally {
  await vite.close();
}
