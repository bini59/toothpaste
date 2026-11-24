import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Download, Play, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { Badge, Button, Card, Input, cn } from "../ui";
import { ConfigError, fetchConfig, getJson, post, postConfig, postJson } from "../api";
import { Select } from "../components/Select";
import { Table } from "../components/Table";
import { ago, type Status } from "../lib";
import type { AgentAccount, AgentAccounts, AgentConf, CodexLogin, AsanaStatus, CheckCmd, ConfigChange, ConfigState, DeployRule, GitHubDevice, GitHubStatus, GlobalConf, HistoryRow, ProjectConf, ProjectSource, RepoConf, State, TrialRun } from "../types";
import { ConfirmButton } from "../components/ConfirmButton";
import { Update } from "./Update";
import { ModeSection } from "./Mode";
import { emptyNotion, NotionAccounts, NotionSource } from "./NotionSource";
import { LanShare, Tunnel } from "./Lan";
import { applyTheme, loadThemes, type ThemeList } from "../theme/theme";

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
/** The top-level keys edited here: orig is the page-load copy, and sending it whole would undo values saved
 * elsewhere since (the workspace picked in AsanaConnect, TP-148). */
const changed = <T extends object>(d: T, orig: T) =>
  Object.fromEntries(Object.entries(d).filter(([k, v]) => !same(v, (orig as Record<string, unknown>)[k])));
/** number inputs: "" -> null, a number -> number, anything else stays text so the server can say why it is wrong */
const num = (v: string): number | string | null => (v.trim() === "" ? null : Number.isFinite(Number(v)) ? Number(v) : v);
const show = (v: unknown) => (v === null || v === undefined ? "없음" : typeof v === "string" ? v : JSON.stringify(v));
const fmt = (v: string) => new Date(v).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
const DAYS = ["월", "화", "수", "목", "금", "토", "일"];
const TRIAL: Record<TrialRun["status"], [string, Status]> = {
  running: ["실행 중", "info"],
  pass: ["통과", "success"],
  fail: ["실패", "danger"],
  error: ["실행 못 함", "warning"],
};
const RESULT: Record<string, Status> = { pass: "success", flaky: "warning", fail: "danger", timeout: "danger", error: "warning" };
export const ACTOR: Record<string, string> = { dashboard: "대시보드", cli: "CLI", import: "가져오기" };

/** one setting as a row: title and description on the left, the control on the right (stacked on narrow screens).
 * A <label> around its one control; `group` for rows that hold several controls. */
