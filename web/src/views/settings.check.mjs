// TP-172: every key in a settings menu list must have content in its own editor.
// Static, since the editors need the whole dashboard state to render. `npm run check` (plain JS: tsc has no node types)
import { readFileSync } from "node:fs";

for (const [file, name] of [["GlobalSettings.tsx", "GLOBAL_SECTIONS"], ["RepoSettings.tsx", "REPO_SECTIONS"]]) {
  const src = readFileSync(new URL(`./settings/${file}`, import.meta.url), "utf8");
  const body = src.match(new RegExp(`const ${name}: SectionDef\\[\\] = \\[([\\s\\S]*?)\\n\\];`))?.[1] ?? "";
  const keys = [...body.matchAll(/^\s*\["(\w+)"/gm)].map((match) => match[1]);
  if (!keys.length) throw new Error(`settings check failed: ${name} not found in ${file}`);
  const missing = keys.filter((key) => !src.includes(`shown === "${key}"`));
  if (missing.length) throw new Error(`settings check failed: ${name} menu without content: ${missing.join(", ")}`);
  console.log(`settings check ok: ${name}`);
}
