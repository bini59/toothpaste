import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Badge, Card } from "../ui";
import { ConfirmButton } from "../components/ConfirmButton";
import { StageBadge } from "../components/StageBadge";
import { Table } from "../components/Table";
import { LIVE_RELEASE, ago, latestPr, liveReleaseOf, pendingItems, type Status } from "../lib";
import { md } from "./ProjectChat";
import type { DiffItem, Mode, PR, Release as Rel, Rule, StagingDiff, State, Ticket } from "../types";

const REL_STATUS: Record<Rel["status"], [string, Status]> = {
  requested: ["요청됨", "info"],
  running: ["진행 중", "info"],
  pending: ["대기", "info"],
  deploying: ["배포 확인 중", "info"],
  done: ["완료", "success"],
  aborted: ["중단", "warning"],
  failed: ["실패", "danger"],
  empty: ["배포할 것 없음", "draft"],
  cancelled: ["취소", "draft"],
};
const MODE_HELP: [Mode, string][] = [
  ["staging", "리뷰 끝난 PR을 staging(테스트 서버)에 머지. staging 브랜치가 없고 preview_label이 있는 저장소는 프리뷰 슬롯"],
  ["direct", "staging을 건너뛰고 티켓을 바로 프로덕션으로"],
  ["promote", "이미 staging에 있는 티켓 하나만 프로덕션으로 (커밋 cherry-pick)"],
  ["all", "staging에 쌓인 것 전체를 프로덕션으로"],
];
const DIFF_VERDICT: Record<StagingDiff["verdict"], [string, Status]> = {
  ready: ["배포 가능", "success"],
  building: ["staging 빌드 중", "info"],
  broken: ["staging 빌드 실패", "danger"],
  none: ["차이 없음", "draft"],
  error: ["비교 실패", "danger"],
};
const NOTE_LEVEL: Record<"ok" | "check" | "hold", [string, Status]> = {
  ok: ["AI: 배포 추천", "success"],
  check: ["AI: 확인 필요", "warning"],
  hold: ["AI: 보류", "danger"],
};
const fmt = (v: string) =>
  new Date(v).toLocaleString("ko-KR", { month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit" });
const link = "text-primary hover:text-primary-700 hover:underline";
const Code = ({ children }: { children: React.ReactNode }) => (
  <code className="rounded bg-grey-100 px-1.5 py-0.5 text-xs font-semibold text-text-main">{children}</code>
);

interface Props {
  s: State;
  onHold: (hold: boolean) => void;
  onDeploy: (mode: Mode, ticket?: string, repo?: string) => void;
  onCancelDeploy: (id: number) => void;
}

function flow(r: Rule) {
  return [r.staging?.branch, r.via, r.prod.branch].filter(Boolean) as string[];
}
function verifyText(r: Rule) {
  const p = r.prod;
  const parts = [p.pipeline ? `CodePipeline ${p.pipeline}` : p.workflow ? `GitHub Actions ${p.workflow}` : "확인 방법 없음"];
  if (p.ecs) parts.push("ECS 롤아웃");
  if (p.smoke?.length) parts.push(`스모크 ${p.smoke.length}개`);
  return parts.join(" + ");
}
/** what the host will do for a ticket-level prod deploy (shown in the confirm) */
function directPlan(r: Rule, pr: PR, mode: "direct" | "promote") {
  const prod = r.prod.branch;
  const plain = mode === "direct" && pr.base === r.target && !r.version;
  const steps = plain
    ? [`PR #${pr.number}을 ${r.target}에 ${r.merge === "squash" ? "squash " : ""}머지`]
    : [`${r.target}에서 release 브랜치를 따서 PR #${pr.number}의 커밋만 cherry-pick${r.version ? " + 버전 올림" : ""} → ${r.target}에 머지`];
  if (mode === "direct" && !plain && pr.base !== r.target) steps.push(`원래 PR은 ${pr.base}에도 머지`);
  if (r.via) steps.push(`${r.via} → ${prod} 머지${r.version ? " + 태그·GitHub 릴리스" : ""}`);
  steps.push(`${verifyText(r)}로 반영 확인 → Asana 완료`);
  return steps.join(" → ");
}

function itemLine(i: DiffItem, github: string) {
  const pr = i.number ? (
    <a href={`https://github.com/${github}/pull/${i.number}`} target="_blank" rel="noreferrer" className={link}>#{i.number}</a>
  ) : (
    <a href={`https://github.com/${github}/commit/${i.sha}`} target="_blank" rel="noreferrer" className={link}>{i.sha.slice(0, 7)}</a>
  );
  return (
    <li key={i.sha} className={`flex gap-2 text-xs ${i.live ? "text-text-muted line-through" : "text-text-main"}`}>
      <span className="shrink-0">{pr}</span>
      {i.kind === "ticket" && i.url ? (
        <a href={i.url} target="_blank" rel="noreferrer" className={`shrink-0 ${link}`}>{i.key}</a>
      ) : (
        <span className="shrink-0 text-text-muted">{i.kind === "pr" ? "PR" : i.kind === "commit" ? "직접 커밋" : "머지"}</span>
      )}
      <span className="min-w-0">{i.title || i.msg}</span>
    </li>
  );
}

export function Release({ s, onHold, onDeploy, onCancelDeploy }: Props) {
  const rules = s.meta.rules ?? {};
  const modes = s.meta.modes ?? ({} as Record<Mode, string>);
  const rel = s.meta.settings.release as { at: string } | undefined;
  const hold = s.meta.release_hold;
  const live = s.releases.filter((r) => LIVE_RELEASE.includes(r.status));
  const history = s.releases.filter((r) => !LIVE_RELEASE.includes(r.status));
  const trainRepos = Object.entries(rules).filter(([, r]) => r.train).map(([n]) => n);
  const tk = (gid: string | null) => s.tickets.find((t) => t.gid === gid);
  const diffs = (s.staging_diffs ?? []).filter((d) => d.verdict !== "none");
  const allButton = (name: string, r: Rule, label = "staging 전체 → prod") => {
    if (!r.modes.includes("all") || !r.staging) return null;
    const staged = s.tickets.filter((t) => t.stage === "staged" && latestPr(s, t.gid)?.repo === name);
    const busyAll = s.releases.some((x) => x.repo === name && x.mode === "all" && LIVE_RELEASE.includes(x.status));
    return (
      <ConfirmButton size="sm" variant="secondary" disabled={busyAll}
        message={
          <>
            <b>{r.staging.branch}</b> 전체를 <b>{r.prod.branch}</b>로 내보낼까요? 추적 중인 티켓 {staged.length}개가 포함되고, toothpaste 밖에서
            들어간 커밋도 같이 나가요.{r.via ? ` ${r.via}를 거쳐 ${r.prod.branch}까지 머지하고 버전 태그를 붙여요.` : ""}{" "}
            {r.staging.pipeline ? `${r.staging.pipeline}가 초록일 때만 시작해요.` : ""}
          </>
        }
        confirmLabel="전체 배포" onConfirm={() => onDeploy("all", undefined, name)}>
        {label}
      </ConfirmButton>
    );
  };

  // tickets that can be shipped from here: AI-approved open PRs and PRs sitting in a staging branch
  const cands = s.tickets
    .map((t) => ({ t, pr: latestPr(s, t.gid) }))
    .filter(({ t, pr }) => {
      if (!pr || !rules[pr.repo] || t.completed) return false;
      if (t.stage === "staged") return true;
      if (t.stage !== "reviewed" || pr.state !== "OPEN") return false;
      const rv = s.reviews.find((v) => v.pr_url === pr.url && v.head_sha === pr.head_sha);
      return rv?.verdict === "approve";
    })
    // ready-for-review PRs first, then what sits in staging; grouped by repo
    .sort((a, b) => Number(a.t.stage === "staged") - Number(b.t.stage === "staged") || a.pr!.repo.localeCompare(b.pr!.repo) || b.pr!.number - a.pr!.number);

  const ticketButtons = (t: Ticket, pr: PR) => {
    const r = rules[pr.repo];
    const busy = !!liveReleaseOf(s, t.gid);
    const out: React.ReactNode[] = [];
    if (t.stage === "reviewed") {
      if (r.modes.includes("staging") && r.staging && pr.base === r.staging.branch)
        out.push(
          <ConfirmButton key="stg" size="sm" variant="secondary" disabled={busy}
            message={<>PR #{pr.number}을 <b>{r.staging.branch}</b>에 머지할까요? (5분 안에 머지)</>}
            confirmLabel="staging 머지" onConfirm={() => onDeploy("staging", t.gid)}>
            {r.staging.branch} 머지
          </ConfirmButton>,
        );
      else if (r.modes.includes("staging") && r.preview_label)
        out.push(
          <ConfirmButton key="pv" size="sm" variant="secondary" disabled={busy}
            message={<>PR #{pr.number}에 <b>{r.preview_label}</b> 라벨을 달까요? 프리뷰 슬롯은 하나라 이전 프리뷰를 대체해요.</>}
            confirmLabel="프리뷰 배포" onConfirm={() => onDeploy("staging", t.gid)}>
            프리뷰
          </ConfirmButton>,
        );
      if (r.modes.includes("direct"))
        out.push(
          <ConfirmButton key="dir" size="sm" disabled={busy}
            message={<><b>프로덕션({r.prod.branch})</b>에 바로 내보낼까요? {directPlan(r, pr, "direct")}</>}
            confirmLabel="prod 배포" onConfirm={() => onDeploy("direct", t.gid)}>
            prod 직행
          </ConfirmButton>,
        );
    }
    if (t.stage === "staged" && r.modes.includes("promote"))
      out.push(
        <ConfirmButton key="pro" size="sm" disabled={busy}
          message={<>이 티켓만 <b>프로덕션({r.prod.branch})</b>에 내보낼까요? {directPlan(r, pr, "promote")}</>}
          confirmLabel="prod 반영" onConfirm={() => onDeploy("promote", t.gid)}>
          prod 반영
        </ConfirmButton>,
      );
    return out.length ? <div className="flex flex-wrap gap-1.5">{out}</div> : <span className="text-xs text-text-muted">이 레포 규칙에 없는 방식</span>;
  };

  return (
    <>
      <div className="mb-6">
        <h1 className="text-xl font-bold text-text-main sm:text-2xl">배포</h1>
        <p className="mt-1 text-sm text-text-muted">
          레포별 규칙대로 티켓이나 staging을 프로덕션으로 내보내요. 누르면 요청만 기록되고 호스트가 5분 안에 바로 시작해요. 프로덕션 반영이 확인되면
          Asana 작업을 완료 처리해서, 이 티켓을 기다리던 후속 티켓도 다음 동기화 때 풀려요. 자동 롤백은 없어요.
        </p>
        <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {MODE_HELP.map(([m, help]) => (
            <div key={m} className="rounded-lg border border-border bg-surface px-3 py-2">
              <p className="text-xs font-semibold text-text-main">{modes[m] ?? m}</p>
              <p className="mt-0.5 text-xs text-text-muted">{help}</p>
            </div>
          ))}
        </div>
      </div>

      <section className="mb-8">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-base font-bold text-text-main sm:text-lg">staging에 있고 프로덕션에 없는 것</h2>
          <span className="text-xs text-text-muted">마지막 비교 {ago(s.meta.last_diff_scan)} · 10분마다 (tp tick "ticket-pilot: staging diff")</span>
        </div>
        {diffs.length ? (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {diffs.map((d) => {
              const r = rules[d.repo];
              const items = JSON.parse(d.items_json || "[]") as DiffItem[];
              const pending = pendingItems(d);
              const liveN = items.filter((i) => i.live).length;
              const kinds = (k: string) => pending.filter((i) => i.kind === k).length;
              const [vl, vs] = DIFF_VERDICT[d.verdict] ?? [d.verdict, "draft"];
              const noteFresh = !!d.note_md && d.note_head === d.staging_head;
              return (
                <Card key={d.repo} className="flex min-w-0 flex-col gap-2.5 px-5 py-4">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <p className="mr-1 text-sm font-bold text-text-main">{d.repo}</p>
                    <Badge status={pending.length || d.verdict === "error" ? vs : "draft"}>
                      {pending.length || d.verdict === "error" ? vl : "새로 나갈 것 없음"}
                    </Badge>
                    {d.note_level && noteFresh && <Badge status={NOTE_LEVEL[d.note_level][1]}>{NOTE_LEVEL[d.note_level][0]}</Badge>}
                    {d.note_md && !noteFresh && <Badge status="draft">AI 메모는 이전 staging 기준</Badge>}
                  </div>
                  {d.verdict === "error" ? (
                    <p className="text-xs font-medium text-danger-700">{d.error}</p>
                  ) : (
                    <p className="text-xs leading-relaxed text-text-muted">
                      <a href={`https://github.com/${d.github}/compare/${d.prod_branch}...${d.staging_branch}`} target="_blank" rel="noreferrer" className={link}>
                        {d.prod_branch}...{d.staging_branch}
                      </a>{" "}
                      커밋 {d.ahead} · 나갈 것 {pending.length} (티켓 {kinds("ticket")} · PR {kinds("pr")} · 직접 커밋 {kinds("commit")})
                      {liveN > 0 && ` · cherry-pick으로 이미 반영 ${liveN}`}
                      {d.oldest_at && ` · 가장 오래된 커밋 ${ago(d.oldest_at)}`}
                      {d.pipeline && ` · staging 파이프라인 ${d.pipeline}`}
                    </p>
                  )}
                  {!!d.hotfixes && d.hotfixes > 0 && (
                    <p className="text-xs font-medium text-warning-700">
                      {d.prod_branch}에만 있는 커밋 {d.hotfixes}개 (staging에 없음): 전체 배포 전에 충돌 여부를 확인하세요
                    </p>
                  )}
                  {!!d.via_ahead && <p className="text-xs font-medium text-warning-700">{r?.via}에 {d.prod_branch} 미반영 커밋 {d.via_ahead}개</p>}
                  {d.note_md && (
                    <details className="rounded-lg border border-border bg-surface px-3 py-2" open={noteFresh && d.note_level !== "ok"}>
                      <summary className="cursor-pointer text-xs font-semibold text-text-main">AI 메모 ({ago(d.note_at)})</summary>
                      <div data-md className="mt-2 text-xs leading-relaxed text-text-main wrap-anywhere [&>:first-child]:mt-0 [&>:last-child]:mb-0">
                        <Markdown remarkPlugins={[remarkGfm]} components={md}>{d.note_md}</Markdown>
                      </div>
                    </details>
                  )}
                  {items.length > 0 && (
                    <details>
                      <summary className="cursor-pointer text-xs font-semibold text-text-main">포함 항목 {items.length}</summary>
                      <ul className="mt-2 flex flex-col gap-1">{items.map((i) => itemLine(i, d.github))}</ul>
                    </details>
                  )}
                  {pending.length > 0 && r && (
                    <div className="mt-auto flex items-center justify-between gap-2 pt-1">
                      <span className="text-xs text-text-muted">{r.train ? `평일 ${rel?.at} 자동 배포 대상` : "자동 배포 없음"}</span>
                      {allButton(d.repo, r)}
                    </div>
                  )}
                </Card>
              );
            })}
          </div>
        ) : (
          <Card className="p-5 text-center text-sm text-text-muted">
            {s.meta.last_diff_scan ? "모든 레포의 staging이 프로덕션과 같아요." : "아직 비교 전이에요. 자동화(tp tick)가 10분마다 비교해요."}
          </Card>
        )}
      </section>

      {live.length > 0 && (
        <section className="mb-8">
          <h2 className="mb-3 text-base font-bold text-text-main sm:text-lg">진행 중</h2>
          <Table head={["#", "방식", "레포", "대상", "단계", "요청", ""]}>
            {live.map((r) => {
              const prs = JSON.parse(r.prs_json || "[]") as { url: string; number: number; key: string; title: string }[];
              const [label, status] = REL_STATUS[r.status] ?? [r.status, "draft"];
              return (
                <tr key={r.id}>
                  <td className="whitespace-nowrap text-text-muted">{r.id}</td>
                  <td className="whitespace-nowrap">{r.mode ? modes[r.mode] ?? r.mode : "평일 자동 배포"}</td>
                  <td className="whitespace-nowrap">{r.repo}</td>
                  <td>
                    {r.ticket_gid ? (
                      <a href={tk(r.ticket_gid)?.url} target="_blank" rel="noreferrer" className={link}>{tk(r.ticket_gid)?.key ?? r.ticket_gid}</a>
                    ) : (
                      prs.map((p) => (
                        <a key={p.url} href={p.url} target="_blank" rel="noreferrer" className={`mr-2 whitespace-nowrap ${link}`} title={`${p.key} ${p.title}`}>
                          #{p.number}
                        </a>
                      ))
                    )}
                    {r.release_pr_url && (
                      <a href={r.release_pr_url} target="_blank" rel="noreferrer" className={`ml-2 ${link}`}>#{r.release_pr_url.split("/").pop()}</a>
                    )}
                  </td>
                  <td>
                    <Badge status={status} className="mr-1.5 whitespace-nowrap">{label}</Badge>
                    <span className="text-xs text-text-muted">{r.step}</span>
                  </td>
                  <td className="whitespace-nowrap text-text-muted">{ago(r.requested_at || r.started_at)}</td>
                  <td>
                    {(r.status === "requested" || (r.status === "pending" && !r.mode)) && (
                      <ConfirmButton
                        size="sm"
                        variant="secondary"
                        message={r.mode ? "이 배포 요청을 취소할까요?" : "이 레포의 오늘 자동 배포를 취소할까요? PR은 staging에 남아 다음 자동 배포에 다시 들어가요."}
                        confirmLabel={r.mode ? "요청 취소" : "배포 취소"}
                        onConfirm={() => onCancelDeploy(r.id)}
                      >
                        취소
                      </ConfirmButton>
                    )}
                  </td>
                </tr>
              );
            })}
          </Table>
        </section>
      )}

      <section className="mb-8">
        <h2 className="mb-3 text-base font-bold text-text-main sm:text-lg">티켓 배포</h2>
        {cands.length ? (
          <Table head={["티켓", "제목", "레포", "PR", "배포"]}>
            {cands.map(({ t, pr }) => (
              <tr key={t.gid}>
                <td className="whitespace-nowrap">
                  <a href={t.url} target="_blank" rel="noreferrer" className={link}>{t.key}</a>
                  <div className="mt-1"><StageBadge stage={t.stage} /></div>
                </td>
                <td className="min-w-[220px]">{t.name}</td>
                <td className="whitespace-nowrap text-text-muted">{pr!.repo}</td>
                <td className="whitespace-nowrap">
                  <a href={pr!.url} target="_blank" rel="noreferrer" className={link}>#{pr!.number}</a>
                  <span className="ml-1 text-xs text-text-muted">→ {pr!.base}</span>
                </td>
                <td className="min-w-[220px]">{ticketButtons(t, pr!)}</td>
              </tr>
            ))}
          </Table>
        ) : (
          <Card className="p-5 text-center text-sm text-text-muted">AI 리뷰 "괜찮음"을 받았거나 staging에 머지된 티켓이 여기에 나와요.</Card>
        )}
      </section>

      <section className="mb-8">
        <h2 className="mb-3 text-base font-bold text-text-main sm:text-lg">레포별 규칙</h2>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-3">
          {Object.entries(rules).map(([name, r]) => {
            const staged = s.tickets.filter((t) => t.stage === "staged" && latestPr(s, t.gid)?.repo === name);
            const chain = flow(r);
            return (
              <Card key={name} className="flex flex-col gap-2.5 px-5 py-4">
                <div className="flex flex-wrap items-center gap-1.5">
                  <p className="mr-1 text-sm font-bold text-text-main">{name}</p>
                  {r.train && <Badge status="info">평일 {rel?.at} 자동 배포</Badge>}
                  {r.version && <Badge>버전 태그</Badge>}
                  <Badge status="draft">{r.merge}</Badge>
                </div>
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-text-muted">
                  {chain.map((b, i) => (
                    <span key={b} className="flex items-center gap-1.5">
                      {i > 0 && <span>→</span>}
                      <Code>{b}</Code>
                      {b === r.prod.branch && <span>(prod)</span>}
                      {r.staging && b === r.staging.branch && <span>(테스트)</span>}
                    </span>
                  ))}
                  {!r.staging && r.preview_label && <span>· 테스트: PR 라벨 <Code>{r.preview_label}</Code></span>}
                </div>
                <p className="text-xs text-text-muted">반영 확인: {verifyText(r)}</p>
                <div className="flex flex-wrap gap-1">
                  {r.modes.map((m) => <Badge key={m} status="success">{modes[m] ?? m}</Badge>)}
                </div>
                {r._note && <p className="text-xs text-text-muted">{r._note}</p>}
                {r.modes.includes("all") && r.staging && (
                  <div className="mt-auto flex items-center justify-between gap-2 pt-1">
                    <span className="text-xs text-text-muted">{r.staging.branch} 대기 티켓 {staged.length}</span>
                    {allButton(name, r)}
                  </div>
                )}
              </Card>
            );
          })}
        </div>
      </section>

      <section className="mb-8">
        <h2 className="mb-1 text-base font-bold text-text-main sm:text-lg">평일 자동 배포</h2>
        <p className="mb-3 text-sm text-text-muted">
          {trainRepos.join(", ")}: staging에 승인 머지된 PR을 평일 {rel?.at}에 staging → main으로 내보내요. 승인 안 된 커밋이 섞여 있으면 그날은 중단해요.
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Card className="px-5 py-4">
            <p className="text-xs font-medium text-text-muted">다음 배포</p>
            <p className={`mt-1 text-2xl font-extrabold tracking-tight ${hold ? "text-text-muted line-through" : "text-text-main"}`}>{fmt(s.meta.next_release_at)}</p>
            <p className="mt-1 text-xs text-text-muted">{hold ? "보류 중: 해제할 때까지 배포하지 않아요" : "10:00~12:00 사이 5분 주기로 시작"}</p>
          </Card>
          <Card className="px-5 py-4">
            <p className="text-xs font-medium text-text-muted">포함될 티켓 (staging 머지됨)</p>
            <p className="mt-1 text-2xl font-extrabold tracking-tight text-primary">
              {s.tickets.filter((t) => t.stage === "staged" && trainRepos.includes(latestPr(s, t.gid)?.repo ?? "")).length}
            </p>
            <p className="mt-1 text-xs text-text-muted">머지 진행 중 {s.tickets.filter((t) => t.stage === "approving").length}</p>
          </Card>
          <Card className="flex flex-col justify-between gap-3 px-5 py-4">
            <p className="text-xs font-medium text-text-muted">배포 보류</p>
            <ConfirmButton
              size="sm"
              variant={hold ? "primary" : "secondary"}
              message={hold ? "배포 보류를 해제할까요? 다음 평일 10:00에 배포돼요." : "다음 배포를 보류할까요? 해제할 때까지 평일 배포가 멈춰요."}
              confirmLabel={hold ? "보류 해제" : "보류"}
              onConfirm={() => onHold(!hold)}
            >
              {hold ? "보류 해제" : "배포 보류"}
            </ConfirmButton>
          </Card>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-base font-bold text-text-main sm:text-lg">배포 이력</h2>
        {history.length ? (
          <Table head={["날짜", "방식", "레포", "상태", "포함 PR", "배포 PR", "사유", "완료"]}>
            {history.map((r) => {
              const prs = JSON.parse(r.prs_json || "[]") as { url: string; number: number; key: string; title: string }[];
              const [label, status] = REL_STATUS[r.status] ?? [r.status, "draft"];
              return (
                <tr key={r.id}>
                  <td className="whitespace-nowrap">{r.day}</td>
                  <td className="whitespace-nowrap">{r.mode ? modes[r.mode] ?? r.mode : "평일 자동"}{r.version ? ` v${r.version}` : ""}</td>
                  <td className="whitespace-nowrap">{r.repo || "-"}</td>
                  <td><Badge status={status} className="whitespace-nowrap">{label}</Badge></td>
                  <td>
                    {prs.map((p) => (
                      <a key={p.url} href={p.url} target="_blank" rel="noreferrer" className={`mr-2 whitespace-nowrap ${link}`} title={`${p.key} ${p.title}`}>
                        #{p.number}
                      </a>
                    ))}
                  </td>
                  <td className="whitespace-nowrap">
                    {r.release_pr_url ? <a href={r.release_pr_url} target="_blank" rel="noreferrer" className={link}>#{r.release_pr_url.split("/").pop()}</a> : "-"}
                  </td>
                  <td className="text-text-muted">{r.reason || ""}</td>
                  <td className="whitespace-nowrap text-text-muted">{r.finished_at ? ago(r.finished_at) : "-"}</td>
                </tr>
              );
            })}
          </Table>
        ) : (
          <Card className="p-5 text-center text-sm text-text-muted">아직 배포 이력이 없어요.</Card>
        )}
      </section>
    </>
  );
}
