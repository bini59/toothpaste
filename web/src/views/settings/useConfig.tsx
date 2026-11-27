import { useCallback, useEffect, useRef, useState } from "react";
import { fetchConfig } from "../../api";
import type { ConfigState } from "../../types";
import { same } from "./values";

export function useConfig() {
  const [c, setC] = useState<ConfigState | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => fetchConfig().then((x) => { setC(x); setError(""); }).catch((e) => setError(String(e))), []);
  useEffect(() => void load(), [load]);

  // Refresh the snapshot while a trial is running so its result becomes visible.
  const running = c && Object.values(c.check_runs).some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [running, load]);
  return { c, error, load };
}

export function ConfigLoading({ error }: { error: string }) {
  return <p className="text-sm text-text-muted">{error ? `설정을 불러오지 못했어요: ${error}` : "불러오는 중…"}</p>;
}

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));

export function useConfigDraft<T extends object>(original: T) {
  const [draft, setDraft] = useState(() => clone(original));
  const previous = useRef(original);
  useEffect(() => {
    // A refresh (including a trial poll) should keep any edits in progress.
    const before = previous.current;
    previous.current = original;
    setDraft((current) => same(current, before) ? clone(original) : current);
  }, [original]);
  const edit = (update: (draft: T) => void) => setDraft((current) => {
    const next = clone(current);
    update(next);
    return next;
  });
  const reset = () => setDraft(clone(original));
  return { draft, edit, reset };
}