export function Row({ label, desc, children, group }: { label: string; desc?: React.ReactNode; children: React.ReactNode; group?: boolean }) {
  const Tag = group ? "div" : "label";
  const Box = group ? "div" : "span";
  return (
    <Tag role={group ? "group" : undefined} aria-label={group ? label : undefined}
      className="grid gap-x-6 gap-y-2 py-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <Box className="block min-w-0 sm:pt-2">
        <span className="block text-sm font-semibold text-text-main">{label}</span>
        {desc && <span className="mt-1 block text-xs leading-relaxed text-text-muted">{desc}</span>}
      </Box>
      <Box className="block min-w-0">{children}</Box>
    </Tag>
  );
}
/** content of a section that is not a setting row (a list, a table, a note) */
function Block({ children }: { children: React.ReactNode }) {
  return <div className="py-4">{children}</div>;
}
/** the settings key, small, for looking a value up with `tp config` */
function Key({ k }: { k: string }) {
  return <code className="font-mono">{k}</code>;
}
const boxCls = "h-4 w-4 accent-primary";
function Check({ label, checked, onChange, disabled }: { label: React.ReactNode; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className={cn("inline-flex items-center gap-2 text-sm text-text-main", disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer")}>
      <input type="checkbox" className={boxCls} checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}
/** an on/off setting: the whole row is the checkbox's label */
function CheckRow({ label, desc, checked, onChange }: { label: string; desc?: React.ReactNode; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <Row label={label} desc={desc}>
      <span className="flex h-9 items-center">
        <input type="checkbox" className={cn(boxCls, "cursor-pointer")} checked={checked} onChange={(e) => onChange(e.target.checked)} />
      </span>
    </Row>
  );
}
export function Section({ title, desc, aside, children }: { title: string; desc?: React.ReactNode; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <Card className="p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-base font-bold text-text-main">{title}</h2>
          {desc && <p className="mt-1 text-xs leading-relaxed text-text-muted">{desc}</p>}
        </div>
        {aside}
      </div>
      <div className="mt-3 divide-y divide-border border-t border-border">{children}</div>
    </Card>
  );
}

/** [address key, label, the top-level setting keys the section edits (for its unsaved mark)] */
type SectionDef = readonly [string, string, readonly string[]];
/** the section an address names, "" when it names none (then wide screens show the first, narrow ones the list) */
const sectionOf = (defs: readonly SectionDef[], key: string) => (defs.some((x) => x[0] === key) ? key : "");
/**
 * The chat page's layout for settings (TP-125): the section list on the left, the one chosen section on the right.
 * Below lg: only the list, or only the chosen section with a link back to the list.
 */
function Sectioned({ label, defs, cur, href, d, orig, children }: {
  label: string; defs: readonly SectionDef[]; cur: string; href: (key: string) => string; d: unknown; orig: unknown; children: React.ReactNode;
}) {
  const a = d as Record<string, unknown>, b = orig as Record<string, unknown>;
  return (
    <div className="lg:flex lg:items-start lg:gap-6">
      <nav aria-label={label} className={cn("space-y-0.5 lg:sticky lg:top-20 lg:w-52 lg:shrink-0", cur && "hidden lg:block")}>
        {defs.map(([key, name, keys], i) => {
          const dirty = keys.some((k) => !same(a[k], b[k]));
          return (
            <a key={key} href={href(key)} aria-current={key === cur ? "page" : undefined}
              className={cn("flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                key === cur ? "bg-primary-50 text-primary-700"
                  // no section in the address: wide screens show the first one
                  : !cur && i === 0 ? "text-text-main hover:bg-grey-50 lg:bg-primary-50 lg:text-primary-700" : "text-text-main hover:bg-grey-50")}>
              <span className="min-w-0 flex-1 truncate">{name}</span>
              {dirty && <span className="h-2 w-2 shrink-0 rounded-full bg-warning-500" role="img" aria-label="저장 안 한 변경" title="저장 안 한 변경" />}
            </a>
          );
        })}
      </nav>
      <div className={cn("min-w-0 flex-1", !cur && "hidden lg:block")}>
        {cur && <a href={href("")} className="mb-3 inline-block text-xs font-semibold text-primary-700 lg:hidden">← {label}</a>}
        {children}
      </div>
    </div>
  );
}

/** GitHub 연결 (TP-112, Device Flow TP-143): tp and its agents use this OAuth token for gh and git push, not the owner's gh login */
export function GitHubConnect({ onStatus }: { onStatus?: (connected: boolean) => void }) {
  const [st, setSt] = useState<GitHubStatus | null>(null);
  const [err, setErr] = useState("");
  const [dev, setDev] = useState<GitHubDevice | null>(null);
  const [copied, setCopied] = useState(false);
  const load = useCallback(() => {
    getJson<GitHubStatus>("/api/github/status").then((x) => { setSt(x); onStatus?.(x.connected); }, (e: Error) => setErr(e.message));
  }, [onStatus]);
  useEffect(load, [load]);
  // poll at GitHub's interval (slow_down raises it) until connected, expired or denied
  useEffect(() => {
    if (!dev) return;
    let stop = false;
    let t = 0;
    const tick = (s: number) => {
      t = window.setTimeout(async () => {
        try {
          const r = await postJson<{ pending?: boolean; interval?: number; connected?: boolean }>("/api/github/device/poll", {});
          if (stop) return;
          if (r.pending) return tick(r.interval ?? s);
          setDev(null);
          load();
        } catch (e) {
          if (stop) return;
          setDev(null);
          setErr((e as Error).message);
        }
      }, s * 1000);
    };
    tick(dev.interval);
    return () => {
      stop = true;
      clearTimeout(t);
    };
  }, [dev, load]);
  const connect = async () => {
    setErr("");
    setCopied(false);
    try {
      setDev(await postJson<GitHubDevice>("/api/github/device", {}));
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const copy = () => navigator.clipboard.writeText(dev?.user_code ?? "").then(() => setCopied(true), () => setCopied(false));
  const disconnect = async () => {
    try {
      await post("/api/github/disconnect", {});
      setErr("");
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <Section title="GitHub 연결" desc="tp와 티켓 에이전트는 이 계정의 토큰으로 gh와 git push를 실행해요. 이 컴퓨터의 gh 로그인은 쓰지 않아요."
      aside={st?.connected ? (
      <ConfirmButton size="sm" variant="secondary" confirmLabel="연결 해제" onConfirm={disconnect}
        message="연결을 해제하면 GitHub를 쓰는 단계(PR, 검사, 머지, 에이전트 push)가 다시 연결할 때까지 멈춰요.">
        연결 해제
      </ConfirmButton>
    ) : (
      <Button size="sm" onClick={connect}>{dev ? "새 코드 받기" : err ? "다시 연결" : "GitHub 연결"}</Button>
    )}>
      <Block>
      <div className="flex flex-wrap items-center gap-2 text-sm text-text-main">
        {!st ? (
          <span className="text-text-muted">확인하는 중…</span>
        ) : st.connected ? (
          <>
            <Badge status={st.error ? "warning" : "success"}>연결됨</Badge>
            {st.login && <span className="font-mono">{st.login}</span>}
            {!!st.scopes?.length && <span className="text-xs text-text-muted">scope: {st.scopes.join(", ")}</span>}
          </>
        ) : (
          <>
            <Badge status="draft">연결 안 됨</Badge>
            <span className="text-text-muted">연결하기 전에는 tp와 에이전트가 GitHub 단계에서 멈춰요.</span>
          </>
        )}
      </div>
      {dev && (
        <div className="mt-3 rounded-lg border border-border bg-grey-50 p-4">
          <p className="text-sm text-text-main">GitHub에서 이 코드를 입력하세요. 입력하면 자동으로 연결돼요.</p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <span className="font-mono text-3xl font-bold tracking-widest text-text-main">{dev.user_code}</span>
            <Button size="sm" variant="secondary" onClick={copy}>{copied ? "복사됨" : "복사"}</Button>
            <a href={dev.verification_uri} target="_blank" rel="noreferrer"
              className="text-sm font-semibold text-primary-700 underline">GitHub에서 입력</a>
          </div>
          <p className="mt-2 text-xs text-text-muted" role="status">기다리는 중… 코드는 {Math.round(dev.expires_in / 60)}분 동안 쓸 수 있어요.</p>
        </div>
      )}
      {(err || st?.error) && (
        <p role="alert" className="mt-3 rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700">{err || st?.error}</p>
      )}
      </Block>
    </Section>
  );
}

/** Asana 연결 (TP-115): the pasted PAT is checked against Asana, then kept in the keychain (never sent back) */
export function AsanaConnect({ onStatus }: { onStatus?: (ready: boolean) => void }) {
  const [st, setSt] = useState<AsanaStatus | null>(null);
  const [err, setErr] = useState("");
  const [tok, setTok] = useState("");
  const [busy, setBusy] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const load = useCallback(() => {
    getJson<AsanaStatus>("/api/asana/status").then((x) => { setSt(x); onStatus?.(x.enabled === false || (x.connected && !!x.workspace)); }, (e: Error) => setErr(e.message));
  }, [onStatus]);
  useEffect(load, [load]);
  const act = async (path: string, body: unknown) => {
    setBusy(true);
    setErr("");
    try {
      await (path === "/api/config" ? postConfig(path, body) : post(path, body));
      setErr("");
      setTok("");
      setReplacing(false);
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const enabled = st?.enabled !== false;
  const form = enabled && (!st?.connected || replacing);
  return (
    <Section title="Asana 연결" desc="Asana를 꺼도 GitHub 이슈와 Notion 동기화는 계속돼요."
      aside={st && (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => void act("/api/config", { scope: "global", value: { asana_enabled: !enabled } })}>{enabled ? "Asana 사용 안 함" : "Asana 사용"}</Button>
          {enabled && st.connected && !replacing && <>
          <Button size="sm" variant="secondary" onClick={() => setReplacing(true)}>교체</Button>
          <ConfirmButton size="sm" variant="secondary" confirmLabel="연결 해제" onConfirm={() => act("/api/asana/disconnect", {})}
            message="연결을 해제하면 Asana 단계(티켓 가져오기, 완료 처리)가 PAT를 다시 등록할 때까지 멈춰요.">
            연결 해제
          </ConfirmButton>
          </>}
        </div>
      )}>
      <Block>
      <div className="flex flex-wrap items-center gap-2 text-sm text-text-main">
        {!st ? (
          <span className="text-text-muted">확인하는 중…</span>
        ) : !enabled ? (
          <><Badge status="draft">사용 안 함</Badge><span className="text-text-muted">Asana 연결을 통한 동기화와 요청을 중지했어요.</span></>
        ) : st.connected ? (
          <>
            <Badge status={st.error ? "warning" : "success"}>연결됨</Badge>
            {st.name && <span>{st.name}</span>}
            {st.email && <span className="text-xs text-text-muted">{st.email}</span>}
            {st.workspace && <span className="text-xs text-text-muted">워크스페이스 {st.workspaces?.find((w) => w.gid === st.workspace)?.name || st.workspace}</span>}
            {st.env && <span className="text-xs text-text-muted">환경변수 ASANA_PAT가 있어 그 값을 써요. 여기서 저장·해제해도 바뀌지 않아요</span>}
          </>
        ) : (
          <>
            <Badge status="draft">연결 안 됨</Badge>
            <span className="text-text-muted">Asana 없이도 GitHub 이슈를 동기화할 수 있어요.</span>
          </>
        )}
      </div>
      {st?.connected && !!st.workspaces?.length && (st.workspaces.length > 1 || !st.workspaces.some((w) => w.gid === st.workspace)) && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-text-main">
          <span>워크스페이스</span>
          {/* saved through the normal settings path, so settings_history keeps it; the next sync uses it */}
          <Select className={selectCls} aria-label="Asana 워크스페이스" value={st.workspace || ""} disabled={busy}
            onChange={(e) => e.target.value && void act("/api/config", { scope: "global", value: { asana_workspace_gid: e.target.value } })}>
            {!st.workspace && <option value="">워크스페이스 고르기…</option>}
            {st.workspace && !st.workspaces.some((w) => w.gid === st.workspace) && <option value={st.workspace}>현재 값 ({st.workspace})</option>}
            {st.workspaces.map((w) => <option key={w.gid} value={w.gid}>{w.name || w.gid}</option>)}
          </Select>
          {!st.workspace && <span className="text-xs text-text-muted">고르기 전에는 tp가 Asana 티켓을 가져오지 않아요.</span>}
        </div>
      )}
      {st && form && (
        <form className="mt-3 flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); void act("/api/asana/token", { token: tok }); }}>
          <Input type="password" autoComplete="off" aria-label="Asana PAT" placeholder="Asana PAT 붙여넣기" className={cn(inputCls, "w-80 font-mono")}
            value={tok} onChange={(e) => setTok(e.target.value)} />
          <Button size="sm" type="submit" disabled={!tok.trim()} loading={busy}>저장</Button>
          {replacing && <Button size="sm" variant="secondary" type="button" onClick={() => { setReplacing(false); setTok(""); }}>취소</Button>}
          <a href="https://app.asana.com/0/my-apps" target="_blank" rel="noreferrer" className="text-xs font-semibold text-primary-700 hover:underline">PAT 발급받기</a>
        </form>
      )}
      {(err || st?.error) && (
        <p role="alert" className="mt-3 rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700">{err || st?.error}</p>
      )}
      </Block>
    </Section>
  );
}

const text = (v: unknown) => (v === null || v === undefined ? "" : String(v));
const inputCls = "h-9 text-sm";
const numCls = "h-9 w-28 text-sm";
const selectCls = "h-9 w-full sm:w-72";
const areaCls = "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text-main outline-none hover:border-grey-300 focus:border-primary focus:ring-2 focus:ring-primary-200";

/** before -> after rows for the confirm step (also used for imports) */
export function ChangeTable({ changes }: { changes: ConfigChange[] }) {
  return (
    <Table head={["범위", "항목", "이전 값", "새 값"]}>
      {changes.map((c, i) => (
        <tr key={i}>
          <td className="whitespace-nowrap text-text-muted">{c.scope}</td>
          <td className="font-mono text-xs">{c.key}</td>
          <td className="max-w-[320px] break-all font-mono text-xs text-danger-700">{show(c.old)}</td>
          <td className="max-w-[320px] break-all font-mono text-xs text-success-700">{show(c.new)}</td>
        </tr>
      ))}
    </Table>
  );
}
export function Errors({ errors }: { errors: string[] }) {
  if (!errors.length) return null;
  return (
    <div role="alert" className="rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700">
      <p className="font-semibold">저장하지 않았어요. 아래를 고쳐 주세요.</p>
      <ul className="mt-1 list-disc pl-5">
        {errors.map((e) => (
          <li key={e}>{e}</li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Save flow: 저장 -> the server validates and returns the changes (dry run) -> the owner confirms -> saved, recorded in
 * the history and backed up. Invalid values come back as errors and nothing is saved.
 */
function SaveBar({ dirty, run, onReset, onSaved }: {
  dirty: boolean;
  run: (dry: boolean) => Promise<{ changes: ConfigChange[] }>;
  onReset: () => void;
  onSaved: () => void;
}) {
  const [changes, setChanges] = useState<ConfigChange[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  useEffect(() => {
    setChanges(null);
    setErrors([]);
    if (dirty) setDone("");
  }, [dirty]);
  const go = async (dry: boolean) => {
    setBusy(true);
    setErrors([]);
    try {
      const r = await run(dry);
      if (dry) {
        if (r.changes.length) setChanges(r.changes);
        else setDone("바뀐 값이 없어요");
      } else {
        setChanges(null);
        setDone(`${r.changes.length}개 항목을 저장했어요 · 이력과 백업에 남았고, 다음 tick부터 적용돼요`);
        onSaved();
      }
    } catch (e) {
      setChanges(null);
      setErrors(e instanceof ConfigError ? e.errors : [String(e)]);
    } finally {
      setBusy(false);
    }
  };
  if (!dirty && !done && !errors.length) return null;
  return (
    <Card className="sticky bottom-3 z-10 mt-6 space-y-3 p-4">
      <Errors errors={errors} />
      {changes ? (
        <>
          <p className="text-sm font-semibold text-text-main">이 {changes.length}개 항목을 바꿀까요?</p>
          <ChangeTable changes={changes} />
          <div className="flex gap-2">
            <Button size="sm" disabled={busy} onClick={() => go(false)}>
              저장
            </Button>
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => setChanges(null)}>
              취소
            </Button>
          </div>
        </>
      ) : dirty ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-auto text-sm text-text-muted">저장하지 않은 변경이 있어요</span>
          <Button size="sm" variant="secondary" disabled={busy} onClick={onReset}>
            되돌리기
          </Button>
          <Button size="sm" loading={busy} onClick={() => go(true)}>
            변경 확인 후 저장
          </Button>
        </div>
      ) : (
        done && <p className="text-sm text-success-700">{done}</p>
      )}
    </Card>
  );
}

export function History({ rows, scope }: { rows: HistoryRow[]; scope?: boolean }) {
  if (!rows.length) return <p className="text-sm text-text-muted">아직 바뀐 적이 없어요.</p>;
  return (
    <Table head={["시각", "누가", ...(scope ? ["범위"] : []), "항목", "이전 → 새 값"]}>
      {rows.map((h) => (
        <tr key={h.id}>
          <td className="whitespace-nowrap text-text-muted">{fmt(h.ts)}</td>
          <td className="whitespace-nowrap">{ACTOR[h.actor] ?? h.actor}</td>
          {scope && <td className="whitespace-nowrap">{h.scope}</td>}
          <td className="font-mono text-xs">{h.key}</td>
          <td className="max-w-[520px] break-all font-mono text-xs">
            <span className="text-danger-700">{show(h.old)}</span> → <span className="text-success-700">{show(h.new)}</span>
          </td>
        </tr>
      ))}
    </Table>
  );
}

// ---------------------------------------------------------------- repo sections

function Basics({ d, set, info, others }: { d: RepoConf; set: (f: (d: RepoConf) => void) => void; info?: ConfigState["repo_info"][string]; others: string[] }) {
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

/** null when every field is empty, so an untouched card keeps no entry (it follows the next level down) */
const agentOf = (n: AgentConf): AgentConf | null => {
  const kept = Object.fromEntries(Object.entries(n).filter(([, v]) => v !== null && v !== undefined && v !== "" && v !== false));
  return Object.keys(kept).length ? kept : null;
};

/**
 * tool / model / effort / ultracode rows for one agent setting: a repo's (repo.<name>.agent) or a role card's
 * (global.agents.<role>, TP-168). `inherit` = what an empty tool follows; a tool the account check (설정 > 에이전트)
 * found logged out is disabled, unless it is the saved value.
 */
function AgentFields({ a, onChange, c, inherit, accounts }: {
  a: AgentConf; onChange: (v: AgentConf | null) => void; c: ConfigState; inherit: { label: string; tool: string }; accounts?: AgentAccounts | null;
}) {
  const tool = a.tool || inherit.tool;
  const models = (tool === "agy" ? accounts?.tools.agy?.models : undefined) ?? c.agent_models?.[tool] ?? [];
  const listed = models.find((m) => m.id === a.model);
  // a saved model the list does not have (hidden in the catalog, a full claude name) stays editable as typed text
  const [typing, setTyping] = useState(false);
  const customModel = typing || (!!a.model && !listed);
  const efforts = listed?.efforts?.length ? listed.efforts : c.agent_efforts[tool] || [];
  const save = (f: (n: AgentConf) => void) => { const n: AgentConf = { ...a }; f(n); onChange(agentOf(n)); };
  const pickTool = (v: string) => {
    setTyping(false);
    // model names and effort levels differ per CLI: start the rest over for the new tool
    save((n) => { n.tool = v || null; n.model = null; n.effort = null; n.ultracode = null; });
  };
  const pickModel = (v: string) => {
    if (v === "__typed") { setTyping(true); return; }
    setTyping(false);
    const m = models.find((x) => x.id === v);
    save((n) => {
      n.model = v || null;
      const ok = m?.efforts?.length ? m.efforts : c.agent_efforts[tool] || [];
      if (n.effort && (!ok.includes(n.effort) || tool === "agy" && m?.default_effort)) n.effort = null;
    });
  };
  const out = (x: string) => accounts?.tools[x] && !accounts.tools[x].logged_in;
  const defaultHint = listed?.default_effort ? " · 이 모델 기본값 " + listed.default_effort : "";
  const modelHint = {
    claude: "별칭은 그 계열의 최신 모델 · 전체 이름은 직접 입력",
    codex: models.length ? "codex에 로그인한 계정이 쓸 수 있는 모델 (codex debug models)" : "모델 목록을 못 불러왔어요 · 직접 입력",
    agy: accounts?.tools.agy?.neon_reason || "연결된 Antigravity 계정의 모델 (agy models)",
  }[tool];
  const defaultModel = { claude: "Claude Code 설정 따름", codex: "~/.codex/config.toml 따름", agy: "Antigravity CLI 설정 따름" }[tool];
  return (
    <>
      <Row label="도구" desc={`비우면 ${inherit.label} (${inherit.tool})`}>
        <Select className={selectCls} value={a.tool || ""} onChange={(e) => pickTool(e.target.value)}>
          <option value="">{inherit.label} 따름 ({inherit.tool})</option>
          {c.agents.map((x) => <option key={x} value={x} disabled={out(x) && a.tool !== x}>{x}{out(x) ? " (로그인 안 됨)" : ""}</option>)}
        </Select>
      </Row>
      <Row group label="모델" desc={modelHint}>
        <Select className={selectCls} value={tool !== "agy" && customModel ? "__typed" : a.model || ""} onChange={(e) => pickModel(e.target.value)} aria-label="모델">
          <option value="">{defaultModel}</option>
          {models.map((m) => <option key={m.id} value={m.id}>{m.name === m.id ? m.id : m.name + " (" + m.id + ")"}</option>)}
          {tool === "agy" ? (a.model && !listed && <option value={a.model} disabled>{a.model} (현재 목록에 없음)</option>)
            : <option value="__typed">직접 입력…</option>}
        </Select>
        {tool !== "agy" && customModel && (
          <Input className={cn(inputCls, "mt-2 sm:w-72")} value={text(a.model)} placeholder={tool === "claude" ? "claude-opus-5-5" : "gpt-6.1-sol"} aria-label="모델 이름"
            onChange={(e) => { const v = e.target.value.trim(); save((n) => { n.model = v || null; }); }} />
        )}
      </Row>
      {!(tool === "agy" && listed?.default_effort) && <Row label="추론 강도" desc={"비우면 CLI 설정 그대로" + defaultHint}>
        <Select className={selectCls} value={a.effort || ""} onChange={(e) => { const v = e.target.value; save((n) => { n.effort = v || null; }); }}>
          <option value="">CLI 설정 따름</option>
          {efforts.map((x) => <option key={x} value={x}>{x}</option>)}
        </Select>
      </Row>}
      {tool === "claude" && (
        <CheckRow label="ultracode" checked={!!a.ultracode} onChange={(v) => save((n) => { n.ultracode = v || null; })}
          desc="Claude가 큰 작업을 여러 하위 에이전트로 나눠 병렬로 처리해요. 결과는 더 꼼꼼해지지만 사용량이 크게 늘어요. dynamic workflows가 켜진 계정에서, xhigh를 지원하는 모델일 때만 실제로 켜져요." />
      )}
    </>
  );
}

function AgentSection({ d, set, c }: { d: RepoConf; set: (f: (d: RepoConf) => void) => void; c: ConfigState }) {
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

function Checks({ name, d, set, c, onReload }: { name: string; d: RepoConf; set: (f: (d: RepoConf) => void) => void; c: ConfigState; onReload: () => void }) {
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

function Review({ d, set, c, info }: { d: RepoConf; set: (f: (d: RepoConf) => void) => void; c: ConfigState; info?: ConfigState["repo_info"][string] }) {
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

function Deploy({ d, set, c, s }: { d: RepoConf; set: (f: (d: RepoConf) => void) => void; c: ConfigState; s: State }) {
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

/** the repo scope without links: the project settings own which repos a project has (a stale copy here would undo them) */
const noLinks = ({ links: _links, ...rest }: RepoConf) => rest;

const REPO_SECTIONS: SectionDef[] = [
  ["basics", "기본", ["path", "github", "description", "worktree_base", "auto_setup", "setup", "dev", "depends_on"]],
  ["agent", "에이전트", ["agent"]],
  ["checks", "검사", ["checks", "ci"]],
  ["review", "리뷰", ["review"]],
  ["deploy", "배포", ["deploy"]],
];

function RepoEditor({ name, c, s, section, href, onReload }: { name: string; c: ConfigState; s: State; section: string; href: (section: string) => string; onReload: () => void }) {
  const orig = c.config.repo[name];
  const [d, setD] = useState<RepoConf>(() => clone(orig));
  const origRef = useRef(orig);
  useEffect(() => {
    // a reload after saving (or a trial run poll) brings fresh values; keep unsaved edits otherwise
    setD((cur) => (same(cur, origRef.current) ? clone(orig) : cur));
    origRef.current = orig;
  }, [orig]);
  const set = (f: (d: RepoConf) => void) => setD((cur) => { const n = clone(cur); f(n); return n; });
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
      <SaveBar dirty={!same(d, orig)} onReset={() => setD(clone(orig))} onSaved={onReload}
        run={(dry) => postConfig("/api/config", { scope: `repo.${name}`, value: noLinks(d), dry_run: dry })} />
    </div>
  );
}

// ---------------------------------------------------------------- 전역

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

const METHOD: Record<string, string> = { cli: "CLI 로그인", oauth_token: "tp 토큰", api_key: "API 키" };
const errBox = "mt-3 rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700";

/** 에이전트 로그인 buttons of one tool (TP-179): codex Device Flow; claude setup-token, a pasted token or an API key.
 * Codes, tokens and keys go to the server and are never shown again. */
function ToolLogin({ tool, t, onChanged }: { tool: string; t: AgentAccount; onChanged: () => void }) {
  const [codex, setCodex] = useState<CodexLogin | null>(null);
  const [oauthUrl, setOAuthUrl] = useState("");
  const [form, setForm] = useState<"" | "token" | "apikey">("");
  const [val, setVal] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);
  const base = `/api/agents/${tool}`;
  const run = async (what: string, f: () => Promise<unknown>) => {
    setBusy(what);
    setErr("");
    try {
      await f();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  // codex: poll until codex login status says done, or the code expires / the login fails
  useEffect(() => {
    if (codex?.state !== "pending") return;
    const id = window.setInterval(() => {
      getJson<CodexLogin>(`${base}/login`).then((r) => {
        if (r.state === "pending") return;
        setCodex(null);
        // idle: another tab or poll already took the result; the refreshed status shows it
        if (r.state === "done" || r.state === "idle") onChanged();
        else setErr(r.error || "로그인하지 못했어요. 다시 해 주세요");
      }, (e: Error) => { setCodex(null); setErr(e.message); });
    }, 3000);
    return () => clearInterval(id);
  }, [codex, base, onChanged]);
  const start = () => run("login", async () => {
    setCopied(false);
    const r = await postJson<CodexLogin>(`${base}/login`, {});
    if (tool === "codex") setCodex(r);
    else if (r.state === "done") onChanged();
    else setOAuthUrl(r.url || "");
  });
  const cancel = () => run("cancel", async () => {
    await post(`${base}/login/cancel`, {});
    setCodex(null);
    setOAuthUrl("");
    setVal("");
  });
  const submit = (path: string, body: unknown) => run("submit", async () => {
    try {
      await post(`${base}/${path}`, body);
    } finally {
      // a wrong code ends the server's setup-token too: the form goes with it (log in again)
      if (path === "login/code") { setOAuthUrl(""); setVal(""); }
    }
    setVal("");
    setForm("");
    setOAuthUrl("");
    onChanged();
  });
  const logout = () => run("logout", async () => { await post(`${base}/logout`, {}); onChanged(); });
  const cliOk = t.method === "cli";
  const pending = !!codex || !!oauthUrl;
  const secretForm = (label: string, ph: string, path: string, key: string) => (
    <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); void submit(path, { [key]: val }); }}>
      <Input type="password" autoComplete="off" aria-label={label} placeholder={ph} className={cn(inputCls, "w-72 font-mono")}
        value={val} onChange={(e) => setVal(e.target.value)} />
      <Button size="sm" type="submit" disabled={!val.trim()} loading={busy === "submit"}>{path === "login/code" ? "확인" : "저장"}</Button>
      <Button size="sm" variant="secondary" type="button" onClick={() => (path === "login/code" ? void cancel() : (setForm(""), setVal("")))}>취소</Button>
    </form>
  );
  return (
    <div className="space-y-2">
      {!pending && !form && (
        <div className="flex flex-wrap items-center gap-2">
          {(tool === "codex" ? t.logged_in : !!t.saved) ? (
            <ConfirmButton size="sm" variant="secondary" confirmLabel="로그아웃" onConfirm={() => void logout()} loading={busy === "logout"}
              message={tool === "codex" ? "codex logout을 실행해요. 다시 로그인할 때까지 codex 에이전트가 멈춰요."
                : tool === "agy" ? "tp에 저장한 Gemini API 키 연결을 해제해요."
                  : "tp에 저장한 토큰·키만 지워요. claude CLI 자체 로그인(~/.claude, 키체인)은 그대로예요."}>
              로그아웃
            </ConfirmButton>
          ) : (!t.logged_in || tool === "agy" && cliOk) && (
            <>
              {!t.logged_in && <Button size="sm" onClick={() => void start()} loading={busy === "login"}>
                {tool === "agy" ? "Google 계정 로그인" : "로그인"}
              </Button>}
              {tool === "agy" && <Button size="sm" variant="secondary" onClick={() => setForm("apikey")}>Gemini API 키</Button>}
              {tool === "claude" && <>
                <Button size="sm" variant="secondary" onClick={() => setForm("token")}>토큰 붙여넣기</Button>
                <Button size="sm" variant="secondary" onClick={() => setForm("apikey")}>API 키</Button>
              </>}
            </>
          )}
        </div>
      )}
      {tool === "agy" && cliOk && <p className="text-xs text-text-muted">Google 계정에서 로그아웃하려면 실행 환경의 터미널에서 agy를 열고 /logout을 입력하세요.</p>}
      {tool === "agy" && t.method === "api_key" && !t.saved &&
        <p className="text-xs text-text-muted">실행 환경의 GEMINI_API_KEY를 쓰고 있어요. 연결을 해제하려면 agy 설정에서 modelProvider를 지우세요.</p>}
      {tool === "claude" && cliOk && !t.saved && <p className="text-xs text-text-muted">CLI 로그인을 그대로 써요. tp는 따로 토큰을 넣지 않아요.</p>}
      {tool === "claude" && t.saved && cliOk && <p className="text-xs text-text-muted">CLI 로그인이 있어서 tp에 저장한 {METHOD[t.saved]}은 쓰지 않아요.</p>}
      {codex?.code && (
        <div className="rounded-lg border border-border bg-grey-50 p-4">
          <p className="text-sm text-text-main">주소를 열어 이 코드를 입력하세요. 입력하면 자동으로 로그인돼요.</p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <span className="font-mono text-3xl font-bold tracking-widest text-text-main">{codex.code}</span>
            <Button size="sm" variant="secondary" onClick={() => navigator.clipboard.writeText(codex.code ?? "").then(() => setCopied(true), () => setCopied(false))}>
              {copied ? "복사됨" : "복사"}
            </Button>
            <a href={codex.url} target="_blank" rel="noreferrer" className="text-sm font-semibold text-primary-700 underline">열기</a>
            <Button size="sm" variant="secondary" onClick={() => void cancel()} loading={busy === "cancel"}>취소</Button>
          </div>
          <p className="mt-2 text-xs text-text-muted" role="status">기다리는 중… 코드는 {Math.round((codex.expires_in ?? 900) / 60)}분 동안 쓸 수 있어요.</p>
        </div>
      )}
      {oauthUrl && (
        <div className="space-y-3 rounded-lg border border-border bg-grey-50 p-4 text-sm text-text-main">
          <p><a href={oauthUrl} target="_blank" rel="noreferrer" className="font-semibold text-primary-700 underline">
            {tool === "agy" ? "Google 로그인 열기" : "Claude 로그인 열기"}
          </a></p>
          <p>{tool === "agy" ? "Google에서 받은 인증 코드를 붙여넣으세요. CLI가 로그인을 저장해요."
            : "Claude 구독 계정으로 로그인하고 코드를 붙여넣으세요. 1년짜리 토큰을 tp에 저장해요."}</p>
          {secretForm(tool === "agy" ? "Google 로그인 코드" : "Claude 로그인 코드", "코드 붙여넣기", "login/code", "code")}
        </div>
      )}
      {form === "token" && (
        <div className="space-y-2 text-sm text-text-main">
          <p className="text-xs text-text-muted">터미널에서 <code className="font-mono text-text-main">claude setup-token</code>을 실행하고 출력된 토큰(sk-ant-oat…)을 붙여넣으세요.</p>
          {secretForm("Claude 토큰", "sk-ant-oat…", "token", "token")}
        </div>
      )}
      {form === "apikey" && (
        <div className="space-y-2 text-sm text-text-main">
          <p className="text-xs text-text-muted">{tool === "agy"
            ? "결제가 활성화된 Google AI Studio 프로젝트의 키를 사용하세요. 실행 비용은 그 프로젝트에 청구돼요."
            : "Claude 구독이 없으면 Anthropic API 키(ANTHROPIC_API_KEY)를 써요. Anthropic에 확인한 뒤에만 저장해요."}</p>
          {secretForm(tool === "agy" ? "Gemini API 키" : "Anthropic API 키",
            tool === "agy" ? "키 붙여넣기" : "sk-ant-…", "apikey", "key")}
        </div>
      )}
      {err && <p role="alert" className={errBox}>{err}</p>}
    </div>
  );
}

/** Shared by 에이전트 settings and 시작하기. */
export function Accounts({ a, error, onRefresh, children }: { a: AgentAccounts | null; error: string; onRefresh: () => void; children?: React.ReactNode }) {
  return (
    <Section title="활성 계정" desc="이 컴퓨터의 Claude, Codex, Antigravity를 확인하고 로그인해요. 쓰는 것 하나만 로그인해도 돼요. 로그인하지 않은 도구는 아래 카드에서 고를 수 없어요."
      aside={<Button size="sm" variant="secondary" onClick={onRefresh}><RefreshCw className="h-4 w-4" /> 다시 확인</Button>}>
      {!a && error ? (
        <Block><p className="text-sm text-text-muted">확인하지 못했어요: {error}</p></Block>
      ) : !a ? (
        // the rows' frame while checking, so the cards below do not move when the answer arrives (TP-174)
        ["claude", "codex", "agy"].map((tool) => (
          <Row key={tool} group label={tool === "agy" ? "Antigravity (agy)" : tool} desc={"\u00a0"}>
            <div className="space-y-1 text-sm">
              <Badge>확인하는 중…</Badge>
              <p aria-hidden>&nbsp;</p>
            </div>
          </Row>
        ))
      ) : Object.entries(a.tools).map(([tool, t]) => (
        <Row key={tool} group label={tool === "agy" ? "Antigravity (agy)" : tool} desc={t.version ?? undefined}>
          <div className="space-y-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge status={t.logged_in ? "success" : t.installed ? "warning" : "danger"}>
                {t.logged_in ? "로그인됨" : t.installed ? "로그인 안 됨" : "설치 안 됨"}
              </Badge>
              {t.method && <span className="text-xs text-text-muted">{METHOD[t.method]}</span>}
            </div>
            {t.logged_in && t.account.length > 0 && <p className="text-text-main">{t.account.join(" · ")}</p>}
            {!t.installed && <p className="text-text-muted">PATH에서 {tool} 명령을 찾지 못했어요{a.docker && tool !== "agy" ? " (Docker 이미지를 다시 빌드해요)" : ""}.</p>}
            {t.neon_reason && <p className="text-text-muted">{t.neon_reason}</p>}
            {!t.installed && tool === "agy" && <a href="https://antigravity.google/docs/getting-started/"
              target="_blank" rel="noreferrer" className="font-semibold text-primary-700 underline">Antigravity CLI 설치 안내</a>}
            {t.installed && (a.admin ? <ToolLogin tool={tool} t={t} onChanged={onRefresh} />
              : <p className="text-xs text-text-muted">로그인은 이 컴퓨터(127.0.0.1){a.docker ? "나 TP_ADMINS의 사람" : ""}만 할 수 있어요.</p>)}
          </div>
        </Row>
      ))}
      {children}
    </Section>
  );
}

function GlobalEditor({ c, section, onReload }: { c: ConfigState; section: string; onReload: () => void }) {
  const orig = c.config.global;
  const [d, setD] = useState<GlobalConf>(() => clone(orig));
  const origRef = useRef(orig);
  useEffect(() => {
    setD((cur) => (same(cur, origRef.current) ? clone(orig) : cur));
    origRef.current = orig;
  }, [orig]);
  const set = (f: (d: GlobalConf) => void) => setD((cur) => { const n = clone(cur); f(n); return n; });
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
  const [accounts, setAccounts] = useState<AgentAccounts | null>(null);
  const [accErr, setAccErr] = useState("");
  const loadAccounts = useCallback(() => {
    setAccounts(null);
    getJson<AgentAccounts>("/api/agents/accounts").then((x) => { setAccounts(x); setAccErr(""); }, (e) => setAccErr(String(e)));
  }, []);
  useEffect(() => loadAccounts(), [loadAccounts]);
  // 이력 moved to 모니터링 > 설정 이력 (TP-168)
  useEffect(() => { if (section === "history") location.replace("#history"); }, [section]);
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
      <SaveBar dirty={!same(d, orig)} onReset={() => setD(clone(orig))} onSaved={onReload}
        run={(dry) => postConfig("/api/config", { scope: "global", value: changed(d, orig), dry_run: dry })} />
    </div>
  );
}

// ---------------------------------------------------------------- page

function useConfig() {
  const [c, setC] = useState<ConfigState | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => fetchConfig().then((x) => { setC(x); setError(""); }).catch((e) => setError(String(e))), []);
  useEffect(() => void load(), [load]);
  // a trial run updates its row when it finishes: poll while one runs
  const running = c && Object.values(c.check_runs).some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [running, load]);
  return { c, error, load };
}
const loading = (error: string) => <p className="text-sm text-text-muted">{error ? `설정을 불러오지 못했어요: ${error}` : "불러오는 중…"}</p>;

/** 설정 menu: global settings only; each repo's settings live on its 프로젝트 page (RepoSettings) */
export function Settings({ sub, onChanged }: { sub: string; onChanged: () => void }) {
  const { c, error, load } = useConfig();
  if (!c) return loading(error);
  return <GlobalEditor c={c} section={sub} onReload={() => { load(); onChanged(); }} />;
}

/** 프로젝트 > <repo> > 설정; `section` is the open section, `href` the address of a section ("" = the section list) */
export function RepoSettings({ name, s, section, href, onChanged }: { name: string; s: State; section: string; href: (section: string) => string; onChanged: () => void }) {
  const { c, error, load } = useConfig();
  if (!c) return loading(error);
  if (!c.config.repo[name]) return <p className="text-sm text-text-muted">설정에 없는 레포예요: {name}</p>;
  return <RepoEditor key={name} name={name} c={c} s={s} section={section} href={href} onReload={() => { load(); onChanged(); }} />;
}

// ---------------------------------------------------------------- 프로젝트 (TP-91)

export const SOURCE_LABEL: Record<ProjectSource, string> = { asana: "Asana 프로젝트", github: "GitHub 이슈", notion: "Notion 데이터베이스", none: "없음" };
export const ID_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;
export const slug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "");
const EMPTY: ProjectConf = { name: "", source: "none", asana_gid: null, repos: [] };

function ProjectEditor({ id, c, onReload, onSaved, onDeleted }: {
  id: string | null; c: ConfigState; onReload: () => void; onSaved?: (id: string) => void; onDeleted?: () => void;
}) {
  const orig = id ? c.config.project[id] : EMPTY;
  const [d, setD] = useState<ProjectConf>(() => clone(orig));
  const [newId, setNewId] = useState("");
  const [add, setAdd] = useState("");
  const [delErr, setDelErr] = useState("");
  const origRef = useRef(orig);
  useEffect(() => {
    setD((cur) => (same(cur, origRef.current) ? clone(orig) : cur));
    origRef.current = orig;
  }, [orig]);
  const set = (f: (d: ProjectConf) => void) => setD((cur) => { const n = clone(cur); f(n); return n; });
  const pid = id ?? (newId || slug(d.name));
  const idErr = id ? "" : !pid ? "id를 정해 주세요 (영문 소문자, 숫자, -)" : !ID_RE.test(pid) ? "영문 소문자, 숫자, - 로 40자 이내" : pid in c.config.project || pid === "unsorted" ? "이미 있는 id예요" : "";
  // Asana projects another tp project already links are not offered again
  const usedGids = new Set(Object.entries(c.config.project).filter(([k]) => k !== id).map(([, p]) => p.asana_gid).filter(Boolean));
  const free = Object.keys(c.config.repo).filter((n) => !d.repos.some((r) => r.repo === n));
  // before TP-91 a project's history was recorded under its Asana gid
  const hist = id ? c.history.filter((h) => h.scope === `project:${id}` || (!!orig.asana_gid && h.scope === `project:${orig.asana_gid}`)) : [];
  const remove = async () => {
    try { await postConfig("/api/config", { scope: `project.${id}`, value: null }); onDeleted?.(); onReload(); } catch (e) { setDelErr(e instanceof ConfigError ? e.errors.join("; ") : String(e)); }
  };
  return (
    <div className="space-y-4">
      <Section title={id ? "프로젝트 정보" : "새 프로젝트"}>
        <Row label="이름">
          <Input className={inputCls} value={d.name} onChange={(e) => set((x) => void (x.name = e.target.value))} />
        </Row>
        <Row label="id" desc={id ? "주소(#projects/<id>)에 쓰여요. 만든 뒤에는 바꿀 수 없어요" : idErr ? <span className="text-danger-700">{idErr}</span> : "주소(#projects/<id>)에 쓰여요"}>
          <Input className={cn(inputCls, "font-mono")} value={pid} disabled={!!id} onChange={(e) => setNewId(e.target.value)} />
        </Row>
        <Row label="티켓 출처">
          <Select className={selectCls} value={d.source} onChange={(e) => set((x) => {
            x.source = e.target.value as ProjectSource;
            if (x.source !== "asana") x.asana_gid = null;
            if (x.source !== "notion") delete x.notion;
          })}>
            {Object.entries(SOURCE_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </Select>
        </Row>
        {d.source === "asana" && (
          <Row label="Asana 프로젝트">
            <Select className={selectCls} value={d.asana_gid ?? ""} onChange={(e) => set((x) => void (x.asana_gid = e.target.value || null))}>
              <option value="">고르세요…</option>
              {Object.entries(c.projects).filter(([g]) => g === d.asana_gid || !usedGids.has(g)).map(([g, n]) => <option key={g} value={g}>{n} ({g})</option>)}
            </Select>
          </Row>
        )}
        {id && (
          <Row group label="프로젝트 삭제" desc="레포와 레포별 설정, 티켓은 그대로예요. 다른 프로젝트에 없는 레포는 미분류로 가요.">
            {delErr && <div className="mb-2"><Errors errors={[delErr]} /></div>}
            <ConfirmButton size="sm" variant="secondary" className="text-danger-700" confirmLabel="삭제" onConfirm={remove}
              message="이 프로젝트를 지울까요? 레포와 레포별 설정, 티켓은 그대로예요. 다른 프로젝트에 없는 레포는 미분류로 가요.">
              <Trash2 className="h-4 w-4" /> 프로젝트 삭제
            </ConfirmButton>
          </Row>
        )}
      </Section>
      {d.source === "notion" && <Section title="Notion 티켓 설정">
        <NotionSource projectId={pid} value={d.notion ?? emptyNotion} repos={d.repos.map((r) => r.repo)}
          workTag={c.config.global.asana_tag} onChange={(v) => set((x) => { x.notion = v; })} />
      </Section>}
      <Section title="레포" desc="한 레포를 여러 프로젝트에 넣을 수 있어요. GitHub에서 새 레포를 받아 등록하려면 위의 새 레포 등록을 눌러요.">
        <Block>
          {d.repos.length ? (
            <Table head={["레포", "role", "기본 레포", ""]}>
              {d.repos.map((r, i) => (
                <tr key={r.repo}>
                  <td className="font-mono text-xs">{r.repo}</td>
                  <td>
                    <Input aria-label="role" className={cn(inputCls, "w-28")} value={text(r.role)} onChange={(e) => set((x) => void (x.repos[i].role = e.target.value))} />
                  </td>
                  <td>
                    {/* one default per project: the chat runs in its checkout, the others are opened with it */}
                    <Check label="기본" checked={!!r.default} onChange={(v) => set((x) => x.repos.forEach((rr, j) => { if (j === i && v) rr.default = true; else delete rr.default; }))} />
                  </td>
                  <td className="text-right">
                    <Button size="sm" variant="secondary" aria-label={`${r.repo} 빼기`} onClick={() => set((x) => void x.repos.splice(i, 1))}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </td>
                </tr>
              ))}
            </Table>
          ) : (
            <p className="text-sm text-text-muted">아직 레포가 없어요.</p>
          )}
        </Block>
        <Row group label="레포 추가" desc="처음 넣는 레포가 기본 레포가 돼요">
          <div className="flex items-center gap-2">
            <Select className="h-9 min-w-0 flex-1 sm:max-w-72" value={add} onChange={(e) => setAdd(e.target.value)} aria-label="추가할 레포">
              <option value="">레포 고르기…</option>
              {free.map((n) => <option key={n} value={n}>{n}</option>)}
            </Select>
            <Button size="sm" variant="secondary" disabled={!add}
              onClick={() => { set((x) => void x.repos.push({ repo: add, role: "app", ...(x.repos.some((r) => r.default) ? {} : { default: true }) })); setAdd(""); }}>
              <Plus className="h-4 w-4" /> 추가
            </Button>
          </div>
        </Row>
      </Section>
      {id && (
        <Section title="이력">
          <Block><History rows={hist} /></Block>
        </Section>
      )}
      <SaveBar dirty={!id || !same(d, orig)} onReset={() => setD(clone(orig))} onSaved={() => { onReload(); onSaved?.(pid); }}
        run={(dry) => (idErr ? Promise.reject(new ConfigError(idErr, [`id: ${idErr}`])) : postConfig("/api/config", { scope: `project.${pid}`, value: d, dry_run: dry, create: !id }))} />
    </div>
  );
}

/** 프로젝트 > <id> > 설정 (project info); id null = the 새 프로젝트 form */
export function ProjectSettings({ id, onChanged, onSaved, onDeleted }: { id: string | null; onChanged: () => void; onSaved?: (id: string) => void; onDeleted?: () => void }) {
  const { c, error, load } = useConfig();
  if (!c) return loading(error);
  if (id && !c.config.project[id]) return <p className="text-sm text-text-muted">설정에 없는 프로젝트예요: {id}</p>;
  return <ProjectEditor key={id ?? ""} id={id} c={c} onReload={() => { load(); onChanged(); }} onSaved={onSaved} onDeleted={onDeleted} />;
}
