import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Play, Plus, Trash2 } from "lucide-react";
import { Badge, Button, Input, cn } from "../../ui";
import { ConfigError, postConfig } from "../../api";
import { Select } from "../../components/Select";
import { Table } from "../../components/Table";
import { ago, type Status } from "../../lib";
import type { CheckCmd, ConfigState, DeployRule, RepoConf, State, TrialRun } from "../../types";
import { AgentFields } from "./AgentFields";
import { Block, Check, CheckRow, Key, Row, Section, areaCls, boxCls, inputCls, numCls, selectCls } from "./Form";
import { SaveBar } from "./SaveBar";
import { Sectioned, sectionOf, type SectionDef } from "./Sectioned";
import { ConfigLoading, useConfig, useConfigDraft } from "./useConfig";
import { num, same, text } from "./values";

const TRIAL: Record<TrialRun["status"], [string, Status]> = {
  running: ["실행 중", "info"],
  pass: ["통과", "success"],
  fail: ["실패", "danger"],
  error: ["실행 못 함", "warning"],
};
const RESULT: Record<string, Status> = { pass: "success", flaky: "warning", fail: "danger", timeout: "danger", error: "warning" };

// Project settings own the links. A project rename must not make the repo draft dirty.
const noLinks = ({ links: _links, ...rest }: RepoConf) => rest;
type RepoDraft = ReturnType<typeof noLinks>;

function Basics({ d, set, info, others }: { d: RepoDraft; set: (f: (d: RepoDraft) => void) => void; info?: ConfigState["repo_info"][string]; others: string[] }) {
  const deps = d.depends_on ?? [];
  const field = (k: "path" | "github" | "worktree_base" | "description", label: string, desc?: React.ReactNode) => (
    <Row label={label} desc={desc}>
      <Input className={inputCls} value={text(d[k])} onChange={(e) => set((x) => void (x[k] = e.target.value || null))} />
    </Row>
  );
  return (
    <Section title="기본">
      {field("path", "경로", info && !info.path_ok ? <span className="text-danger-700">이 폴더가 없어요</span> : "이 Mac의 체크아웃 (~ 사용 가능)")}
      {field("github", "GitHub", "owner/name")}
      {field("description", "설명")}
      {field("worktree_base", "워크트리 시작 브랜치", "비우면 배포 규칙의 staging(있을 때) 또는 GitHub 기본 브랜치")}
      <CheckRow label="자동 준비" checked={d.auto_setup !== false} onChange={(v) => set((x) => void (x.auto_setup = v ? null : false))}
        desc="새 워크트리에 이 체크아웃의 .env / .env.* 파일을 같은 위치로 복사하고(하위 폴더 포함, 이미 있으면 그대로), lockfile을 보고 pnpm → yarn → npm 순으로 골라 install 해요." />
      <Row label="워크트리 준비 스크립트" desc="자동 준비 다음에, 에이전트보다 먼저 실행돼요. $TP_ROOT_PATH = 이 체크아웃, $TP_WORKTREE_PATH = 새 워크트리">
        <textarea rows={3} value={text(d.setup)} onChange={(e) => set((x) => void (x.setup = e.target.value || null))}
          placeholder={"pnpm install\ncp $TP_ROOT_PATH/.env $TP_WORKTREE_PATH/.env"} className={cn(areaCls, "font-mono")} />
      </Row>
      <Row label="dev 서버 명령" desc={<>마일스톤 페이지의 dev 서버. 비우면 package.json의 dev / start:dev / start 스크립트. {"{port}"} = 고른 포트, $PORT도 넣어줘요</>}>
        <Input className={cn(inputCls, "font-mono")} value={text(d.dev?.cmd)} placeholder="pnpm dev --port {port}"
          onChange={(e) => set((x) => void (x.dev = devOf({ ...x.dev, cmd: e.target.value || null })))} />
      </Row>
      <Row label="dev 서버 포트" desc="비우면 스크립트, .env PORT, 프레임워크 기본값">
        <Input className={numCls} inputMode="numeric" value={d.dev?.port ? String(d.dev.port) : ""}
          onChange={(e) => set((x) => void (x.dev = devOf({ ...x.dev, port: Number(e.target.value.replace(/\D/g, "")) || null })))} />
      </Row>
      <Row label="의존 레포" desc={<>이 레포의 staging 변경이 나가려면 먼저 프로덕션에 있어야 할 수 있는 레포. staging 변경 요약이 그 레포의 프로덕션 브랜치를 확인해요 · <Key k="depends_on" /></>}>
        <div className="flex min-h-9 flex-wrap items-center gap-x-4 gap-y-1">
          {others.length ? others.map((n) => (
            <label key={n} className="flex cursor-pointer items-center gap-1.5 text-sm">
              <input type="checkbox" className={cn(boxCls, "cursor-pointer")} checked={deps.includes(n)}
                onChange={(e) => set((x) => {
                  const v = (x.depends_on ?? []).filter((r) => r !== n).concat(e.target.checked ? [n] : []);
                  x.depends_on = v.length ? v : null;
                })} />
              {n}
            </label>
          )) : <span className="text-sm text-text-muted">다른 레포가 없어요</span>}
        </div>
      </Row>
    </Section>
  );
}

