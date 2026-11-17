import { cn } from "../ui";
import type { ClaudeUsage, CodexUsage, CodexWindow } from "../types";

const pad = (n: number) => String(n).padStart(2, "0");
const hhmm = (v?: string | null) => (v ? `${pad(new Date(v).getHours())}:${pad(new Date(v).getMinutes())}` : "");
/** reset instant: 월/일 only when it is not today */
const at = (ms: number | null) => {
  if (ms === null || !Number.isFinite(ms)) return "?";
  const d = new Date(ms);
  const day = d.toDateString() === new Date().toDateString() ? "" : `${d.getMonth() + 1}/${d.getDate()} `;
  return `${day}${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const windowName = (minutes: number | null) => {
  if (minutes === null || !Number.isFinite(minutes) || minutes <= 0) return "한도";
  if (minutes === 10080) return "주간";
  if (minutes % 1440 === 0) return `${minutes / 1440}일`;
  if (minutes % 60 === 0) return `${minutes / 60}시간`;
  return `${minutes}분`;
};
const REASON: Record<string, string> = {
  no_token: "Claude Code 토큰 없음",
  token_expired: "Claude Code 토큰 만료",
  unauthorized: "토큰 거부(401)",
  network: "네트워크 오류",
  bad_response: "응답 형식 변경",
  cli_missing: "Codex CLI 없음",
  login_required: "Codex 로그인 필요",
  unsupported: "지원하지 않는 CLI 또는 계정",
  unsupported_auth: "현재 인증은 한도 조회 미지원",
  timeout: "조회 시간 초과",
  account_changed: "계정 변경 · 재조회 대기",
  invalid_response: "사용량 응답 확인 불가",
  unavailable: "사용량 조회 불가",
};
/** one limit window: used percent (null = never read yet, drawn as an empty bar) and its reset instant */
type Meter = { id: string; label: string; used: number | null; resetAt: number | null };

function Bar({ m, collapsed }: { m: Meter; collapsed: boolean }) {
  const used = m.used === null ? null : Math.max(0, Math.min(100, m.used));
  const tone = used === null ? "" : used >= 95 ? "text-danger-700" : used >= 80 ? "text-warning-700" : "";
  // info blue, not primary: primary-500 (#ee1c24) and danger-500 (#cc1934) are indistinguishable in a 4px bar
  const fill = used === null ? "" : used >= 95 ? "bg-danger-500" : used >= 80 ? "bg-warning-500" : "bg-info-500";
  return (
    <div className="min-w-0 flex-1">
      <div className={cn("flex justify-between gap-1 text-[10px] leading-tight", collapsed && "lg:hidden")}>
        <span className="truncate text-text-muted">{m.label}</span>
        <span className={cn("min-w-[4ch] text-right tabular-nums", tone || "text-text-main")}>{used === null ? "-" : `${Math.round(used)}%`}</span>
      </div>
      <div
        role="progressbar"
        aria-label={`${m.label} 사용량`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={used ?? undefined}
        aria-valuetext={used === null ? "확인 전" : `${Math.round(used)}% 사용`}
        className="mt-0.5 h-1 overflow-hidden rounded-full bg-grey-200"
      >
        {used !== null && <div className={cn("h-full rounded-full", fill)} style={{ width: `${used}%` }} />}
      </div>
    </div>
  );
}

function Row({ name, meters, note, collapsed }: { name: string; meters: Meter[]; note: string; collapsed: boolean }) {
  // collapsed (72px aside): the name moves above the bars so they stay wide enough to read
  return (
    <div className={cn("flex items-center gap-2", collapsed && "lg:flex-col lg:items-stretch lg:gap-0.5")}>
      <span className={cn("w-14 shrink-0 text-[10px] font-semibold leading-tight text-text-main", collapsed && "lg:w-full")}>
        {name}
        {note && <span className={cn("block font-normal tabular-nums text-text-muted", collapsed && "lg:hidden")}>{note}</span>}
      </span>
      <div className="flex min-w-0 flex-1 gap-2">
        {meters.map((m) => <Bar key={m.id} m={m} collapsed={collapsed} />)}
      </div>
    </div>
  );
}

/** Sidebar footer (the Sidebar has no footer slot): fixed under the aside, which reserves the space with pb-20.
 *  TP-56: the bars always show the last stored values — no loading, refreshing or "run Claude Code" states. */
export function UsageCard({ claude, codex, collapsed, mobileOpen }: {
  claude: ClaudeUsage | null | undefined;
  codex: CodexUsage | null | undefined;
  collapsed: boolean;
  mobileOpen: boolean;
}) {
  const iso = (v: string | null | undefined) => (v ? new Date(v).getTime() : null);
  const cm: Meter[] = [
    { id: "5h", label: "5시간", used: claude?.five_hour?.utilization ?? null, resetAt: iso(claude?.five_hour?.resets_at) },
    { id: "7d", label: "7일", used: claude?.seven_day?.utilization ?? null, resetAt: iso(claude?.seven_day?.resets_at) },
  ];
  const cx = (codex?.buckets || []).flatMap((b) => b.windows) as CodexWindow[];
  const xm: Meter[] = (cx.length ? cx : [{ window: "primary", windowDurationMins: 300, remainingPercent: null, resetAt: null },
                                         { window: "secondary", windowDurationMins: 10080, remainingPercent: null, resetAt: null }] as CodexWindow[])
    .slice(0, 2)
    .map((w) => ({ id: w.window, label: windowName(w.windowDurationMins),
                   used: w.remainingPercent === null ? null : 100 - w.remainingPercent,
                   resetAt: w.resetAt === null ? null : w.resetAt * 1000 }));

  // older than 15 min = at least two missed 5-minute checks; stale values keep their bars and show the last check time
  const old = (u: ClaudeUsage | CodexUsage | null | undefined) =>
    !!u?.fetched_at && (!!u.error || Date.now() - new Date(u.fetched_at).getTime() > 15 * 60000);
  const why = (u: ClaudeUsage | CodexUsage | null | undefined) => (u?.error ? REASON[u.error] ?? u.error : "");
  const note = (u: ClaudeUsage | CodexUsage | null | undefined) => (old(u) ? `확인 ${hhmm(u!.checked_at)}` : "");
  const tip = (label: string, u: ClaudeUsage | CodexUsage | null | undefined, ms: Meter[]) =>
    [`${label} ${ms.map((m) => `${m.label} ${at(m.resetAt)} 리셋`).join(" · ")}`,
     u?.fetched_at ? `마지막 확인 ${hhmm(u.checked_at)}` : "아직 확인 전",
     why(u)].filter(Boolean).join(" · ");
  const title = `${tip("Claude", claude, cm)}\n${tip("Codex", codex, xm)}`;

  return (
    <div
      role="status"
      title={title}
      aria-label={title}
      className={cn(
        "fixed bottom-0 left-0 z-50 flex h-20 w-[260px] max-w-[85vw] flex-col justify-center gap-2 border-t border-r border-border bg-surface px-4 transition-[width,transform] duration-300",
        collapsed ? "lg:w-[72px] lg:px-2" : "lg:w-[260px]",
        mobileOpen ? "transform-none" : "[transform:translateX(-100%)] lg:transform-none",
      )}
    >
      <Row name="Claude" meters={cm} note={note(claude)} collapsed={collapsed} />
      <Row name="Codex" meters={xm} note={note(codex)} collapsed={collapsed} />
    </div>
  );
}
