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
export const books = {
  Fable: {
    genre: "우화집",
    title: "등불을 든 여우",
    glyph: "숲",
    lines: ["해 질 녘 숲에서", "여우는 작은 잎 하나를 들었다.", "돌아가는 길이 그 안에 있었다."],
    image: "/model-lab/fable.webp",
  },
  Opus: {
    genre: "악보집",
    title: "현을 위한 소품",
    glyph: "현",
    lines: ["고요한 현 위에", "오후의 빛이 잠시 머문다.", "손을 떼고도 울림은 남는다."],
    image: "/model-lab/opus.webp",
  },
  Sonnet: {
    genre: "소네트 시집",
    title: "강가의 두 목소리",
    glyph: "빛",
    lines: [
      "물결의 빛 아래",
      "강은 두 목소리를 모은다.",
      "하나는 떠나고 하나는 남아",
      "같은 물가의 저녁을 읽는다.",
    ],
    image: "/model-lab/sonnet.webp",
  },
  Haiku: {
    genre: "하이쿠 시집",
    title: "비 갠 아침",
    glyph: "비",
    lines: ["아침 비 그쳐", "잎 하나 기울 때면", "연못에 파문"],
    image: "/model-lab/haiku.webp",
  },
} as const;
export function bookFor(key: string) {
  return Object.hasOwn(books, key) ? books[key as PrintName] : undefined;
}
export type Sculpture = (typeof collections)[Tool][number] | "Other";
export type Floor = { key: string; name: string; sculpture: Sculpture; models: AgentModel[] };
/** book is the volume being read, including on the return journey from its ink. */
export type Visit = { tool: Tool; key: string; book?: string };
export const arrivals: Record<Tool, Visit> = {
  codex: { tool: "codex", key: "Sol" },
  claude: { tool: "claude", key: "Fable" },
  agy: { tool: "agy", key: "Hydrogen", book: "Fable" },
};
export function zoomFrom(v: Visit | null): Visit | null {
  if (!v) return arrivals.codex;
  if (v.tool === "codex") return arrivals.claude;
  if (v.tool === "claude") return { tool: "agy", key: "Hydrogen", book: v.key };
  return null;
}
export function backFrom(v: Visit): Visit | null {
  if (v.tool === "agy") {
    const key = v.book ?? "Fable";
    return { tool: "claude", key, book: key };
  }
  if (v.tool === "claude") return v.book ? { tool: "claude", key: v.key } : { tool: "codex", key: "Terra" };
  return null;
}
export type Selection = { tool: Tool; id: string; name: string };
export const selectionKey = "tp.lab.model";

export function readSelection(value: string | null): Selection | null {
  try {
    const saved = JSON.parse(value ?? "null");
    if (
      saved &&
      tools.includes(saved.tool) &&
      typeof saved.id === "string" &&
      saved.id &&
      typeof saved.name === "string" &&
      saved.name
    )
      return { tool: saved.tool, id: saved.id, name: saved.name };
  } catch {
    /* Older or incomplete browser data. */
  }
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
export function modelStatus(
  tool: Tool,
  account: AgentAccount | undefined,
  model: AgentModel | undefined,
  loading: boolean,
  hasCatalog: boolean
) {
  if (loading) return "계정 확인 중";
  if (!account) return "계정 확인 필요";
  if (!account.installed) return "도구 미설치";
  if (!account.logged_in) return "로그인 필요";
  if (!model) return hasCatalog ? "계정 목록에 없음" : "목록 확인 필요";
  return tool === "claude" ? "계정 권한 미확인" : "사용 가능";
}
