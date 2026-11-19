import type { AgentModel } from "../../types";

export const providers = {
  agy: { name: "AGY", material: "금속 원소 타일" },
  claude: { name: "Claude", material: "접힌 종이 조형" },
  codex: { name: "OpenAI", material: "유리 천체" },
} as const;
export type Tool = keyof typeof providers;
export const tools: Tool[] = ["agy", "claude", "codex"];

const collections = {
  agy: ["Hydrogen", "Neon", "Argon"],
  claude: ["Haiku", "Sonnet", "Opus", "Fable"],
  codex: ["Luna", "Terra", "Sol", "Astra"],
} as const;
export type Sculpture = (typeof collections)[Tool][number] | "Other";
export type Floor = { key: string; name: string; sculpture: Sculpture; models: AgentModel[] };
export type Visit = { tool: Tool; key: string };
export type Selection = { tool: Tool; id: string; name: string };
export const selectionKey = "tp.lab.model";

export function readSelection(value: string | null): Selection | null {
  try {
    const saved = JSON.parse(value ?? "null");
    if (saved && tools.includes(saved.tool) && typeof saved.id === "string" && saved.id &&
        typeof saved.name === "string" && saved.name) return { tool: saved.tool, id: saved.id, name: saved.name };
  } catch { /* Older or incomplete browser data. */ }
  return null;
}

/** An exhibit is a model family. Multiple catalog versions/efforts retain their exact CLI IDs at that stop. */
export function floorsFor(tool: Tool, models: AgentModel[]): Floor[] {
  const floors: Floor[] = collections[tool].map((name) => ({ key: name, name, sculpture: name, models: [] }));
  const others: Floor[] = [];
  for (const model of models) {
    const words = (text: string) => text.toLowerCase().split(/[^a-z0-9]+/);
    const match = (tokens: string[]) => floors.find((floor) => tokens.includes(floor.key.toLowerCase()));
    const floor = match(words(model.id)) ?? match(words(model.name));
    if (floor) floor.models.push(model);
    else others.push({ key: "model/" + model.id, name: model.name, sculpture: "Other", models: [model] });
  }
  return [...floors, ...others];
}
