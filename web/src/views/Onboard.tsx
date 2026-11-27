import { useEffect, useMemo, useState } from "react";
import { Check, Plus, RefreshCw, Trash2 } from "lucide-react";
import { Button, Card, Input, cn } from "../ui";
import { ConfigError, getJson, postConfig, postJson } from "../api";
import { Select } from "../components/Select";
import type { CloneJob, ConfigChange, ConfigState, GithubRepo, PathCheck, ProjectConf, ProjectSource } from "../types";
import { ChangeTable, Errors } from "./settings/ConfigFeedback";
import { ID_RE, SOURCE_LABEL, slug } from "./settings/projectFields";
import { emptyNotion, notionError, NotionPreview, NotionSource } from "./NotionSource";

const ROLES = ["fe", "be", "admin", "app"];
const NAME_RE = /^[A-Za-z0-9_.-]+$/;
const UNSORTED = "unsorted";

const Hint = ({ children }: { children: React.ReactNode }) => (
  <p className="rounded-lg border border-warning-200 bg-warning-50 p-2 text-xs text-warning-700">{children}</p>
);
const Label = ({ children }: { children: React.ReactNode }) => <span className="mb-1 block text-xs font-semibold text-text-muted">{children}</span>;

/** a repo the wizard puts in the project: a new one (checked local checkout, registered on save) or an already
 * registered one (registered: true, only linked) + its role */
type NewRepo = { name: string; github: string; description: string; path: string; role: string; registered?: boolean };

