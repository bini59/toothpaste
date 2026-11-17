// Themes (TP-157): GET /api/themes lists the built-in and user themes with their tokens already resolved over their base;
// applying one sets those tokens as inline CSS variables on :root (tokens.css stays the light default underneath).
import { useSyncExternalStore } from "react";
import { getJson } from "../api";

export type Theme = { id: string; file: string; name: string; base: "light" | "dark"; builtin: boolean; tokens: Record<string, string>; warnings: string[]; ok: boolean };
export type ThemeList = { themes: Theme[]; selected: string; user_dir: string };

let list: ThemeList | null = null;
let current = "light";
let applied: string[] = [];
let version = 0;
const listeners = new Set<() => void>();
const dark = matchMedia("(prefers-color-scheme: dark)");

export async function loadThemes(): Promise<ThemeList> {
  list = await getJson<ThemeList>("/api/themes");
  return list;
}

/** apply a theme id (auto = light or dark by the OS setting); an unknown or broken id falls back to light.
 * Before the list has loaded this does nothing: startTheme applies the saved one once it has. */
export function applyTheme(id: string) {
  if (!list) return;
  current = id;
  const want = id === "auto" ? (dark.matches ? "dark" : "light") : id;
  const t = list?.themes.find((x) => x.id === want && x.ok);
  const root = document.documentElement.style;
  applied.forEach((k) => root.removeProperty(k));
  applied = Object.entries(t?.tokens ?? {}).map(([k, v]) => (root.setProperty(`--${k}`, v), `--${k}`));
  document.documentElement.style.colorScheme = t?.base ?? "light";
  version++;
  listeners.forEach((f) => f());
}
dark.addEventListener("change", () => current === "auto" && applyTheme("auto"));

/** at startup: the saved theme; failures keep the tokens.css default */
export const startTheme = () => loadThemes().then((l) => applyTheme(l.selected), () => {});

export const themeVersion = () => version;
/** changes on every applyTheme: the 작업실 canvas redraws with it */
export const useThemeVersion = () =>
  useSyncExternalStore((f) => (listeners.add(f), () => void listeners.delete(f)), () => version);
