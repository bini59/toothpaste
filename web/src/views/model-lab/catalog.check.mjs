// Run with npm run check (Node 24 strips the imported TypeScript types).
import assert from "node:assert/strict";
import { Object3D, Texture } from "three";
import { modelsForTool } from "../../agentModels.ts";
import { backFrom, floorsFor, modelStatus, printFor, readSelection, zoomFrom } from "./catalog.ts";
import { createExhibitSelector } from "./exhibitSelection.ts";
import { highlight, world } from "./materials.ts";

const model = (id, name = id) => ({ id, name, efforts: [], default_effort: null });
const agy = [model("gemini-4-neon-high", "Gemini 4 Neon (High)")];
const catalog = { agy, codex: [model("gpt-6.2-sol"), model("gpt-6.1-sol"), model("legacy", "Terra"), model("solstice")] };
const loggedOut = { tools: { agy: { models: [], logged_in: false } } };
assert.deepEqual(modelsForTool("agy", catalog, loggedOut), []);
assert.equal(modelsForTool("agy", catalog), agy);
assert.equal(modelsForTool("codex", catalog, loggedOut), catalog.codex);
const floors = floorsFor("codex", catalog.codex);
assert.deepEqual(floors.map((f) => f.name), ["Luna", "Terra", "Sol", "Astra", "solstice"]);
assert.deepEqual(floors[0].models, []);
assert.equal(floors[1].models[0].id, "legacy");
assert.deepEqual(floors[2].models.map((m) => m.id), ["gpt-6.2-sol", "gpt-6.1-sol"]);
assert.equal(floors[4].sculpture, "Other");
assert.equal(floorsFor("agy", agy)[1].models[0].name, "Gemini 4 Neon (High)");
assert.deepEqual(floorsFor("claude", [model("opus"), model("fable")]).map((f) => f.name), ["Fable", "Opus", "Sonnet", "Haiku"]);
const connected = { installed: true, logged_in: true };
assert.equal(modelStatus("claude", connected, model("opus"), false, true), "계정 권한 미확인");
assert.equal(modelStatus("codex", connected, model("gpt-6.2-sol"), false, true), "사용 가능");
assert.equal(modelStatus("agy", connected, undefined, false, true), "계정 목록에 없음");
assert.equal(modelStatus("agy", connected, undefined, false, false), "목록 확인 필요");
assert.equal(modelStatus("claude", { ...connected, logged_in: false }, model("opus"), false, true), "로그인 필요");
assert.equal(modelStatus("codex", undefined, model("gpt-6.2-sol"), false, true), "계정 확인 필요");
for (const value of [null, "bad", "{}", '{"tool":"other","id":"x","name":"X"}', '{"tool":"codex","id":"","name":"X"}']) {
  assert.equal(readSelection(value), null);
}
const saved = { tool: "codex", id: floors[2].models[1].id, name: floors[2].models[1].name };
assert.deepEqual(readSelection(JSON.stringify(saved)), saved);
// Zooming into the Haiku picture and confirming Claude Haiku are independent choices.
const solar = zoomFrom(null);
assert.deepEqual(solar, { tool: "codex", key: "Sol" });
assert.deepEqual(zoomFrom(solar), { tool: "claude", key: "Fable" });
const haiku = { tool: "claude", key: "Haiku" };
assert.deepEqual(zoomFrom(haiku), { tool: "agy", key: "Hydrogen" });
assert.deepEqual(backFrom("agy"), haiku);
assert.deepEqual(backFrom("claude"), { tool: "codex", key: "Terra" });
assert.equal(backFrom("codex"), null);
assert.equal(zoomFrom({ tool: "claude", key: "Opus" }), null);
assert.equal(zoomFrom({ tool: "agy", key: "Hydrogen" }), null);
assert.equal(readSelection(JSON.stringify(haiku)), null);
for (const name of ["Fable", "Opus", "Sonnet", "Haiku"]) assert.ok(printFor(name)?.image);
assert.equal(printFor("toString"), undefined);

// Build the Three.js scene objects with a stand-in for the label's 2D canvas.
const labels = [];
const context = {
  clearRect() {},
  fillText(text) { labels.push(text); },
};
globalThis.document = { createElement: () => ({ getContext: () => context }) };
const paint = { color: () => "white", haze: new Texture() };
for (const tool of ["codex", "agy", "claude"]) {
  const entries = floorsFor(tool, [model("long-model", "Preview"), model("another-model", "Preview")]);
  const [first, second] = entries.slice(-2);
  const w = world();
  const named = new Object3D();
  highlight(w, entries[0].key, named);
  const select = createExhibitSelector(w, paint);
  select(entries[0]);
  assert.ok(named.visible);
  select(first);
  const [marker] = w.hits;
  assert.equal(marker.parent, w.root);
  assert.ok(marker.visible && marker.children.some((child) => child.isMesh));
  assert.ok(marker.children.some((child) => child.isSprite && child.material.map?.isCanvasTexture));
  assert.equal(marker.userData.choice, first.key);
  assert.equal(named.visible, false);
  assert.equal(labels.at(-1), "long-model");
  select(second);
  assert.equal(w.hits.length, 1);
  assert.equal(marker.userData.choice, second.key);
  assert.equal(labels.at(-1), "another-model");
  select(entries[0]);
  assert.ok(named.visible && !marker.visible);
  select(first);
  assert.ok(marker.visible && !named.visible);
  select();
  assert.equal(marker.visible, false);
}
delete globalThis.document;
console.log("model lab catalog, zoom route and 3D exhibit selection ok");