/** null when both fields are empty, so an untouched repo keeps no dev entry */
const devOf = (v: NonNullable<RepoConf["dev"]>): RepoConf["dev"] => {
  const out = { ...(v.cmd ? { cmd: v.cmd } : {}), ...(v.port ? { port: v.port } : {}) };
  return Object.keys(out).length ? out : null;
};

function AgentSection({ d, set, c }: { d: RepoDraft; set: (f: (d: RepoDraft) => void) => void; c: ConfigState }) {
  const g = c.config.global;
  const work = g.agents?.work?.tool ? { label: "전역 투입 카드", tool: g.agents.work.tool } : { label: "전역 설정", tool: String(g.agent || "codex") };
  return (
    <Section title="에이전트" desc="새로 투입하는 티켓과 수정·충돌·답장 재실행에 적용돼요. 이미 돌고 있는 에이전트는 그대로 둬요. 비우면 전역 설정 > 에이전트의 투입 카드를 따라요.">
      <AgentFields a={d.agent || {}} c={c} inherit={work} onChange={(v) => set((x) => void (x.agent = v))} />
    </Section>
  );
}

function TrialPanel({ name, c, dirty, onReload }: { name: string; c: ConfigState; dirty: boolean; onReload: () => void }) {
  const run = c.check_runs[name];
  const [err, setErr] = useState("");
  const [log, setLog] = useState<{ title: string; body: string } | null>(null);
  const saved = c.config.repo[name];
  const enabled = (saved.checks || []).filter((x) => !x.off).length;
  const ref = c.repo_info[name]?.base_ref || "origin/<base>";
  const start = async () => {
    setErr("");
    try {
      await postConfig("/api/config/check-run", { repo: name });
    } catch (e) {
      setErr(e instanceof ConfigError ? e.errors.join(" ") : String(e));
    }
    onReload();
  };
  const openLog = async (title: string, q: string) => {
    const r = await fetch(`/api/config/check-log?${q}`);
    const j = await r.json().catch(() => ({}));
    setLog({ title, body: j.log || "(로그 없음)" });
  };
  const why = saved.ci ? "CI를 쓰는 레포는 GitHub 검사를 읽어요" : !enabled ? "켜진 검사 명령이 없어요" : dirty ? "저장한 명령으로 돌아요. 먼저 저장하세요" : "";
  return (
    <div className="rounded-lg border border-border bg-grey-50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="secondary" disabled={!!why || run?.status === "running"} onClick={start}>
          <Play className="h-4 w-4" /> base 브랜치에서 시험 실행
        </Button>
        <span className="text-xs text-text-muted">
          기준 <code className="font-mono">{run?.ref || ref}</code> · 저장된 켜진 검사 {enabled}개 · 백그라운드에서 돌아요
          {why && <> · {why}</>}
        </span>
      </div>
      {err && <p className="mt-2 text-sm text-danger-700">{err}</p>}
      {run ? (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-semibold text-text-main">마지막 결과</span>
            <Badge status={TRIAL[run.status][1]}>{TRIAL[run.status][0]}</Badge>
            <span className="text-xs text-text-muted">
              #{run.id} · {run.ref}
              {run.sha ? ` @ ${run.sha}` : ""} · {ago(run.finished_at || run.started_at)}
            </span>
            <button type="button" className="text-xs text-primary hover:underline" onClick={() => openLog(`#${run.id} 실행 로그`, `run=${run.id}`)}>
              실행 로그
            </button>
          </div>
          {run.error && <p className="text-xs text-danger-700">{run.error}</p>}
          {run.results.length > 0 && (
            <Table head={["검사", "명령", "결과", "시간", ""]}>
              {run.results.map((r) => (
                <tr key={r.name}>
                  <td className="font-semibold">{r.name}</td>
                  <td className="font-mono text-xs">{r.cmd}</td>
                  <td>
                    <Badge status={RESULT[r.status] ?? "draft"}>{r.status}</Badge>
                  </td>
                  <td className="whitespace-nowrap text-text-muted">{Math.round(r.duration_s)}초</td>
                  <td className="text-right">
                    <button type="button" className="text-xs text-primary hover:underline" onClick={() => openLog(r.name, `run=${run.id}&name=${encodeURIComponent(r.name)}`)}>
                      로그
                    </button>
                  </td>
                </tr>
              ))}
            </Table>
          )}
        </div>
      ) : (
        <p className="mt-2 text-xs text-text-muted">아직 시험 실행한 적이 없어요.</p>
      )}
      {log && (
        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between text-xs font-semibold text-text-main">
            {log.title}
            <button type="button" className="text-primary hover:underline" onClick={() => setLog(null)}>닫기</button>
          </div>
          <pre className="max-h-72 overflow-auto rounded-md border border-border bg-surface p-2 text-xs text-text-main">{log.body}</pre>
        </div>
      )}
    </div>
  );
}

