export const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
/** The top-level keys edited here: orig is the page-load copy, and sending it whole would undo values saved
 * elsewhere since (the workspace picked in AsanaConnect, TP-148). */
export const changed = <T extends object>(d: T, orig: T) =>
  Object.fromEntries(Object.entries(d).filter(([k, v]) => !same(v, (orig as Record<string, unknown>)[k])));
/** number inputs: "" -> null, a number -> number, anything else stays text so the server can say why it is wrong */
export const num = (v: string): number | string | null => (v.trim() === "" ? null : Number.isFinite(Number(v)) ? Number(v) : v);
export const show = (v: unknown) => (v === null || v === undefined ? "없음" : typeof v === "string" ? v : JSON.stringify(v));
export const fmt = (v: string) => new Date(v).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

export const text = (v: unknown) => (v === null || v === undefined ? "" : String(v));
