// self-check for Settings.tsx (TP-172): every key in a settings menu list has a `shown === "<key>"` branch, so no menu opens empty.
// Static, since the editors need the whole dashboard state to render. `npm run check` (plain JS: tsc has no node types)
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("./Settings.tsx", import.meta.url), "utf8");
const lists = [...src.matchAll(/const (\w+_SECTIONS): SectionDef\[\] = \[([\s\S]*?)\n\];/g)];
if (lists.length < 2) throw new Error("settings check failed: GLOBAL_SECTIONS / REPO_SECTIONS not found in Settings.tsx");
const missing = lists.flatMap(([, name, body]) =>
  [...body.matchAll(/^\s*\["(\w+)"/gm)].map((m) => m[1]).filter((k) => !src.includes(`shown === "${k}"`)).map((k) => `${name}: ${k}`));
if (missing.length) throw new Error("settings check failed: menu without content: " + missing.join(", "));
console.log(`settings check ok: ${lists.map(([, n]) => n).join(", ")}`);