function Checks({ name, d, set, c, onReload }: { name: string; d: RepoDraft; set: (f: (d: RepoDraft) => void) => void; c: ConfigState; onReload: () => void }) {
  const upd = (i: number, k: keyof CheckCmd, v: string | boolean) =>
    set((x) => {
      const ck = x.checks[i] as unknown as Record<string, unknown>;
      if (v === "" || v === false) delete ck[k];
      else ck[k] = v;
    });
  const move = (i: number, by: number) => set((x) => void x.checks.splice(i + by, 0, x.checks.splice(i, 1)[0]));
  const dirty = !same(d.checks, c.config.repo[name].checks) || d.ci !== c.config.repo[name].ci;
  return (
    <Section title="검사" desc="PR head마다 tp check가 돌리는 명령이에요. 실패하면 에이전트에게 돌아가요.">
      <CheckRow label="GitHub 검사 결과 읽기" desc={<>로컬 명령 대신 gh pr checks 결과를 읽어요 · <Key k="ci" /></>} checked={d.ci} onChange={(v) => set((x) => void (x.ci = v))} />
      <Block>
        <div className="space-y-2">
          {d.checks.map((ck, i) => (
            <div key={i} className={cn("rounded-lg border border-border p-2.5", ck.off && "bg-grey-50")}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="flex flex-col">
                  <button type="button" aria-label="위로" disabled={i === 0} className="text-text-muted hover:text-text-main disabled:opacity-30" onClick={() => move(i, -1)}>
                    <ArrowUp className="h-3.5 w-3.5" />
                  </button>
                  <button type="button" aria-label="아래로" disabled={i === d.checks.length - 1} className="text-text-muted hover:text-text-main disabled:opacity-30" onClick={() => move(i, 1)}>
                    <ArrowDown className="h-3.5 w-3.5" />
                  </button>
                </span>
                <Input aria-label="이름" placeholder="이름" className={cn(inputCls, "w-32")} value={ck.name || ""} onChange={(e) => upd(i, "name", e.target.value)} />
                <Input aria-label="명령" placeholder="명령 (예: pnpm run lint)" className={cn(inputCls, "min-w-[220px] flex-1 font-mono")} value={ck.cmd || ""} onChange={(e) => upd(i, "cmd", e.target.value)} />
                <Input aria-label="dir" placeholder="dir (선택)" className={cn(inputCls, "w-28")} value={ck.dir || ""} onChange={(e) => upd(i, "dir", e.target.value)} />
                <Check label="켜짐" checked={!ck.off} onChange={(v) => upd(i, "off", !v)} />
                <Button size="sm" variant="secondary" aria-label="검사 삭제" onClick={() => set((x) => void x.checks.splice(i, 1))}>
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
              {ck.off && (
                <Input aria-label="꺼둔 이유" placeholder="꺼둔 이유 (예: staging에서도 이미 실패 중)" className={cn(inputCls, "mt-2")} value={ck.reason || ""} onChange={(e) => upd(i, "reason", e.target.value)} />
              )}
            </div>
          ))}
          {!d.checks.length && <p className="text-sm text-text-muted">검사 명령이 없어요{d.ci ? " (CI 결과를 읽어요)" : " · 검사 없이 AI 리뷰만 해요"}.</p>}
        </div>
        <Button size="sm" variant="secondary" className="mt-3" onClick={() => set((x) => void x.checks.push({ name: "", cmd: "" }))}>
          <Plus className="h-4 w-4" /> 검사 추가
        </Button>
      </Block>
      <Block>
        <TrialPanel name={name} c={c} dirty={dirty} onReload={onReload} />
      </Block>
    </Section>
  );
}

