import type { AutomationsResponse, ConfigChange, ConfigState, DevCheck, DevConflict, DevServer, State, SystemJobLog, SystemJobsResponse } from "./types";

export async function fetchState(): Promise<State> {
  const r = await fetch("/api/state");
  if (r.status === 401) location.reload(); // 로컬망 공유 session ended (TP-152): back to the login screen
  if (!r.ok) throw new Error(`state ${r.status}`);
  return r.json();
}
export async function fetchAutomations(): Promise<AutomationsResponse> {
  return (await fetch("/api/automations")).json();
}
export async function fetchSystemJobs(): Promise<SystemJobsResponse> {
  const r = await fetch("/api/system-automations");
  if (!r.ok) throw new Error(`system-automations ${r.status}`);
  return r.json();
}
export async function fetchSystemJobLog(id: string, i: number): Promise<SystemJobLog> {
  const r = await fetch(`/api/system-automations/log?id=${encodeURIComponent(id)}&i=${i}`);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `log ${r.status}`);
  return j;
}
export async function post(path: string, body: unknown): Promise<void> {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "실패");
}

/** /api/dev/start answered 409: the port is in use (processes listening there, a free port to offer instead) */
export class PortConflict extends Error {
  constructor(message: string, public info: DevConflict) {
    super(message);
  }
}
export async function devCheck(gid: string): Promise<DevCheck> {
  const r = await fetch("/api/dev/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gid }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "실패");
  return j as DevCheck;
}
export async function devStart(gid: string, port: number, kill: boolean): Promise<DevServer> {
  const r = await fetch("/api/dev/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gid, port, kill }) });
  const j = await r.json().catch(() => ({}));
  if (r.status === 409 && j.conflict) throw new PortConflict(j.error, j.conflict);
  if (!r.ok) throw new Error(j.error || "실패");
  return j.server as DevServer;
}

export class ConfigError extends Error {
  constructor(message: string, public errors: string[]) {
    super(message);
  }
}
export async function fetchConfig(): Promise<ConfigState> {
  const r = await fetch("/api/config");
  if (!r.ok) throw new Error(`config ${r.status}`);
  return r.json();
}
/** POST to the settings API; invalid values throw ConfigError with one line per problem. */
export async function postConfig<T = { changes: ConfigChange[] }>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new ConfigError(j.error || "실패", j.errors || [j.error || "실패"]);
  return j as T;
}


/** GET a JSON endpoint; the server's {error} becomes the thrown message */
export async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${path} ${r.status}`);
  return j as T;
}
/** POST that returns the response body (post() only reports failure) */
export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "실패");
  return j as T;
}
