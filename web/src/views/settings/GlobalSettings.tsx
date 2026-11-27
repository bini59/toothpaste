import { useEffect, useRef, useState } from "react";
import { Download, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { Button, Input, cn } from "../../ui";
import { ConfigError, postConfig } from "../../api";
import { Select } from "../../components/Select";
import type { ConfigChange, ConfigState, GlobalConf } from "../../types";
import { applyTheme, loadThemes, type ThemeList } from "../../theme/theme";
import { LanShare, Tunnel } from "../Lan";
import { ModeSection } from "../Mode";
import { NotionAccounts } from "../NotionSource";
import { Update } from "../Update";
import { Accounts, useAgentAccounts } from "./Accounts";
import { AgentFields } from "./AgentFields";
import { AsanaConnect } from "./AsanaConnect";
import { ChangeTable, Errors } from "./ConfigFeedback";
import { Block, Check, CheckRow, Key, Row, Section, inputCls, numCls, selectCls } from "./Form";
import { GitHubConnect } from "./GitHubConnect";
import { SaveBar } from "./SaveBar";
import { Sectioned, sectionOf, type SectionDef } from "./Sectioned";
import { ConfigLoading, useConfig, useConfigDraft } from "./useConfig";
import { changed, fmt, num, same, text } from "./values";

const DAYS = ["월", "화", "수", "목", "금", "토", "일"];

/** 테마 (TP-157): the list of themes; GlobalEditor previews the picked one and puts the saved one back on leaving */
function ThemeSection({ value, onPick }: { value: string; onPick: (id: string) => void }) {
  const [l, setL] = useState<ThemeList | null>(null);
  const [error, setError] = useState("");
  const reload = () => loadThemes().then((x) => { setL(x); setError(""); applyTheme(value); }, (e) => setError(String(e)));
  useEffect(() => void reload(), []); // eslint-disable-line react-hooks/exhaustive-deps
  const broken = l?.themes.filter((t) => t.warnings.length) ?? [];
  return (
    <Section title="테마" desc={<>대시보드의 색과 글꼴이에요. 고르면 바로 미리 보이고, 저장해야 유지돼요 · <Key k="theme" /></>}>
      <Row label="테마" desc="auto는 시스템 설정을 따라 light와 dark 사이를 오가요">
        <Select className={selectCls} value={value} onChange={(e) => onPick(e.target.value)}>
          <option value="auto">auto (시스템 설정)</option>
          {l?.themes.filter((t) => t.ok).map((t) => (
            <option key={t.id} value={t.id}>{t.builtin ? t.name : `${t.name} (${t.file})`}</option>
          ))}
          {l && value !== "auto" && !l.themes.some((t) => t.id === value && t.ok) && <option value={value}>{value} (없음 · light로 보여요)</option>}
        </Select>
      </Row>
      <Row group label="내 테마" desc={<>이 폴더의 JSON 파일 하나가 테마 하나예요. 만드는 법은 도움말의 theme 주제에 있어요: <code className="font-mono">{l?.user_dir ?? "~/tp-work/themes"}</code></>}>
        <Button size="sm" variant="secondary" onClick={() => void reload()}>
          <RefreshCw className="h-4 w-4" /> 다시 불러오기
        </Button>
      </Row>
      {(error || broken.length > 0) && (
        <Block>
          <ul role="status" className="list-disc space-y-1 rounded-lg border border-warning-200 bg-warning-50 p-3 pl-8 text-sm text-warning-800">
            {error ? <li>테마 목록을 불러오지 못했어요: {error}</li> : broken.flatMap((t) => t.warnings.map((w) => <li key={t.file + w}>{t.file}: {w}</li>))}
          </ul>
        </Block>
      )}
    </Section>
  );
}

const GLOBAL_SECTIONS: SectionDef[] = [
  ["agents", "에이전트", ["agent", "agents", "max_active_runs", "agent_max_min", "agent_asana_write", "review", "check"]],
  ["git", "Git · GitHub", ["git_user", "branch_prefix", "worktree_root", "clone_root", "github_issues"]],
  ["asana", "Asana", ["asana_tag", "milestone_tag", "hidden_projects"]],
  ["notion", "Notion", []],
  ["release", "배포 열차", ["release", "deploy"]],
  ["theme", "테마", ["theme"]],
  ["advanced", "고급", ["owner_name", "update"]],
];
/** section addresses from before TP-168 (bookmarks, the guide) -> the section that holds them now */
const OLD_SECTIONS: Record<string, string> = { dispatch: "agents", review: "agents", github: "git", paths: "git", issues: "git", hidden: "asana", mode: "advanced", update: "advanced" };
type Role = "work" | "review" | "check";
const globalHref = (key: string) => `#settings${key ? `/${key}` : ""}`;

function GlobalEditor({ c, section, onReload }: { c: ConfigState; section: string; onReload: () => void }) {
  const orig = c.config.global;
  const { draft: d, edit: set, reset } = useConfigDraft(orig);
  // theme preview follows the draft (pick, 되돌리기, save); leaving the settings page unsaved shows the saved theme again
  const savedTheme = useRef(orig.theme);
  savedTheme.current = orig.theme;
  useEffect(() => applyTheme(d.theme), [d.theme]);
  useEffect(() => () => applyTheme(savedTheme.current), []);
  const [ghRepo, setGhRepo] = useState("");
  const [hide, setHide] = useState("");
  const [imp, setImp] = useState<{ doc: unknown; name: string; changes?: ConfigChange[]; errors?: string[]; done?: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const numField = (label: string, get: (g: GlobalConf) => unknown, put: (g: GlobalConf, v: unknown) => void, desc?: React.ReactNode) => (
    <Row label={label} desc={desc}>
      <Input className={numCls} inputMode="numeric" value={text(get(d))} onChange={(e) => set((x) => put(x, num(e.target.value)))} />
    </Row>
  );
  const strField = (label: string, get: (g: GlobalConf) => unknown, put: (g: GlobalConf, v: string) => void, desc?: React.ReactNode, type = "text") => (
    <Row label={label} desc={desc}>
      <Input className={cn(inputCls, type === "time" && "w-36")} type={type} value={text(get(d))} onChange={(e) => set((x) => put(x, e.target.value))} />
    </Row>
  );
  const pick = async (f: File | undefined) => {
    if (!f) return;
    try {
      const doc = JSON.parse(await f.text());
      const r = await postConfig("/api/config/import", { doc, dry_run: true });
      setImp({ doc, name: f.name, changes: r.changes, done: r.changes.length ? undefined : "가져올 파일의 값이 지금과 같아요" });
    } catch (e) {
      setImp({ doc: null, name: f.name, errors: e instanceof ConfigError ? e.errors : [`JSON을 읽지 못했어요: ${String(e)}`] });
    }
    if (fileRef.current) fileRef.current.value = "";
  };
  const doImport = async () => {
    try {
      const r = await postConfig("/api/config/import", { doc: imp!.doc });
      setImp({ doc: null, name: imp!.name, done: `${r.changes.length}개 항목을 가져왔어요` });
      onReload();
    } catch (e) {
      setImp({ ...imp!, changes: undefined, errors: e instanceof ConfigError ? e.errors : [String(e)] });
    }
  };
  const { accounts, error: accErr, reload: loadAccounts } = useAgentAccounts();
  const role = (r: Role, title: string, desc: React.ReactNode, children: React.ReactNode) => (
    <Section title={title} desc={desc}>
      <AgentFields key={r + (d.agents?.[r]?.tool || d.agent)} a={d.agents?.[r] || {}} c={c} accounts={accounts} inherit={{ label: "기본 도구", tool: d.agent }}
        onChange={(v) => set((x) => { const n = { ...(x.agents || {}) }; if (v) n[r] = v; else delete n[r]; x.agents = n; })} />
      {children}
    </Section>
  );
  const repos = Object.keys(c.config.repo);
  const cur = sectionOf(GLOBAL_SECTIONS, OLD_SECTIONS[section] ?? section), shown = cur || GLOBAL_SECTIONS[0][0];
  return (
    <div>
      <div className="mb-4">
        <h1 className="text-xl font-bold text-text-main sm:text-2xl">전역 설정</h1>
        <p className="mt-1 text-sm text-text-muted">
          모든 레포에 쓰는 값이에요. 저장하면 다음 tick부터 적용되고, 진행 중인 작업은 그대로 둬요.
          처음 설정은 <a href="#start" className="font-semibold text-primary hover:underline">시작하기</a>에서 한 번에 할 수 있어요.
          레포별 설정은 <a href="#projects" className="font-semibold text-primary hover:underline">프로젝트</a>에서 각 프로젝트의 설정 탭에 있어요.
          {c.imported_at && <> · JSON에서 가져온 시각 {fmt(c.imported_at)}</>}
        </p>
      </div>
      <Sectioned label="설정 목록" defs={GLOBAL_SECTIONS} cur={cur} href={globalHref} d={d} orig={orig}>
        {shown === "agents" && (
          <div className="flex flex-col gap-4">
            <Accounts a={accounts} error={accErr} onRefresh={() => { loadAccounts(); onReload(); }}>
              <Row label="기본 도구" desc="아래 카드에서 도구를 비우면 이 도구를 써요 · 티켓 작업과 프로젝트 채팅에서는 레포별 설정이 먼저 적용돼요">
                <Select className={selectCls} value={d.agent} onChange={(e) => set((x) => void (x.agent = e.target.value))}>
                  {c.agents.map((a) => <option key={a} value={a} disabled={!!accounts?.tools[a] && !accounts.tools[a].logged_in && d.agent !== a}>{a}</option>)}
                </Select>
              </Row>
            </Accounts>
            {role("work", "투입", "티켓 작업 에이전트: 새 티켓, 리뷰 수정, 충돌 해결, 답장 뒤 재실행.", <>
              {numField("동시 실행 수", (g) => g.max_active_runs, (g, v) => void (g.max_active_runs = v as number), "1~10")}
              {numField("에이전트 최대 실행 (분)", (g) => g.agent_max_min, (g, v) => void (g.agent_max_min = v as number), "넘기면 멈추고 막힘으로 보내요 · 0 = 제한 없음")}
              <CheckRow label="에이전트의 Asana 쓰기 허용" desc="채팅·티켓 에이전트가 tp asana로 댓글, 태스크 생성, 상태 변경, 태그를 써요. 끄면 읽기 전용이에요."
                checked={d.agent_asana_write !== false} onChange={(v) => set((x) => void (x.agent_asana_write = v))} />
            </>)}
            {role("review", "리뷰", "AI PR 리뷰 자동화가 쓰는 에이전트예요.", <>
              {numField("수정 왕복 한도", (g) => g.review.max_rework_rounds, (g, v) => void (g.review.max_rework_rounds = v as number), <>0 = 제한 없음 · 레포별로 덮어쓸 수 있어요 · <Key k="review.max_rework_rounds" /></>)}
            </>)}
            {role("check", "검사", "판단을 맡는 자동화(배포 diff 요약, worktree 정리 판정, 저장소 고르기)의 에이전트와 tp check 설정이에요.", <>
              {numField("검사 명령 제한 시간 (분)", (g) => g.check.timeout_min, (g, v) => void (g.check.timeout_min = v as number), "검사 명령 하나가 이 시간을 넘기면 실패로 봐요")}
              {numField("동시에 검사하는 PR 수", (g) => g.check.max_parallel, (g, v) => void (g.check.max_parallel = v as number))}
            </>)}
          </div>
        )}
        {shown === "git" && (
          <div className="flex flex-col gap-4">
            <GitHubConnect />
            <Section title="경로 · git" desc="워크트리와 클론을 어디에 두고, 어떤 이름으로 커밋하고 브랜치를 만들지 정해요.">
              {strField("git 사용자", (g) => g.git_user, (g, v) => void (g.git_user = v))}
              {strField("브랜치 접두어", (g) => g.branch_prefix, (g, v) => void (g.branch_prefix = v), "예: tp/ → tp/crsc-349")}
              {strField("워크트리 위치", (g) => g.worktree_root, (g, v) => void (g.worktree_root = v), "<위치>/<레포>/<티켓>")}
              {strField("클론 위치", (g) => g.clone_root, (g, v) => void (g.clone_root = v), "프로젝트 추가에서 <위치>/<레포>로 클론해요")}
            </Section>
            <Section title="GitHub 이슈" desc={<>여기 넣은 레포는 열린 이슈 전부가 티켓이에요. GitHub 소스 프로젝트의 레포는 tp 라벨 이슈만 가져와요 · <Key k="github_issues" /></>}>
              {Object.entries(d.github_issues).map(([repo, prefix]) => (
                <Row key={repo} group label={repo} desc={<>티켓 {prefix || "?"}-&lt;이슈 번호&gt;</>}>
                  <div className="flex items-center gap-2">
                    <Input aria-label={`${repo} 접두어`} placeholder="접두어" className={cn(inputCls, "w-28")} value={prefix} onChange={(e) => set((x) => void (x.github_issues[repo] = e.target.value))} />
                    <Button size="sm" variant="secondary" aria-label={`${repo} 삭제`} onClick={() => set((x) => void delete x.github_issues[repo])}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </Row>
              ))}
              <Row group label="레포 추가" desc="접두어는 티켓 번호 앞에 붙어요">
                <div className="flex items-center gap-2">
                  <Select className="h-9 min-w-0 flex-1 sm:max-w-72" value={ghRepo} onChange={(e) => setGhRepo(e.target.value)} aria-label="레포">
                    <option value="">레포 고르기…</option>
                    {repos.filter((r) => !(r in d.github_issues)).map((r) => <option key={r}>{r}</option>)}
                  </Select>
                  <Button size="sm" variant="secondary" disabled={!ghRepo} onClick={() => { set((x) => void (x.github_issues[ghRepo] = "")); setGhRepo(""); }}>
                    <Plus className="h-4 w-4" /> 추가
                  </Button>
                </div>
              </Row>
            </Section>
          </div>
        )}
        {shown === "asana" && (
          <div className="flex flex-col gap-4">
            <AsanaConnect />
            <Section title="태그" desc="어떤 Asana 티켓을 가져올지 정해요.">
              {strField("Asana 태그", (g) => g.asana_tag, (g, v) => void (g.asana_tag = v), "이 태그가 붙은 티켓만 투입해요")}
              {strField("마일스톤 태그", (g) => g.milestone_tag, (g, v) => void (g.milestone_tag = v), "부모 태스크에 달면 하위작업이 마일스톤 브랜치로 모였다가 한 번에 staging으로 가요")}
            </Section>
            <Section title="숨김 프로젝트" desc="이 Asana 프로젝트의 티켓은 동기화하지도, 보여주지도 않아요.">
              <Block>
                {Object.keys(d.hidden_projects).length ? (
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(d.hidden_projects).map(([gid, n]) => (
                      <span key={gid} className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-grey-50 py-1 pl-2.5 pr-1 text-sm text-text-main">
                        {n || gid}
                        <button type="button" aria-label={`${n} 숨김 해제`} className="rounded p-0.5 text-text-muted hover:bg-grey-200 hover:text-text-main" onClick={() => set((x) => void delete x.hidden_projects[gid])}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </span>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-text-muted">숨긴 프로젝트가 없어요.</p>
                )}
              </Block>
              <Row group label="프로젝트 숨기기">
                <div className="flex items-center gap-2">
                  <Select className="h-9 min-w-0 flex-1 sm:max-w-72" value={hide} onChange={(e) => setHide(e.target.value)} aria-label="숨길 프로젝트">
                    <option value="">프로젝트 고르기…</option>
                    {Object.entries(c.projects).filter(([g]) => !(g in d.hidden_projects)).map(([g, n]) => <option key={g} value={g}>{n} ({g})</option>)}
                  </Select>
                  <Button size="sm" variant="secondary" disabled={!hide} onClick={() => { set((x) => void (x.hidden_projects[hide] = c.projects[hide] || hide)); setHide(""); }}>
                    <Plus className="h-4 w-4" /> 숨기기
                  </Button>
                </div>
              </Row>
            </Section>
          </div>
        )}
        {shown === "notion" && (
          <Section title="Notion 계정 연결" desc="회사·개인 계정을 각각 연결할 수 있어요. 로그인할 때 공유한 데이터베이스를 여러 프로젝트에서 쓸 수 있어요.">
            <NotionAccounts />
          </Section>
        )}
        {shown === "release" && (
          <Section title="배포 열차" desc={<>정한 요일과 시각에 staging을 프로덕션으로 내보내요. 레포마다 배포 설정에서 열차에 태워요 · <Key k="release" /></>}>
            <Row group label="요일">
              <div className="flex flex-wrap gap-x-4 gap-y-2">
                {DAYS.map((l, i) => (
                  <Check key={i} label={l} checked={d.release.weekdays.includes(i)}
                    onChange={(v) => set((x) => void (x.release.weekdays = v ? [...x.release.weekdays, i].sort() : x.release.weekdays.filter((k) => k !== i)))} />
                ))}
              </div>
            </Row>
            {strField("시작 시각", (g) => g.release.at, (g, v) => void (g.release.at = v), undefined, "time")}
            {numField("창 (분)", (g) => g.release.window_min, (g, v) => void (g.release.window_min = v as number), "시작 시각부터 이 시간 안에만 출발해요")}
            {strField("시간대", (g) => g.release.timezone, (g, v) => void (g.release.timezone = v))}
            {strField("staging 브랜치", (g) => g.release.staging_branch, (g, v) => void (g.release.staging_branch = v))}
            {strField("main 브랜치", (g) => g.release.main_branch, (g, v) => void (g.release.main_branch = v))}
            {numField("배포 확인 제한 시간 (분)", (g) => g.deploy.deploy_timeout_min, (g, v) => void (g.deploy.deploy_timeout_min = v as number),
              <>열차만의 값이 아니에요. 열차와 배포 페이지에서 요청한 배포 둘 다, 머지와 프로덕션 반영을 이 시간까지 기다려요 · 5~240 · <Key k="deploy.deploy_timeout_min" /></>)}
          </Section>
        )}
        {shown === "theme" && <ThemeSection value={d.theme} onPick={(v) => set((x) => void (x.theme = v))} />}
        {shown === "advanced" && (
          <div className="flex flex-col gap-4">
            <Section title="사용자">
              {strField("사용자 이름", (g) => g.owner_name, (g, v) => void (g.owner_name = v), "에이전트 프롬프트에서 이 이름으로 불러요 (기본값 the user)")}
            </Section>
            <Section title="업데이트" desc={<>새 릴리즈를 확인하고 이 컴퓨터의 toothpaste(치약)를 업데이트해요 · <Key k="update" /></>}>
              <CheckRow label="새 릴리즈 자동 적용" desc="tick이 새 릴리즈를 찾으면 업데이트 버튼과 같은 tp update를 실행해요. 실행할 수 없는 상태면 건너뛰고 알려요."
                checked={d.update.auto} onChange={(v) => set((x) => void (x.update.auto = v))} />
              {numField("확인 주기 (분)", (g) => g.update.check_min, (g, v) => void (g.update.check_min = v as number), "새 릴리즈 태그를 이 주기로 가져와요 · 10 이상")}
              <Block><Update onDone={onReload} /></Block>
            </Section>
            <Section title="설정 파일" desc={<>아래 읽기 전용 값은 대시보드를 재시작해야 바뀌어요. 바꾸려면 <code className="font-mono">bin/tp config set global.&lt;키&gt; …</code> 후 재시작해요.</>}>
              {c.read_only.map((k) => (
                <Row key={k} label={k} desc="읽기 전용 · 재시작 필요">
                  <Input className={inputCls} value={text(orig[k])} disabled readOnly />
                </Row>
              ))}
              <Row group label="내보내기 · 가져오기" desc={<>설정 전체를 JSON으로 받거나 올려요. 저장할 때마다 자동 백업도 남아요: {c.backups.count}개 · {c.backups.dir}</>}>
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" variant="secondary" onClick={() => (location.href = "/api/config/export")}>
                    <Download className="h-4 w-4" /> 내보내기 (JSON)
                  </Button>
                  <Button size="sm" variant="secondary" onClick={() => fileRef.current?.click()}>
                    <Upload className="h-4 w-4" /> 가져오기…
                  </Button>
                  <input ref={fileRef} type="file" accept="application/json,.json" className="hidden" onChange={(e) => pick(e.target.files?.[0])} />
                </div>
              </Row>
              {imp && (
                <Block>
                  <div className="space-y-2">
                    <p className="text-sm text-text-main">파일: {imp.name}</p>
                    {imp.errors && <Errors errors={imp.errors} />}
                    {imp.done && <p className="text-sm text-success-700">{imp.done}</p>}
                    {imp.changes && imp.changes.length > 0 && (
                      <>
                        <p className="text-sm font-semibold text-text-main">가져오면 이 {imp.changes.length}개 항목이 바뀌어요</p>
                        <ChangeTable changes={imp.changes} />
                        <div className="flex gap-2">
                          <Button size="sm" onClick={doImport}>가져오기</Button>
                          <Button size="sm" variant="secondary" onClick={() => setImp(null)}>취소</Button>
                        </div>
                      </>
                    )}
                  </div>
                </Block>
              )}
            </Section>
            {/* the risky switches last */}
            <Section title="실행 방식" desc="호스트 설치와 Docker 사이를 옮겨요. 서비스가 멈췄다 다시 떠요.">
              <Block><ModeSection /></Block>
            </Section>
            <Section title="로컬망 공유" desc="같은 망의 팀원이 이 대시보드를 쓰게 해요.">
              <LanShare onSaved={onReload} />
              <Tunnel />
            </Section>
          </div>
        )}
      </Sectioned>
      <SaveBar dirty={!same(d, orig)} onReset={reset} onSaved={onReload}
        run={(dry) => postConfig("/api/config", { scope: "global", value: changed(d, orig), dry_run: dry })} />
    </div>
  );
}

/** 설정 menu: global settings only; each repo's settings live on its 프로젝트 page (RepoSettings) */
export function GlobalSettings({ sub, onChanged }: { sub: string; onChanged: () => void }) {
  const { c, error, load } = useConfig();
  if (!c) return <ConfigLoading error={error} />;
  return <GlobalEditor c={c} section={sub} onReload={() => { load(); onChanged(); }} />;
}