function Review({ d, set, c, info }: { d: RepoDraft; set: (f: (d: RepoDraft) => void) => void; c: ConfigState; info?: ConfigState["repo_info"][string] }) {
  const g = c.config.global.review.max_rework_rounds;
  return (
    <Section title="리뷰">
      <Row label="수정 왕복 한도" desc={<>비우면 전역 값({g})을 써요 · 0 = 제한 없음 · <Key k="review.max_rework_rounds" /></>}>
        <Input className={numCls} inputMode="numeric" placeholder={`전역 ${g}`} value={text(d.review.max_rework_rounds)}
          onChange={(e) => set((x) => { const v = num(e.target.value); if (v === null) delete x.review.max_rework_rounds; else x.review.max_rework_rounds = v; })} />
      </Row>
      <Row group label="REVIEW.md" desc="레포에 이 파일이 있으면 AI 리뷰가 따라요">
        <div className="flex h-9 items-center">
          {info?.review_md ? <Badge status="success">있음</Badge> : <Badge status="draft">없음</Badge>}
        </div>
      </Row>
    </Section>
  );
}

function Deploy({ d, set, c, s }: { d: RepoDraft; set: (f: (d: RepoDraft) => void) => void; c: ConfigState; s: State }) {
  const r = d.deploy;
  if (!r)
    return (
      <Section title="배포">
        <Block>
        <p className="text-sm text-text-muted">배포 규칙이 없어요. 머지된 PR이 배포 대기에 머물고 Asana 완료 처리가 안 돼요.</p>
        <Button size="sm" variant="secondary" className="mt-3" onClick={() => set((x) => void (x.deploy = { merge: "merge", modes: ["direct"], prod: { branch: "main" } }))}>
          <Plus className="h-4 w-4" /> 배포 규칙 추가
        </Button>
        </Block>
      </Section>
    );
  const up = (f: (r: DeployRule) => void) => set((x) => f(x.deploy!));
  /** optional text keys: "" removes the key */
  const opt = (o: Record<string, unknown>, k: string, v: string) => (v === "" ? delete o[k] : (o[k] = v));
  const verify = r.prod.pipeline !== undefined ? "pipeline" : r.prod.workflow !== undefined ? "workflow" : r.prod.release ? "release" : "";
  const modes = r.modes ?? ["direct"];
  return (
    <Section title="배포" desc={<>이 레포의 변경이 프로덕션까지 가는 길이에요 · <Key k="deploy" /></>}>
      <Row group label="배포 방식" desc={<>배포 페이지에서 요청할 수 있는 방식 · <Key k="modes" /></>}>
        <div className="flex flex-col gap-2">
          {c.modes.map((m) => (
            <Check key={m} label={<>{m} <span className="text-text-muted">· {s.meta.modes?.[m as keyof typeof s.meta.modes] ?? ""}</span></>} checked={modes.includes(m)}
              onChange={(v) => up((x) => void (x.modes = v ? c.modes.filter((k) => k === m || modes.includes(k)) : modes.filter((k) => k !== m)))} />
          ))}
        </div>
      </Row>
      <CheckRow label="평일 배포 열차" desc={<>전역 설정의 배포 열차에 이 레포를 태워요 · <Key k="train" /></>} checked={!!r.train} onChange={(v) => up((x) => (v ? (x.train = true) : delete x.train))} />
      <CheckRow label="티켓 PR을 staging 브랜치로" desc={<Key k="pr_into_staging" />} checked={!!r.pr_into_staging} onChange={(v) => up((x) => (v ? (x.pr_into_staging = true) : delete x.pr_into_staging))} />
      <CheckRow label="staging 브랜치 사용" checked={!!r.staging} onChange={(v) => up((x) => (v ? (x.staging = { branch: "staging" }) : delete x.staging))} />
      {r.staging && (
        <>
          <Row label="staging 브랜치">
            <Input className={inputCls} value={r.staging.branch} onChange={(e) => up((x) => void (x.staging!.branch = e.target.value))} />
          </Row>
          <Row label="staging 파이프라인" desc="선택 · CodePipeline 이름">
            <Input className={inputCls} value={r.staging.pipeline ?? ""} onChange={(e) => up((x) => void opt(x.staging!, "pipeline", e.target.value))} />
          </Row>
        </>
      )}
      <Row label="중간 브랜치" desc={<>선택 · staging과 prod 사이 브랜치 (예: main) · <Key k="via" /></>}>
        <Input className={inputCls} value={r.via ?? ""} onChange={(e) => up((x) => void opt(x, "via", e.target.value))} />
      </Row>
      <Row label="prod 브랜치" desc="프로덕션이 도는 브랜치">
        <Input className={inputCls} value={r.prod.branch} onChange={(e) => up((x) => void (x.prod.branch = e.target.value))} />
      </Row>
      <Row label="반영 확인" desc="머지한 커밋이 프로덕션에 올라갔는지 무엇으로 확인할지">
        <Select className={selectCls} value={verify}
          onChange={(e) => up((x) => {
            const v = e.target.value, old = x.prod.pipeline ?? x.prod.workflow ?? "";
            delete x.prod.pipeline; delete x.prod.workflow; delete x.prod.release;
            if (v) (x.prod as Record<string, unknown>)[v] = v === "release" ? true : old;
          })}>
          <option value="pipeline">CodePipeline</option>
          <option value="workflow">GitHub Actions workflow</option>
          <option value="release">최신 GitHub Release에 포함</option>
          <option value="">확인 안 함</option>
        </Select>
      </Row>
      {(verify === "pipeline" || verify === "workflow") && (
        <Row label={verify === "pipeline" ? "prod 파이프라인" : "prod workflow 파일"}>
          <Input className={inputCls} value={text(r.prod[verify])} onChange={(e) => up((x) => void ((x.prod as Record<string, unknown>)[verify] = e.target.value))} />
        </Row>
      )}
      <Row label="머지 방식">
        <Select className={selectCls} value={r.merge ?? "merge"} onChange={(e) => up((x) => void (x.merge = e.target.value))}>
          {c.merges.map((m) => <option key={m}>{m}</option>)}
        </Select>
      </Row>
      <Row label="버전">
        <Select className={selectCls} value={r.version ?? ""} onChange={(e) => up((x) => void opt(x, "version", e.target.value))}>
          <option value="">없음</option>
          {c.versions.map((m) => <option key={m}>{m}</option>)}
        </Select>
      </Row>
      <Row label="프리뷰 라벨" desc="선택">
        <Input className={inputCls} value={r.preview_label ?? ""} onChange={(e) => up((x) => void opt(x, "preview_label", e.target.value))} />
      </Row>
      {!!(r.prod.ecs || r.prod.smoke?.length) && (
        <Block>
          <p className="text-xs text-text-muted">
            {r.prod.ecs && <>ECS 롤아웃 확인: {r.prod.ecs.cluster} / {r.prod.ecs.service}. </>}
            {r.prod.smoke?.length ? <>스모크 {r.prod.smoke.length}개: {r.prod.smoke.join(", ")}. </> : null}
            (CLI: <code className="font-mono">tp config set repo.&lt;이름&gt;.deploy.prod.ecs …</code>)
          </p>
        </Block>
      )}
      <Row label="메모">
        <textarea rows={2} value={r._note ?? ""} onChange={(e) => up((x) => void opt(x, "_note", e.target.value))} className={areaCls} />
      </Row>
      <Row group label="배포 규칙 삭제" desc="규칙이 없으면 머지된 PR이 배포 대기에 머물고 완료 처리가 안 돼요. 저장해야 지워져요.">
        <Button size="sm" variant="secondary" className="text-danger-700" onClick={() => set((x) => void (x.deploy = null))}><Trash2 className="h-4 w-4" /> 배포 규칙 삭제</Button>
      </Row>
    </Section>
  );
}

