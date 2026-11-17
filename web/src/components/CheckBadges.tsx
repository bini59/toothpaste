import { Badge, Button } from "../ui";
import { useState } from "react";
import type { CheckRow, PrChecks } from "../types";
import type { Status } from "../lib";

/** tp check status -> label suffix + badge color (pass green, fail red, flaky yellow, running / none grey) */
const LOOK: Record<CheckRow["status"], [string, Status]> = {
  pass: ["통과", "success"],
  flaky: ["flaky", "warning"],
  fail: ["실패", "danger"],
  timeout: ["시간 초과", "danger"],
  error: ["오류", "danger"],
  running: ["실행 중", "draft"],
  ci_pending: ["CI 대기", "draft"],
};

/** tp's own checks of the PR head (checks.py). Click a badge to read its log (GitHub CI: opens the run). */
export function CheckBadges({ checks }: { checks: PrChecks | undefined }) {
  const [open, setOpen] = useState<number | null>(null);
  const [log, setLog] = useState<string>("");
  if (!checks) return null;
  if (checks.state === "none") return <Badge status="draft">검사 없음</Badge>;
  if (!checks.rows.length) return <Badge status="draft">검사 대기</Badge>;
  const show = async (c: CheckRow) => {
    if (open === c.id) return setOpen(null);
    setOpen(c.id);
    setLog("불러오는 중…");
    try {
      const r = await fetch(`/api/check-log?id=${c.id}`);
      const j = await r.json();
      setLog(j.log || "(로그 없음)");
    } catch {
      setLog("로그를 읽지 못했어요");
    }
  };
  const cur = checks.rows.find((c) => c.id === open);
  return (
    <>
      {checks.rows.map((c) => {
        const [label, status] = LOOK[c.status] ?? [c.status, "draft"];
        const badge = (
          <Badge status={status}>
            검사 {c.name} {label}
          </Badge>
        );
        const title = `${c.cmd}${c.duration_s != null ? ` · ${Math.round(c.duration_s)}s` : ""}`;
        return c.log_path?.startsWith("http") ? (
          <a key={c.id} href={c.log_path} target="_blank" rel="noreferrer" title={title}>
            {badge}
          </a>
        ) : (
          <button key={c.id} type="button" title={title} aria-expanded={open === c.id} onClick={() => show(c)} className="cursor-pointer">
            {badge}
          </button>
        );
      })}
      {cur && (
        <div className="w-full">
          <div className="mt-1 flex items-center justify-between gap-2 text-xs text-text-muted">
            <span className="min-w-0 truncate font-mono">$ {cur.cmd}</span>
            <Button size="sm" variant="secondary" className="shrink-0 whitespace-nowrap" onClick={() => setOpen(null)}>
              닫기
            </Button>
          </div>
          {/* failures sit at the end of the log: open scrolled to the bottom */}
          <pre
            ref={(el) => {
              if (el) el.scrollTop = el.scrollHeight;
            }}
            className="mt-1 max-h-[320px] overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-grey-50 p-2.5 font-mono text-[11px] leading-relaxed text-text-main"
          >
            {log}
          </pre>
        </div>
      )}
    </>
  );
}

/** The owner ignores the tp checks of the head with a written reason (stored + logged): before the AI review
 * ("검사 무시": the head goes to the reviewer instead of back to the agent) or at the approval ("예외 승인"). */
export function OverrideForm({
  gate,
  message,
  label,
  confirmLabel,
  onConfirm,
  placeholder = "검사를 무시하는 사유…",
}: {
  placeholder?: string;
  gate: string;
  message: React.ReactNode;
  label: string;
  confirmLabel: string;
  onConfirm: (reason: string) => void;
}) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  if (!asking)
    return (
      <Button size="sm" variant="secondary" className="mt-1.5 w-full" onClick={() => setAsking(true)}>
        {label}
      </Button>
    );
  return (
    <div className="mt-1.5 rounded-lg border border-primary-200 bg-primary-50 p-2.5">
      <p className="text-xs leading-relaxed text-text-main">
        <b>{gate}</b>. {message} 사유는 티켓과 로그에 남아요.
      </p>
      <textarea
        aria-label={placeholder}
        rows={2}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder={placeholder}
        className="mt-2 w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text-main"
      />
      <div className="mt-2 flex gap-2">
        <Button
          size="sm"
          disabled={!reason.trim()}
          onClick={() => {
            setAsking(false);
            onConfirm(reason.trim());
          }}
        >
          {confirmLabel}
        </Button>
        <Button size="sm" variant="secondary" onClick={() => setAsking(false)}>
          취소
        </Button>
      </div>
    </div>
  );
}
