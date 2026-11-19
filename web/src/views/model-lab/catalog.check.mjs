// Run with npm run check (Node 24 strips the imported TypeScript types).
import assert from "node:assert/strict";
import { modelsForTool } from "../../agentModels.ts";
import { floorsFor, readSelection } from "./catalog.ts";

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
assert.deepEqual(floorsFor("claude", [model("opus"), model("fable")]).map((f) => f.name), ["Haiku", "Sonnet", "Opus", "Fable"]);
for (const value of [null, "bad", "{}", '{"tool":"other","id":"x","name":"X"}', '{"tool":"codex","id":"","name":"X"}']) {
  assert.equal(readSelection(value), null);
}
const saved = { tool: "codex", id: floors[2].models[1].id, name: floors[2].models[1].name };
assert.deepEqual(readSelection(JSON.stringify(saved)), saved);
console.log("model lab catalog ok");
