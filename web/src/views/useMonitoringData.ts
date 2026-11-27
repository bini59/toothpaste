import { useEffect, useState } from "react";
import { getJson } from "../api";
import type { Role } from "../lib";

export function useMonitoringData<T>(path: string, syncing: boolean, role?: Role) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (syncing) return;
    let request: AbortController | undefined;
    const load = async () => {
      request?.abort();
      request = new AbortController();
      const { signal } = request;
      try {
        const next = await getJson<T>(path, signal);
        if (!signal.aborted) {
          setData(next);
          setError("");
        }
      } catch (e) {
        if (!signal.aborted) setError(String(e));
      }
    };
    void load();
    const timer = setInterval(load, 60000);
    return () => {
      clearInterval(timer);
      request?.abort();
    };
  }, [path, syncing, role]);

  return { data, error };
}
