// self-check for Button loading (TP-174): a busy button keeps its width, so (1) Button renders its children in every
// state and only hides them (visibility, which keeps their box) under the spinner, and (2) no <Button> swaps its label
// for a "…중…" text while busy. Static, like settings.check.mjs. `npm run check`
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const fail = (m) => { throw new Error("button check failed: " + m); };
const btn = readFileSync(new URL("./Button.tsx", import.meta.url), "utf8");
if (!/<span className=\{cn\("contents", loading && "invisible"\)\}>\{children\}<\/span>/.test(btn))
  fail("Button.tsx must always render {children} in a `contents` span that only turns `invisible` while loading");
if (!/disabled=\{disabled \|\| loading\}/.test(btn) || !/aria-busy=\{loading \|\| undefined\}/.test(btn))
  fail("Button.tsx must be disabled and aria-busy while loading");

const src = new URL("..", import.meta.url).pathname;
const files = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? files(join(d, f)) : /\.tsx$/.test(f) ? [join(d, f)] : []));
const bad = [];
for (const f of files(src)) {
  const text = readFileSync(f, "utf8");
  for (const m of text.matchAll(/<(Button|ConfirmButton)\b[\s\S]*?<\/\1>/g)) {
    // a busy label: `x ? "…하는 중…" : "…"` (or the ellipsis in either branch) inside the button
    if (/\?\s*["`][^"`]*…["`]\s*:/.test(m[0])) bad.push(`${f.slice(src.length)}:${text.slice(0, m.index).split("\n").length}`);
  }
}
if (bad.length) fail("label swapped while busy, use <Button loading> instead: " + bad.join(", "));
console.log("button check ok");