/** one GitHub repo -> local checkout (path or clone) -> onAdd. Stays mounted while other steps show (clone keeps polling). */
function RepoPicker({ taken, picked, onAdd, onPending }: {
  taken: string[]; picked: string[]; onAdd: (r: Omit<NewRepo, "role">) => void; onPending: (pending: boolean) => void;
}) {
  const [repos, setRepos] = useState<{ repos: GithubRepo[]; error?: string } | null>(null);
  const [q, setQ] = useState("");
  const [gh, setGh] = useState<GithubRepo | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<"clone" | "path">("clone");
  const [path, setPath] = useState("");
  const [check, setCheck] = useState<(PathCheck & { path: string }) | null>(null);
  const [job, setJob] = useState<CloneJob | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => onPending(!!gh), [gh]); // eslint-disable-line react-hooks/exhaustive-deps
  const loadRepos = (refresh = false) => { setRepos(null); getJson<typeof repos & object>(`/api/onboard/github-repos${refresh ? "?refresh=1" : ""}`).then(setRepos).catch((e) => setRepos({ repos: [], error: String(e) })); };
  useEffect(() => loadRepos(), []);

  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    return (repos?.repos ?? []).filter((r) => !picked.includes(r.full_name))
      .filter((r) => !t || r.full_name.toLowerCase().includes(t) || (r.description ?? "").toLowerCase().includes(t)).slice(0, 50);
  }, [repos, q, picked]);

  const reset = () => { setCheck(null); setError(""); };
  const pick = (r: GithubRepo) => {
    setGh(r); setName(r.full_name.split("/")[1]); setDescription(r.description ?? ""); setPath(""); setJob(null); reset();
  };
  const runCheck = async (p: string) => {
    if (!gh) return;
    setBusy(true);
    try {
      // remembers which path it checked: a late answer for an edited path never enables 추가
      setCheck({ ...(await postJson<PathCheck>("/api/onboard/check-path", { path: p, github: gh.full_name })), path: p.trim() });
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };
  const startClone = async () => {
    if (!gh) return;
    reset();
    setBusy(true);
    try {
      const j = await postJson<CloneJob>("/api/onboard/clone", { github: gh.full_name, name });
      setJob(j);
      setPath(j.path);
      if (j.status === "exists") await runCheck(j.path); // existing folder: never cloned over, checked like a typed path
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    if (job?.status !== "running") return;
    let live = true; // a poll answering after the job was dropped must not touch state
    const id = setInterval(async () => {
      try {
        const j = await getJson<CloneJob>(`/api/onboard/clone?id=${job.id}`);
        if (!live || j.status === "running") return;
        setJob(j);
        if (j.status === "done") void runCheck(j.path);
      } catch (e) {
        // only an unknown job (dashboard restarted) ends polling; other errors retry on the next tick
        if (live && e instanceof Error && e.message === "not found") setJob({ ...job, status: "error", error: "대시보드가 재시작돼서 클론 상태를 잃었어요. 다시 클론하면 받은 폴더를 확인해요." });
      }
    }, 2000);
    return () => { live = false; clearInterval(id); };
  }, [job]); // eslint-disable-line react-hooks/exhaustive-deps

  const nameErr = !name ? "이름을 정해 주세요" : !NAME_RE.test(name) ? "영문, 숫자, _ . - 만 써요" : taken.includes(name) ? "이미 있는 레포 이름이에요" : "";
  const cloning = job?.status === "running";
  const ready = !!gh && !nameErr && !cloning && !!check?.matches && check.path === path.trim();
  const clear = () => { setGh(null); setName(""); setDescription(""); setPath(""); setJob(null); setQ(""); reset(); };
  const add = () => { onAdd({ name, github: gh!.full_name, description, path: path.trim() }); clear(); };

  return (
    <div className="space-y-3 rounded-lg border border-border p-3">
      <div className="flex gap-2">
        <Input className="h-9 text-sm" placeholder="owner/repo 또는 설명으로 검색" value={q} onChange={(e) => setQ(e.target.value)} aria-label="GitHub 레포 검색" />
        <Button size="sm" variant="secondary" onClick={() => loadRepos(true)} aria-label="레포 목록 새로고침" title="새로고침"><RefreshCw className="h-4 w-4" /></Button>
      </div>
      {repos?.error && <Hint>GitHub 레포 목록을 못 불러왔어요: {repos.error}</Hint>}
      {!repos ? <p className="text-sm text-text-muted">불러오는 중…</p> : (
        <ul className="max-h-56 divide-y divide-border overflow-auto rounded-lg border border-border" role="listbox" aria-label="GitHub 레포">
          {shown.map((r) => (
            <li key={r.full_name}>
              <button type="button" role="option" aria-selected={gh?.full_name === r.full_name} disabled={cloning} onClick={() => pick(r)}
                className={cn("block w-full px-3 py-2 text-left text-sm transition-colors hover:bg-grey-50", gh?.full_name === r.full_name && "bg-primary-50")}>
                <span className="font-semibold text-text-main">{r.full_name}</span>
                {r.description && <span className="ml-2 text-xs text-text-muted">{r.description}</span>}
              </button>
            </li>
          ))}
          {!shown.length && <li className="px-3 py-2 text-sm text-text-muted">맞는 레포가 없어요</li>}
        </ul>
      )}
      {gh && (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block">
              <Label>레포 이름</Label>
              <Input className="h-9 text-sm" value={name} disabled={cloning} onChange={(e) => { setName(e.target.value); if (mode === "clone") { setJob(null); setPath(""); reset(); } }} />
              {nameErr && <span className="mt-1 block text-xs text-danger-700">{nameErr}</span>}
            </label>
            <label className="block">
              <Label>설명</Label>
              <Input className="h-9 text-sm" value={description} onChange={(e) => setDescription(e.target.value)} />
            </label>
          </div>
          <div className="space-y-2">
            <Label>로컬 체크아웃 — 자동 클론은 설정의 클론 위치/&lt;레포 이름&gt;에 받아요. 폴더가 이미 있으면 덮어쓰지 않고 그 폴더를 확인해요.</Label>
            <div className="flex gap-4 text-sm">
              {(["clone", "path"] as const).map((m) => (
                <label key={m} className="inline-flex cursor-pointer items-center gap-2">
                  <input type="radio" className="accent-primary" name="checkout" checked={mode === m} disabled={cloning} onChange={() => { setMode(m); setPath(""); setJob(null); reset(); }} />
                  {m === "clone" ? "자동 클론" : "기존 경로 입력"}
                </label>
              ))}
            </div>
            {mode === "clone" ? (
              <div className="space-y-2">
                <Button size="sm" disabled={busy || !!nameErr} loading={cloning} onClick={startClone}>
                  {`${gh.full_name} 클론`}
                </Button>
                {job && <p className="font-mono text-xs text-text-muted">{job.path}</p>}
                {job?.status === "exists" && <p className="text-xs text-text-muted">폴더가 이미 있어서 클론하지 않고 확인했어요.</p>}
                {job?.status === "error" && <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-danger-50 p-2 text-xs text-danger-700">{job.error}</pre>}
              </div>
            ) : (
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void runCheck(path); }}>
                <Input className="h-9 font-mono text-sm" placeholder="~/tp-work/repos/my-repo" value={path} aria-label="로컬 경로"
                  onChange={(e) => { setPath(e.target.value); reset(); }} />
                <Button size="sm" type="submit" className="shrink-0" disabled={busy || !path.trim()}>확인</Button>
              </form>
            )}
            {error && <Errors errors={[error]} />}
            {check && check.path === path.trim() && (
              check.matches ? <p className="text-sm text-success-700">git 레포이고 origin이 {gh.full_name}이에요.</p>
                : <p role="alert" className="text-sm text-danger-700">
                  {!check.exists ? "폴더가 없어요." : !check.is_git ? "git 레포의 최상위 폴더가 아니에요." : `origin이 달라요 (${check.origin ?? "없음"}). 이 경로로는 등록할 수 없어요.`}
                </p>
            )}
          </div>
          <div className="flex gap-2">
            <Button size="sm" disabled={!ready} onClick={add}><Plus className="h-4 w-4" /> 이 레포 추가</Button>
            <Button size="sm" variant="secondary" disabled={cloning} onClick={clear}>선택 취소</Button>
          </div>
        </>
      )}
    </div>
  );
}

