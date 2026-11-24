import { useEffect, useRef, useState } from "react";
import { postJson } from "../api";
import { Select } from "../components/Select";
import { STAGES } from "../lib";
import type { NotionFilters } from "../types";
import { Badge, Button, Input } from "../ui";

type Choice = { id: string; name: string };
type Property = Choice & { type: string; options: Choice[] };
type Accounts = { configured: boolean; admin: boolean; connections: { id: string; workspace_name: string; user_name: string }[] };
type Preview = { count: number; tickets: { gid: string; title: string; auto_start: boolean; stage: string }[] };
export const emptyNotion: NotionFilters = { connection_id: "", data_source_id: "", assignee: "me" };
const callback = "/notion/callback";
const connectionsChannel = "tp-notion-connections";
const message = (e: unknown) => e instanceof Error ? e.message : String(e);
const request = <T,>(path: string, body: unknown) => postJson<T>(`/api/notion/${path}`, body);

export function notionError(value: NotionFilters) {
  if (!value.connection_id) return "Notion 계정을 골라 주세요";
  if (!value.data_source_id) return "데이터베이스를 골라 주세요";
  if (value.assignee !== "all" && !value.people_property) return "담당자 열을 고르거나 담당자를 전체로 바꿔 주세요";
  return "";
}

// A response belongs to the connection and data source that requested it, including after a rapid switch.
function useNotionList<T,>(path: string, args: object | null, refresh: number) {
  const body = JSON.stringify(args);
  const [result, setResult] = useState<{ body: string; data?: T; error?: string }>();
  useEffect(() => {
    if (body === "null") return;
    let current = true;
    request<T>(path, JSON.parse(body)).then((data) => {
      if (current) setResult({ body, data });
    }).catch((e) => { if (current) setResult({ body, error: message(e) }); });
    return () => { current = false; };
  }, [path, body, refresh]);
  return result?.body === body ? result : undefined;
}

function Picker({ label, value, choices, placeholder, disabled, onChange }: {
  label: string; value: string; choices: Choice[]; placeholder?: string; disabled?: boolean; onChange: (id: string) => void;
}) {
  return <label className="block min-w-0 space-y-1">
    <span className="block text-sm font-semibold text-text-main">{label}</span>
    <Select className="w-full" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      {placeholder && <option value="">{placeholder}</option>}
      {value && !choices.some((v) => v.id === value) && <option value={value}>현재 선택 · {value}</option>}
      {choices.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
    </Select>
  </label>;
}

function Connect({ configured, reload }: { configured: boolean; reload: () => void }) {
  const [client, setClient] = useState("");
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [url, setUrl] = useState("");
  const connect = async () => {
    setError(""); setBusy(true); setUrl("");
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    try {
      const r = await request<{ url: string }>("connect", {});
      if (tab) tab.location.replace(r.url);
      else setUrl(r.url);
    } catch (e) {
      tab?.close(); setError(message(e));
    } finally { setBusy(false); }
  };
  const save = async () => {
    setError(""); setBusy(true);
    try {
      await request("app", { client_id: client, client_secret: secret });
      setSecret(""); setClient(""); reload();
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  };
  return <div className="space-y-2">
    {configured && <Button variant="secondary" size="sm" disabled={busy} onClick={connect}>Notion 계정 연결</Button>}
    {url && <a href={url} target="_blank" rel="noreferrer" className="block text-sm text-primary hover:underline">Notion에서 연결 계속하기</a>}
    <details open={configured ? undefined : true} className="text-sm">
      <summary className="cursor-pointer text-text-muted">OAuth 앱 설정</summary>
      <div className="mt-2 space-y-2">
        <p className="text-xs text-text-muted">Notion OAuth 앱에 리디렉션 주소 <code className="break-all">{location.origin}{callback}</code>를 등록하고 콘텐츠 및 사용자 읽기 권한을 켜세요.</p>
        <label className="block">클라이언트 ID<Input value={client} onChange={(e) => setClient(e.target.value)} autoComplete="off" /></label>
        <label className="block">클라이언트 시크릿<Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="new-password" /></label>
        <Button size="sm" variant="secondary" disabled={busy || !client.trim() || !secret.trim()} onClick={save}>OAuth 앱 저장</Button>
      </div>
    </details>
    {error && <p role="alert" className="text-sm text-danger-700">{error}</p>}
  </div>;
}

export function NotionCallback() {
  const [status, setStatus] = useState("Notion 계정을 연결하는 중…");
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const q = new URLSearchParams(location.search);
    history.replaceState(null, "", callback);
    request("finish", { code: q.get("code"), state: q.get("state") })
      .then(() => {
        const channel = new BroadcastChannel(connectionsChannel);
        channel.postMessage("connected");
        channel.close();
        setStatus("계정을 연결했어요. 프로젝트 설정으로 돌아가 연결 계정을 고르세요.");
      })
      .catch((e) => setStatus(message(e)));
  }, []);
  return <main className="mx-auto max-w-lg space-y-4 p-6">
    <h1 className="text-xl font-bold text-text-main">Notion 계정 연결</h1>
    <p role="status" className="text-sm text-text-main">{status}</p>
    <Button size="sm" variant="secondary" onClick={() => window.close()}>창 닫기</Button>
  </main>;
}

