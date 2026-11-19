import type { AgentAccount, AgentModel } from "../../types";

export const providers = {
  agy: { name: "AGY" },
  claude: { name: "Anthropic" },
  codex: { name: "OpenAI" },
} as const;
export type Tool = keyof typeof providers;
export const tools: Tool[] = ["agy", "claude", "codex"];

const collections = {
  agy: ["Hydrogen", "Neon", "Argon"],
  claude: ["Fable", "Opus", "Sonnet", "Haiku"],
  codex: ["Luna", "Terra", "Sol", "Astra"],
} as const;
export type PrintName = (typeof collections.claude)[number];
export const artworks: Record<PrintName, { image: string; alt: string }> = {
  Fable: { image: "/model-lab/fable.webp", alt: "잎사귀 등불을 든 숲속 여우" },
  Opus: { image: "/model-lab/opus.webp", alt: "가을빛 아래 고토의 현을 연주하는 손" },
  Sonnet: { image: "/model-lab/sonnet.webp", alt: "매화 핀 강가에서 마주 보는 두루미 두 마리" },
  Haiku: { image: "/model-lab/haiku.webp", alt: "연잎 위에 맺힌 한 방울의 이슬" },
};
export function printFor(key: string) {
  return Object.hasOwn(artworks, key) ? artworks[key as PrintName] : undefined;
}

export type Sculpture = (typeof collections)[Tool][number] | "Other";
export type Floor = { key: string; name: string; sculpture: Sculpture; models: AgentModel[] };
export type Visit = { tool: Tool; key: string };
export const arrivals: Record<Tool, Visit> = {
  codex: { tool: "codex", key: "Sol" },
  claude: { tool: "claude", key: "Fable" },
  agy: { tool: "agy", key: "Hydrogen" },
};

/** A visit is a camera destination. Only explicit confirmation writes an execution model. */
export function zoomFrom(v: Visit | null): Visit | null {
  if (!v) return arrivals.codex;
  if (v.tool === "codex") return arrivals.claude;
  return v.tool === "claude" && v.key === "Haiku" ? arrivals.agy : null;
}
export function backFrom(tool: Tool): Visit | null {
  if (tool === "agy") return { tool: "claude", key: "Haiku" };
  return tool === "claude" ? { tool: "codex", key: "Terra" } : null;
}
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
  const match = (text: string) => {
    const words = text.toLowerCase().split(/[^a-z0-9]+/);
    return floors.find((floor) => words.includes(floor.key.toLowerCase()));
  };
  for (const model of models) {
    const floor = match(model.id) ?? match(model.name);
    if (floor) floor.models.push(model);
    else others.push({ key: "model/" + model.id, name: model.name, sculpture: "Other", models: [model] });
  }
  return [...floors, ...others];
}

/** Claude exposes CLI aliases, while AGY and Codex list models for the connected account. */
export function modelStatus(tool: Tool, account: AgentAccount | undefined, model: AgentModel | undefined, loading: boolean, hasCatalog: boolean) {
  if (loading) return "계정 확인 중";
  if (!account) return "계정 확인 필요";
  if (!account.installed) return "도구 미설치";
  if (!account.logged_in) return "로그인 필요";
  if (!model) return hasCatalog ? "계정 목록에 없음" : "목록 확인 필요";
  return tool === "claude" ? "계정 권한 미확인" : "사용 가능";
}
