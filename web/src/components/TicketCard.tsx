import { Badge, Button, Card } from "../ui";
import { useState } from "react";
import { DROPPABLE, PENDING, VERDICT, ago, isMs, isOverdue, liveReleaseOf, reposFor, ticketRepos, type Index } from "../lib";
import type { State, Ticket } from "../types";
import { CheckBadges, OverrideForm } from "./CheckBadges";
import { ConfirmButton } from "./ConfirmButton";
import { Select } from "./Select";
import { StageBadge } from "./StageBadge";

interface Props {
  t: Ticket;
  s: State;
  I: Index;
  onAssign: (gid: string, repo: string) => void;
  onSkip: (gid: string, skip: boolean) => void;
  onApprove: (gid: string, cancel: boolean, override?: string, overrideReview?: string) => void;
  onOverride: (gid: string, reason: string) => void;
  onReply: (gid: string, text: string) => void;
  onDrop: (gid: string) => void;
  /** shown inside its milestone's detail page: no link back to it */
  inMilestoneView?: boolean;
  /** 프로젝트 board (TP-91): badges with the ticket's repos */
  repoBadge?: boolean;
}

export function TicketCard({ t, s, I, onAssign, onSkip, onApprove, onOverride, onReply, onDrop, inMilestoneView, repoBadge }: Props) {
  const [reply, setReply] = useState("");
  const dropButton = (
    <ConfirmButton
      size="sm"
      variant="secondary"
      message="이 작업을 그만두고 티켓을 건너뜀으로 옮길까요? 에이전트 터미널을 닫고 리뷰·머지 대상에서 빠져요. 워크트리는 청소기가 곧 지우고(커밋 안 된 변경 포함), 원격 브랜치와 PR은 남아요. 원본 티켓을 닫으려면 직접 처리하세요."
      confirmLabel="그만두기"
      onConfirm={() => onDrop(t.gid)}
    >
      그만두기
    </ConfirmButton>
  );
  const run = (I.runBy[t.gid] || []).at(-1);
  const pr = [...(I.prBy[t.gid] || [])].sort((a, b) => b.number - a.number)[0];
  // the AI review of the current head, same as the host check (release.approval_target); tp check failures
  // (source 'check') show as the check badges
  const rv = pr && (I.revBy[pr.url] || []).find((v) => v.head_sha === pr.head_sha && !v.source);
  const rule = pr ? s.meta.rules?.[pr.repo] : undefined;
  // a milestone subtask merges into its milestone branch (automatically once the AI approves; milestone.py)
  const msBase = pr?.base?.startsWith("milestone/") ? pr.base : null;
  // staging approval: any repo whose deploy rule has a staging branch and the `staging` mode
  const stg = rule?.staging && rule.modes.includes("staging") ? (msBase ?? rule.staging.branch) : null;
  const milestoneOf = t.parent_gid && (t.in_milestone || isMs(t.gid)) ? t.parent_gid : null;
  const okReview = t.stage === "reviewed" && pr?.state === "OPEN" && rv?.verdict === "approve" && rv.head_sha === pr.head_sha;
  const approvable = !!stg && okReview;
  const baseOk = pr?.base === stg;
  const conflict = pr?.state === "OPEN" && pr.mergeable === "CONFLICTING";
  // tp check gate of the head (release.approval_target); the owner's override with a reason applies to that head only
  const overridden = !!t.check_override && !!pr && t.check_override_sha === pr.head_sha;
  const gate = overridden ? null : (pr?.checks?.gate ?? null);
  const approvalShown = approvable && baseOk && !conflict;
  // AI said blocked/changes on the head: the owner can approve that head with a reason (TP-99, release.request_approval)
  const reviewOverridden = !!t.review_override && !!pr && t.review_override_sha === pr.head_sha;
  const canOverrideReview =
    !!stg && pr?.state === "OPEN" && baseOk && !conflict && !gate &&
    // blocked: escalate_rework handed the fix loop to the owner; a rule violation (pipeline.VIOLATION) is not approvable
    (t.stage === "reviewed" || t.stage === "rework" || (t.stage === "blocked" && !run?.ws_comment?.startsWith("BLOCKED: 규칙 위반"))) &&
    (rv?.verdict === "blocked" || rv?.verdict === "changes");
  // failed (or uncheckable) head: the owner can send it to the AI review anyway; the approval itself comes after
  const canIgnore =
    !!gate && pr?.state === "OPEN" && !approvalShown && t.stage !== "approving" &&
    (pr.checks?.state === "fail" || pr.checks?.state === "error");
  const mergeMsg = (
    <>
      PR #{pr?.number}을 <b>{stg}</b>에 머지할까요?{" "}
      {msBase
        ? "마일스톤 PR과 함께 staging으로 가요. 승인하지 않아도 AI가 괜찮다고 하면 자동으로 머지돼요."
        : rule?.train
        ? "머지되면 다음 평일 10:00 프로덕션 배포에 포함돼요."
        : stg === rule?.prod.branch
          ? "머지되면 바로 프로덕션에 배포돼요."
          : "프로덕션은 배포 페이지에서 내보내요."}
    </>
  );
  const tracked = !!(rule?.prod.pipeline || rule?.prod.workflow || rule?.prod.release);
  const live = t.stage === "deploying" ? liveReleaseOf(s, t.gid) : undefined;
  const prodModes = rule ? rule.modes.filter((m) => m === "direct" || m === "promote") : [];
  const deps = (JSON.parse(t.deps_json || "[]") as { name: string; completed: boolean; landed?: string | null }[]).filter(
    (d) => !d.completed,
  );
  const options =
    t.stage === "needs_repo"
      ? reposFor(s, t.project_gid).map((r) => ({ v: r.repo!, l: `${r.repo} (${r.role})` }))
      : t.stage === "unmapped"
        ? s.repos.map((r) => ({ v: r.name, l: r.name }))
        : null;

  return (
    <Card className="p-3.5">
      <div className="flex flex-wrap items-center gap-1.5 text-xs font-medium text-text-muted">
        <a href={t.url} target="_blank" rel="noreferrer" className="text-primary hover:text-primary-700 hover:underline">
          {t.key}
        </a>
        <StageBadge stage={t.stage} />
        {t.due_on && <span className={isOverdue(t.due_on) ? "text-danger-700" : ""}>{t.due_on}</span>}
        {repoBadge && ticketRepos(s, I, t).map((r) => <Badge key={r} status="draft">{r}</Badge>)}
      </div>
      <div className="my-1.5 text-sm font-semibold leading-snug text-text-main">{t.name}</div>
      <div className="text-xs leading-relaxed text-text-muted">
        {t.project_name || "-"}
        {run ? (
          <>
            {" → "}
            <b className="font-semibold text-text-main">{run.repo}</b>/{run.worktree_name}
          </>
        ) : t.repo_override ? (
          ` → ${t.repo_override}`
        ) : null}
      </div>
      {run?.ws_comment && <div className="text-xs leading-relaxed text-text-muted">메모: {run.ws_comment}</div>}
      {milestoneOf && !inMilestoneView && (
        <a href={`#milestones/${encodeURIComponent(milestoneOf)}`} className="text-xs font-medium text-primary hover:text-primary-700 hover:underline">
          마일스톤: {t.parent_name || milestoneOf} →
        </a>
      )}
      {deps.length > 0 && (
        <div className="text-xs text-text-muted">
          선행: {deps.map((d) => (d.landed ? `${d.name} (${d.landed} 머지됨)` : d.name)).join(", ")}
        </div>
      )}
      {pr && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">
          <a href={pr.url} target="_blank" rel="noreferrer" className="text-primary hover:text-primary-700 hover:underline">
            PR #{pr.number}
          </a>
          <Badge status={pr.state === "MERGED" ? "success" : pr.state === "OPEN" ? "info" : "draft"}>{pr.state.toLowerCase()}</Badge>
          {!!pr.is_draft && <Badge>draft</Badge>}
          {rv && <Badge status={VERDICT[rv.verdict][1]}>{VERDICT[rv.verdict][0]}</Badge>}
          {conflict && <Badge status="danger">{pr.base ?? "base"} 충돌</Badge>}
          <CheckBadges checks={pr.checks} />
        </div>
      )}
      {overridden && <p className="mt-1.5 text-xs font-medium text-warning-700">검사 무시: {t.check_override}</p>}
      {reviewOverridden && <p className="mt-1.5 text-xs font-medium text-warning-700">AI 판정 무시: {t.review_override}</p>}
      {canOverrideReview && rv && (
        <OverrideForm
          gate={`AI 판정: ${VERDICT[rv.verdict][0]}`}
          label="사유 적고 승인…"
          confirmLabel="예외 승인"
          placeholder="AI 판정과 달리 승인하는 사유…"
          message={
            <>
              {mergeMsg} 이 커밋({pr?.head_sha?.slice(0, 7)})에만 적용되고, PR 코멘트에도 남겨요.
            </>
          }
          onConfirm={(reason) => onApprove(t.gid, false, undefined, reason)}
        />
      )}
      {canIgnore && gate && (
        <OverrideForm
          gate={gate}
          label="사유 적고 검사 무시…"
          confirmLabel="검사 무시"
          message={
            <>
              이 커밋({pr?.head_sha?.slice(0, 7)})은 에이전트에게 돌려보내지 않고 AI 리뷰로 넘어가요. 머지는 AI '괜찮음' 판정
              뒤 승인 버튼으로 해요.
            </>
          }
          onConfirm={(reason) => onOverride(t.gid, reason)}
        />
      )}
      {options && (
        <Select size="sm" className="mt-2.5 w-full" defaultValue="" onChange={(e) => e.target.value && onAssign(t.gid, e.target.value)}>
          <option value="">{t.stage === "needs_repo" ? "레포 선택…" : "레포 지정…"}</option>
          {options.map((o) => (
            <option key={o.v} value={o.v}>
              {o.l}
            </option>
          ))}
        </Select>
      )}
      {approvalShown && !gate && (
        <ConfirmButton
          size="sm"
          className="mt-2.5 w-full"
          message={mergeMsg}
          confirmLabel="머지 승인"
          onConfirm={() => onApprove(t.gid, false)}
        >
          {stg} 머지 승인
        </ConfirmButton>
      )}
      {approvalShown && gate && (
        <div className="mt-2.5">
          <Button size="sm" className="w-full" disabled>
            {stg} 머지 승인
          </Button>
          <p className="mt-1.5 text-xs font-medium text-danger-700">승인할 수 없어요: {gate}</p>
          <OverrideForm
            gate={gate}
            label="사유 적고 예외 승인…"
            confirmLabel="예외 승인"
            message={mergeMsg}
            onConfirm={(reason) => onApprove(t.gid, false, reason)}
          />
        </div>
      )}
      {approvable && !baseOk && (
        <p className="mt-2 text-xs text-text-muted">PR base가 {pr?.base ?? "?"}라 승인할 수 없어요 ({stg}만 가능)</p>
      )}
      {okReview && !stg && prodModes.length > 0 && (
        <a href="#release" className="mt-2 block text-xs font-medium text-primary hover:text-primary-700 hover:underline">
          배포 페이지에서 prod 배포 →
        </a>
      )}
      {live && (
        <p className="mt-2 text-xs text-text-muted">
          {live.mode ? `#${live.id} ${s.meta.modes?.[live.mode] ?? live.mode}` : "평일 배포"} · {live.step || live.status}
        </p>
      )}
      {t.stage === "approving" && (
        <div className="mt-2 flex items-center justify-between gap-2 text-xs text-text-muted">
          <span>승인됨 {ago(t.approve_requested_at)} · 5분 안에 {stg} 머지</span>
          <Button size="sm" variant="secondary" onClick={() => onApprove(t.gid, true)}>
            취소
          </Button>
        </div>
      )}
      {t.stage === "staged" && pr?.merged_at && (
        <p className="mt-2 text-xs text-text-muted">
          {pr.base} 머지 {ago(pr.merged_at)} ·{" "}
          {rule?.train ? (
            `${new Date(s.meta.next_release_at).toLocaleString("ko-KR", { month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit" })} 배포 예정`
          ) : (
            <a href="#release" className="font-medium text-primary hover:text-primary-700 hover:underline">배포 페이지에서 prod 반영 →</a>
          )}
        </p>
      )}
      {t.stage === "milestone" && pr?.merged_at && (
        <p className="mt-2 text-xs text-text-muted">{pr.base} 머지 {ago(pr.merged_at)} · 마일스톤 PR과 함께 배포돼요</p>
      )}
      {t.stage === "ms_hold" && (
        <p className="mt-2 text-xs text-text-muted">
          하위작업이 모두 들어왔어요.{" "}
          {inMilestoneView ? (
            "위의 '마일스톤 작업 완료'를 누르면 staging PR 리뷰로 넘어가요."
          ) : (
            <a href={`#milestones/${encodeURIComponent(t.parent_gid || "")}`} className="font-medium text-primary hover:text-primary-700 hover:underline">
              마일스톤 화면에서 작업 완료를 누르면 리뷰로 넘어가요 →
            </a>
          )}
        </p>
      )}
      {t.stage === "merged" && pr && (
        <p className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-text-muted">
          <Badge status="draft">{pr.base ?? "?"}</Badge>
          <span>
            머지 {ago(pr.merged_at)} ·{" "}
            {tracked ? "프로덕션 반영 확인 대기" : t.gid.startsWith("notion:") ? "배포 추적 미설정" : "배포 추적 미설정 (Asana 완료 시 완료 처리)"}
          </span>
        </p>
      )}
      {approvable && baseOk && conflict && (
        <p className="mt-2 text-xs font-medium text-danger-700">
          {pr?.base}와 충돌해서 승인할 수 없어요. 충돌을 풀고 재리뷰를 받으면 다시 승인할 수 있어요.
        </p>
      )}
      {t.approve_error && t.stage === "reviewed" && !conflict && <p className="mt-2 text-xs font-medium text-danger-700">머지 실패: {t.approve_error}</p>}
      {t.stage === "blocked" &&
        (t.reply ? (
          <div className="mt-2 flex items-center justify-between gap-2 text-xs text-text-muted">
            <span>답장 보냄 · 5분 안에 에이전트 재실행: {t.reply}</span>
            <Button size="sm" variant="secondary" onClick={() => onReply(t.gid, "")}>
              취소
            </Button>
          </div>
        ) : (
          <div className="mt-2.5 flex flex-col gap-1.5">
            <textarea
              aria-label="에이전트에게 답장"
              rows={2}
              value={reply}
              onChange={(e) => setReply(e.target.value)}
              placeholder="에이전트에게 답장…"
              className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text-main"
            />
            <div className="flex gap-1.5">
              <Button size="sm" disabled={!reply.trim()} onClick={() => { onReply(t.gid, reply); setReply(""); }}>
                답장하고 재실행
              </Button>
              {dropButton}
            </div>
          </div>
        ))}
      {DROPPABLE.includes(t.stage) && t.stage !== "blocked" && <div className="mt-2">{dropButton}</div>}
      {PENDING.includes(t.stage) && (
        <Button size="sm" variant="secondary" className="mt-2" onClick={() => onSkip(t.gid, true)}>
          건너뛰기
        </Button>
      )}
      {t.stage === "skipped" && (
        <Button size="sm" variant="secondary" className="mt-2" onClick={() => onSkip(t.gid, false)}>
          다시 대기열로
        </Button>
      )}
    </Card>
  );
}