/** 프로젝트 추가 (TP-93): 이름 -> 소스 -> 소스 설정 -> 레포 (여러 개) -> 확인, one step at a time. With projectId only the
 * 레포 step opens: new repos go into that project ("unsorted": registered without a project, 미분류).
 * Every step stays mounted (hidden), so going back keeps what was typed. Saved as one change (/api/config updates). */
export function Onboard({ projectId, onCancel, onSaved }: { projectId?: string; onCancel?: () => void; onSaved: (projectId: string) => void }) {
  const [c, setC] = useState<ConfigState | null>(null);
  const [cErr, setCErr] = useState("");
  const [asana, setAsana] = useState<{ projects: { gid: string; name: string }[]; error?: string } | null>(null);
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [newId, setNewId] = useState("");
  const [source, setSource] = useState<ProjectSource | null>(null);
  const [asanaGid, setAsanaGid] = useState("");
  const [notion, setNotion] = useState(emptyNotion);
  const [added, setAdded] = useState<NewRepo[]>([]);
  const [dflt, setDflt] = useState("");
  const [pending, setPending] = useState(false);
  const [link, setLink] = useState("");
  const [changes, setChanges] = useState<ConfigChange[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const loadAsana = (refresh = false) => { setAsana(null); getJson<typeof asana & object>(`/api/onboard/asana-projects${refresh ? "?refresh=1" : ""}`).then(setAsana).catch((e) => setAsana({ projects: [], error: String(e) })); };
  useEffect(() => {
    getJson<ConfigState>("/api/config").then(setC).catch((e) => setCErr(String(e)));
    if (!projectId) loadAsana();
  }, [projectId]);

  const unsorted = projectId === UNSORTED;
  const existing: ProjectConf | null = projectId && !unsorted ? c?.config.project[projectId] ?? null : null;
  // an existing project's default stays unless the owner picks another one here (a project without one keeps none)
  const oldDflt = existing?.repos.find((r) => r.default)?.repo ?? "";
  useEffect(() => { if (oldDflt && !dflt) setDflt(oldDflt); }, [oldDflt]); // eslint-disable-line react-hooks/exhaustive-deps

  const pid = projectId ?? (newId || slug(name));
  const idErr = !pid ? "id를 정해 주세요 (영문 소문자, 숫자, -)" : !ID_RE.test(pid) ? "영문 소문자, 숫자, - 로 40자 이내" : pid === UNSORTED || (c && pid in c.config.project) ? "이미 있는 id예요" : "";
  const usedGids = new Set(Object.values(c?.config.project ?? {}).map((p) => p.asana_gid).filter(Boolean));
  const takenRepos = [...Object.keys(c?.config.repo ?? {}), ...added.map((r) => r.name)];
  const rows = [...(existing?.repos.map((r) => ({ repo: r.repo, role: r.role ?? "", old: true })) ?? []), ...added.map((r) => ({ repo: r.name, role: r.role, old: false }))];
  const repoNames = rows.map((r) => r.repo);
  const linkable = Object.keys(c?.config.repo ?? {}).filter((n) => !repoNames.includes(n));
  const repoErr = pending ? "고른 레포를 추가하거나 선택을 취소해 주세요" : !added.length ? "레포를 하나 이상 추가해 주세요" : "";

  const sourceStep = { title: "소스 설정", err: source === "notion" ? notionError(notion) : source === "asana" && !asanaGid ? "Asana 프로젝트를 골라 주세요" : "" };
  const repoStep = { title: "레포", err: repoErr || (!rows.some((r) => r.repo === dflt) ? "기본 레포를 골라 주세요" : "") };
  const steps = projectId
    ? [{ title: "레포", err: repoErr }, { title: "확인", err: "" }]
    : [
      { title: "이름", err: !name.trim() ? "이름을 정해 주세요" : idErr },
      { title: "작업 소스", err: source ? "" : "소스를 골라 주세요" },
      sourceStep, repoStep,
      { title: "확인", err: "" },
    ];
  const last = step === steps.length - 1;

  const updates = () => [
    ...added.filter((r) => !r.registered).map((r) => ({ scope: `repo.${r.name}`, create: true, value: { path: r.path, github: r.github, description: r.description || null } })),
    ...(unsorted ? [] : [{
      scope: `project.${pid}`, create: !existing,
      value: {
        ...(existing ?? { name: name.trim(), source, asana_gid: source === "asana" ? asanaGid : null,
          ...(source === "notion" ? { notion } : {}) }),
        repos: rows.map((r) => ({ repo: r.repo, role: r.role, ...(r.repo === dflt ? { default: true } : {}) })),
      },
    }]),
  ];
  const run = async (dry: boolean) => {
    setBusy(true);
    setErrors([]);
    try {
      const r = await postConfig("/api/config", { updates: updates(), dry_run: dry });
      if (dry) setChanges(r.changes);
      else onSaved(unsorted ? added[0].name : pid);
    } catch (e) {
      setChanges(null);
      setErrors(e instanceof ConfigError ? e.errors : [String(e)]);
    } finally {
      setBusy(false);
    }
  };
  // the confirm step always shows a fresh dry run of what the other steps hold now
  useEffect(() => { if (last) void run(true); else { setChanges(null); setErrors([]); } }, [step]); // eslint-disable-line react-hooks/exhaustive-deps

  const addRepo = (r: NewRepo) => { setAdded((a) => [...a, r]); if (!dflt && !existing) setDflt(r.name); };
  const setRole = (repo: string, role: string) => setAdded((a) => a.map((r) => (r.name === repo ? { ...r, role } : r)));
  const remove = (repo: string) => {
    const next = added.filter((r) => r.name !== repo);
    setAdded(next);
    if (dflt === repo) setDflt(existing ? oldDflt : next[0]?.name ?? "");
  };

  if (!c) return <p className="text-sm text-text-muted">{cErr ? `설정을 불러오지 못했어요: ${cErr}` : "불러오는 중…"}</p>;
  if (projectId && !unsorted && !existing) return <p className="text-sm text-text-muted">설정에 없는 프로젝트예요: {projectId}</p>;

  const pane = (title: string, children: React.ReactNode) => (
    <div hidden={steps[step].title !== title} className="space-y-3">{children}</div>
  );
  return (
    <div className="max-w-3xl space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-bold text-text-main sm:text-2xl">
            {unsorted ? "레포 추가 (미분류)" : existing ? `${existing.name}에 레포 추가` : "새 프로젝트"}
          </h1>
          <p className="mt-1 text-sm text-text-muted">
            {projectId ? "GitHub 레포를 고르고 로컬 체크아웃을 확인해요." : "한 단계씩 정해요. 이전으로 돌아가도 입력은 그대로예요."} 검사, 배포 규칙, 에이전트는 등록한 뒤 설정 탭에서 바꿔요.
          </p>
        </div>
        {onCancel && <Button size="sm" variant="secondary" onClick={onCancel}>취소</Button>}
      </div>

      <ol className="flex flex-wrap gap-2" aria-label="진행 단계">
        {steps.map((s, i) => (
          <li key={s.title} aria-current={i === step ? "step" : undefined}
            className={cn("inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs",
              i === step ? "border-primary bg-primary-50 font-semibold text-primary-700" : i < step ? "border-border text-text-main" : "border-border text-text-muted")}>
            {i < step ? <Check className="h-3.5 w-3.5 text-success-700" /> : <span>{i + 1}</span>} {s.title}
          </li>
        ))}
      </ol>

      <Card className="space-y-4 p-4 sm:p-5">
        {!projectId && pane("이름", (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block">
              <Label>프로젝트 이름</Label>
              <Input className="h-9 text-sm" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
            </label>
            <label className="block">
              <Label>id (주소 #projects/&lt;id&gt;, 만든 뒤에는 못 바꿔요)</Label>
              <Input className="h-9 font-mono text-sm" value={pid} onChange={(e) => setNewId(e.target.value)} />
              {name.trim() && idErr && <span className="mt-1 block text-xs text-danger-700">{idErr}</span>}
            </label>
          </div>
        ))}

        {!projectId && pane("작업 소스", (
          <fieldset className="space-y-2">
            <legend className="mb-1 text-sm text-text-muted">티켓을 어디서 가져올까요?</legend>
            {(["asana", "github", "notion", "none"] as const).map((k) => (
              <label key={k} className="flex cursor-pointer items-start gap-2 text-sm">
                <input type="radio" className="mt-1 accent-primary" name="source" checked={source === k} onChange={() => setSource(k)} />
                <span>
                  <span className="font-semibold text-text-main">{k === "github" ? "GitHub 이슈 (tp 라벨)" : SOURCE_LABEL[k]}</span>
                  <span className="block text-xs text-text-muted">
                    {k === "asana" ? "Asana 프로젝트에서 나에게 배정된 작업" : k === "github" ? "프로젝트 레포의 열린 이슈 중 tp 라벨이 붙은 것" : k === "notion" ? "공유한 DB에서 담당자·태그·상태 조건에 맞는 페이지" : "티켓 없이 채팅과 레포 설정만 써요"}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
        ))}

        {!projectId && pane("소스 설정", source === "asana" ? (
          <>
            {asana?.error && <Hint>Asana 프로젝트 목록을 못 불러왔어요: {asana.error}</Hint>}
            <div className="flex gap-2">
              <Select size="sm" className="min-w-0 flex-1" aria-label="Asana 프로젝트" value={asanaGid} onChange={(e) => setAsanaGid(e.target.value)} disabled={!asana}>
                <option value="">{asana ? "Asana 프로젝트 고르기" : "불러오는 중…"}</option>
                {(asana?.projects ?? []).filter((p) => p.gid === asanaGid || !usedGids.has(p.gid)).map((p) => <option key={p.gid} value={p.gid}>{p.name}</option>)}
              </Select>
              <Button size="sm" variant="secondary" onClick={() => loadAsana(true)} aria-label="Asana 목록 새로고침" title="새로고침"><RefreshCw className="h-4 w-4" /></Button>
            </div>
            <p className="text-xs text-text-muted">다른 프로젝트에 이미 연결된 Asana 프로젝트는 빠져 있어요.</p>
          </>
        ) : source === "notion" ? (
          <NotionSource projectId={pid} value={notion} repos={repoNames.length ? repoNames : undefined}
            workTag={c.config.global.asana_tag} onChange={setNotion} />
        ) : source === "github" ? (
          <p className="text-sm text-text-main">
            다음 단계에서 고르는 레포마다 <code className="rounded bg-grey-50 px-1 font-mono">tp</code> 라벨이 붙은 열린 이슈가 티켓으로 들어와요.
            라벨을 떼거나 이슈를 닫으면 보드에서 빠지고, 배포가 끝나면 이슈에 댓글을 남기고 닫아요. 레포에 tp 라벨이 있는지 GitHub에서 확인해 주세요.
          </p>
        ) : (
          <p className="text-sm text-text-muted">소스가 없어서 설정할 게 없어요. 다음으로 넘어가요.</p>
        ))}

        {pane("레포", (
          <>
            {rows.length > 0 && (
              <ul className="divide-y divide-border rounded-lg border border-border" aria-label="프로젝트 레포">
                {rows.map((r) => (
                  <li key={r.repo} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                    <span className="min-w-0 flex-1 truncate font-mono text-text-main">{r.repo}{r.old && <span className="ml-1 font-sans text-xs text-text-muted">(프로젝트에 있음)</span>}</span>
                    {!unsorted && (
                      <>
                        {r.old ? <span className="text-xs text-text-muted">{r.role}</span> : (
                          <Select size="sm" aria-label={`${r.repo} role`} value={r.role} onChange={(e) => setRole(r.repo, e.target.value)}>
                            {ROLES.map((x) => <option key={x}>{x}</option>)}
                          </Select>
                        )}
                        <label className="inline-flex items-center gap-1 text-xs text-text-muted">
                          <input type="radio" className="accent-primary" name="default-repo" checked={dflt === r.repo} onChange={() => setDflt(r.repo)} />
                          기본 레포
                        </label>
                      </>
                    )}
                    {!r.old && (
                      <button type="button" aria-label={`${r.repo} 빼기`} className="rounded p-1 text-text-muted hover:bg-grey-50 hover:text-danger-700" onClick={() => remove(r.repo)}>
                        <Trash2 className="h-4 w-4" />
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {!unsorted && <p className="text-xs text-text-muted">기본 레포의 체크아웃에서 채팅이 돌고, 나머지 레포는 함께 열려요.</p>}
            {!unsorted && linkable.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <Select size="sm" aria-label="등록된 레포" value={link} onChange={(e) => setLink(e.target.value)}>
                  <option value="">이미 등록된 레포 넣기…</option>
                  {linkable.map((n) => <option key={n} value={n}>{n}</option>)}
                </Select>
                <Button size="sm" variant="secondary" disabled={!link}
                  onClick={() => { addRepo({ name: link, github: c.config.repo[link].github ?? "", description: "", path: "", role: "app", registered: true }); setLink(""); }}>
                  <Plus className="h-4 w-4" /> 넣기
                </Button>
              </div>
            )}
            <RepoPicker taken={takenRepos} picked={added.map((r) => r.github)} onPending={setPending}
              onAdd={(r) => addRepo({ ...r, role: "app" })} />
          </>
        ))}

        {pane("확인", (
          <>
            {!projectId && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                <dt className="text-text-muted">이름</dt><dd className="text-text-main">{name} <span className="font-mono text-xs text-text-muted">({pid})</span></dd>
                <dt className="text-text-muted">소스</dt><dd className="text-text-main">{source && SOURCE_LABEL[source]}{source === "asana" && ` · ${asana?.projects.find((p) => p.gid === asanaGid)?.name ?? asanaGid}`}</dd>
                <dt className="text-text-muted">레포</dt><dd className="font-mono text-text-main">{rows.map((r) => `${r.repo}${r.repo === dflt ? " (기본)" : ""}`).join(", ")}</dd>
              </dl>
            )}
            {last && !projectId && source === "notion" && <NotionPreview projectId={pid} value={notion} repos={repoNames} />}
            <Errors errors={errors} />
            {busy && !changes && <p className="text-sm text-text-muted">확인 중…</p>}
            {changes && (
              <>
                <p className="text-sm font-semibold text-text-main">이 {changes.length}개 항목을 저장할까요?</p>
                <ChangeTable changes={changes} />
              </>
            )}
          </>
        ))}

        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
          {step > 0 && <Button size="sm" variant="secondary" disabled={busy} onClick={() => setStep(step - 1)}>이전</Button>}
          {last ? (
            <Button size="sm" disabled={busy || !changes} onClick={() => run(false)}>저장</Button>
          ) : (
            <Button size="sm" disabled={!!steps[step].err} onClick={() => setStep(step + 1)}>다음</Button>
          )}
          {!last && steps[step].err && <span className="text-xs text-text-muted">{steps[step].err}</span>}
          {!projectId && steps[step].title === "레포" && <span className="text-xs text-text-muted">레포는 몇 개든 추가할 수 있어요.</span>}
        </div>
      </Card>
    </div>
  );
}