const REPO_SECTIONS: SectionDef[] = [
  ["basics", "기본", ["path", "github", "description", "worktree_base", "auto_setup", "setup", "dev", "depends_on"]],
  ["agent", "에이전트", ["agent"]],
  ["checks", "검사", ["checks", "ci"]],
  ["review", "리뷰", ["review"]],
  ["deploy", "배포", ["deploy"]],
];

function RepoEditor({ name, c, s, section, href, onReload }: { name: string; c: ConfigState; s: State; section: string; href: (section: string) => string; onReload: () => void }) {
  const saved = c.config.repo[name];
  const orig = useMemo(() => noLinks(saved), [saved]);
  const { draft: d, edit: set, reset } = useConfigDraft(orig);
  const info = c.repo_info[name];
  const cur = sectionOf(REPO_SECTIONS, section), shown = cur || REPO_SECTIONS[0][0];
  return (
    <div>
      <p className="mb-3 text-right text-xs">
        <a href={`#history/${encodeURIComponent(name)}`} className="font-semibold text-primary-700 hover:underline">이 저장소의 변경 이력 보기</a>
      </p>
      <Sectioned label="레포 설정 목록" defs={REPO_SECTIONS} cur={cur} href={href} d={d} orig={orig}>
        {shown === "basics" && <Basics d={d} set={set} info={info} others={Object.keys(c.config.repo).filter((n) => n !== name)} />}
        {shown === "agent" && <AgentSection d={d} set={set} c={c} />}
        {shown === "checks" && <Checks name={name} d={d} set={set} c={c} onReload={onReload} />}
        {shown === "review" && <Review d={d} set={set} c={c} info={info} />}
        {shown === "deploy" && <Deploy d={d} set={set} c={c} s={s} />}
      </Sectioned>
      <SaveBar dirty={!same(d, orig)} onReset={reset} onSaved={onReload}
        run={(dry) => postConfig("/api/config", { scope: `repo.${name}`, value: d, dry_run: dry })} />
    </div>
  );
}

/** 프로젝트 > <repo> > 설정; `section` is the open section, `href` the address of a section ("" = the section list) */
export function RepoSettings({ name, s, section, href, onChanged }: { name: string; s: State; section: string; href: (section: string) => string; onChanged: () => void }) {
  const { c, error, load } = useConfig();
  if (!c) return <ConfigLoading error={error} />;
  if (!c.config.repo[name]) return <p className="text-sm text-text-muted">설정에 없는 레포예요: {name}</p>;
  return <RepoEditor key={name} name={name} c={c} s={s} section={section} href={href} onReload={() => { load(); onChanged(); }} />;
}