function MatchPreview({ projectId, value, repos }: { projectId: string; value: NotionFilters; repos: string[] }) {
  const [preview, setPreview] = useState<Preview>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    setBusy(true); setError(""); setPreview(undefined);
    try { setPreview(await request<Preview>("preview", { project_id: projectId, notion: value, repos })); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  };
  return <div className="space-y-2 border-t border-border pt-3">
    <Button size="sm" variant="secondary" loading={busy} disabled={!!notionError(value)} onClick={load}>티켓 미리보기</Button>
    {error && <p role="alert" className="text-sm text-danger-700">{error}</p>}
    {preview && <div aria-live="polite">
      <p className="mb-2 text-sm font-semibold text-text-main">조건에 맞는 티켓 {preview.count}개 · 자동 시작 {preview.tickets.filter((t) => t.auto_start).length}개</p>
      <ul className="max-h-72 divide-y divide-border overflow-y-auto">
        {preview.tickets.map((t) => <li key={t.gid} className="flex items-center justify-between gap-3 py-2 text-sm">
          <span className="min-w-0 break-words">{t.title}</span>
          <Badge status={t.auto_start ? "success" : "draft"}>{t.auto_start ? "자동 시작" : t.stage === "other_project" ? "다른 프로젝트" : STAGES[t.stage]?.[0] ?? t.stage}</Badge>
        </li>)}
      </ul>
    </div>}
  </div>;
}

export function NotionSource({ projectId, value, repos, workTag, onChange }: {
  projectId: string; value: NotionFilters; repos: string[]; workTag: string; onChange: (v: NotionFilters) => void;
}) {
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const onFocus = () => setRefresh((n) => n + 1);
    const channel = new BroadcastChannel(connectionsChannel);
    channel.onmessage = onFocus;
    window.addEventListener("focus", onFocus);
    return () => { window.removeEventListener("focus", onFocus); channel.close(); };
  }, []);
  const accounts = useNotionList<Accounts>("accounts", {}, refresh);
  const connection = value.connection_id ? { connection_id: value.connection_id } : null;
  const sources = useNotionList<Choice[]>("sources", connection, refresh);
  const users = useNotionList<Choice[]>("people", connection, refresh);
  const schema = useNotionList<Property[]>("properties", connection && value.data_source_id
    ? { ...connection, data_source_id: value.data_source_id } : null, refresh);
  const props = schema?.data ?? [];
  const members = [{ id: "me", name: "연결한 나" }, { id: "all", name: "전체" }, ...(users?.data ?? [])];
  const optionRow = (role: "tag" | "status", label: string, types: string[]) => {
    const prop = `${role}_property` as const, option = `${role}_option` as const;
    return <div className="grid gap-3 sm:grid-cols-2">
      <Picker label={`${label} 열`} choices={props.filter((p) => types.includes(p.type))} value={value[prop] || ""}
        placeholder="사용 안 함" disabled={!schema?.data} onChange={(id) => onChange({ ...value, [prop]: id, [option]: "" })} />
      <Picker label={`${label} 값`} choices={props.find((p) => p.id === value[prop])?.options ?? []} value={value[option] || ""}
        placeholder="전체" disabled={!value[prop] || !schema?.data} onChange={(id) => onChange({ ...value, [option]: id })} />
    </div>;
  };
  return <div className="space-y-4 py-4">
    <Picker label="연결 계정 · 워크스페이스" value={value.connection_id} placeholder={accounts ? "계정 고르기…" : "불러오는 중…"}
      choices={(accounts?.data?.connections ?? []).map((c) => ({ id: c.id, name: `${c.user_name} · ${c.workspace_name}` }))}
      onChange={(id) => onChange({ ...emptyNotion, connection_id: id })} />
    {accounts?.data?.admin && <Connect configured={accounts.data.configured} reload={() => setRefresh((n) => n + 1)} />}
    {connection && <Picker label="공유한 데이터베이스" value={value.data_source_id} choices={sources?.data ?? []}
      placeholder={!sources ? "불러오는 중…" : sources.data?.length ? "데이터 소스 고르기…" : "공유한 데이터 소스가 없어요"}
      onChange={(id) => onChange({ ...emptyNotion, connection_id: value.connection_id, data_source_id: id })} />}
    {value.data_source_id && <>
      <div className="grid gap-3 sm:grid-cols-2">
        <Picker label="담당자 열" value={value.people_property || ""} placeholder="사람 속성 고르기…" choices={props.filter((p) => p.type === "people")}
          disabled={value.assignee === "all" || !schema?.data} onChange={(id) => onChange({ ...value, people_property: id })} />
        <Picker label="담당자" value={value.assignee ?? "me"} choices={members} onChange={(id) => onChange({ ...value, assignee: id })} />
      </div>
      {optionRow("tag", "태그", ["multi_select", "select"])}
      {optionRow("status", "상태", ["status", "select"])}
      <p className="text-xs text-text-muted">선택한 조건을 모두 만족하는 페이지를 가져와요. 태그 열에 <strong>{workTag}</strong>가 있고 작업 레포가 정해진 티켓이 자동 시작 대상이에요.</p>
      <MatchPreview key={JSON.stringify([projectId, value, repos, workTag])} projectId={projectId} value={value} repos={repos} />
    </>}
    {[accounts, sources, users, schema].map((r, i) => r?.error && <p key={i} role="alert" className="text-sm text-danger-700">{r.error}</p>)}
  </div>;
}
