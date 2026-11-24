import { useEffect, useRef, useState } from "react";
import { postJson } from "../api";
import { Select } from "../components/Select";
import { Table } from "../components/Table";
import { STAGES } from "../lib";
import type { NotionFilters } from "../types";
import { Badge, Button } from "../ui";

type Choice = { id: string; name: string };
type Property = Choice & { type: string; options: Choice[] };
type Connection = { id: string; workspace_name: string; user_name: string };
type Accounts = { configured: boolean; admin: boolean; connections: Connection[] };
type Preview = { count: number; tickets: { gid: string; title: string; auto_start: boolean; stage: string }[] };
export const emptyNotion: NotionFilters = { connection_id: "", data_source_id: "", assignee: "me" };
const callback = "/notion/callback";
const connectionsChannel = "tp-notion-connections";
const popupFlag = "tp-notion-login-popup";
const settingsHref = "/#settings/notion";
const accountName = (c: Connection) => `${c.user_name} · ${c.workspace_name}`;
const message = (e: unknown) => e instanceof Error ? e.message : String(e);
const request = <T,>(path: string, body: unknown) => postJson<T>(`/api/notion/${path}`, body);

export function notionError(value: NotionFilters) {
  if (!value.connection_id) return "프로젝트에 사용할 Notion 계정이 필요해요";
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

function useNotionRefresh() {
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const reload = () => setRefresh((n) => n + 1);
    const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(connectionsChannel);
    channel?.addEventListener("message", reload);
    window.addEventListener("focus", reload);
    return () => { window.removeEventListener("focus", reload); channel?.close(); };
  }, []);
  return refresh;
}

export function NotionAccounts() {
  const refresh = useNotionRefresh();
  const accounts = useNotionList<Accounts>("accounts", {}, refresh);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const connect = async () => {
    setError(""); setBusy(true);
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    try {
      const r = await request<{ url: string }>("connect", {});
      if (tab) {
        tab.sessionStorage.setItem(popupFlag, "1");
        tab.location.replace(r.url);
      } else {
        location.assign(r.url);
      }
    } catch (e) {
      tab?.close(); setError(message(e));
    } finally { setBusy(false); }
  };
  if (accounts?.error) return <p role="alert" className="py-4 text-sm text-danger-700">{accounts.error}</p>;
  if (!accounts?.data) return <p className="py-4 text-sm text-text-muted">연결 계정을 불러오는 중…</p>;
  const { admin, configured, connections } = accounts.data;
  return <div className="space-y-3 py-4">
    {connections.length ? (
      <Table head={["연결된 계정", "워크스페이스"]}>
        {connections.map((c) => <tr key={c.id}><td>{c.user_name}</td><td>{c.workspace_name}</td></tr>)}
      </Table>
    ) : <p className="text-sm text-text-muted">연결된 Notion 계정이 없어요.</p>}
    {!configured && <p className="text-sm text-text-muted">
      운영자가 서버에 Notion OAuth 앱을 등록하면 로그인할 수 있어요. 등록 방법은 <code>tp guide ticket</code>에서 확인하세요.
      콜백 주소는 <code className="break-all">{location.origin}{callback}</code>예요.
    </p>}
    {configured && (admin ? (
      <Button size="sm" loading={busy} onClick={connect}>Notion으로 로그인</Button>
    ) : <p className="text-sm text-text-muted">계정을 추가하려면 대시보드 관리자에게 요청하세요.</p>)}
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
    const isPopup = sessionStorage.getItem(popupFlag) === "1";
    sessionStorage.removeItem(popupFlag);
    history.replaceState(null, "", callback);
    request("finish", { code: q.get("code"), state: q.get("state") })
      .then(() => {
        if (typeof BroadcastChannel !== "undefined") {
          const channel = new BroadcastChannel(connectionsChannel);
          channel.postMessage("connected");
          channel.close();
        }
        if (isPopup) window.close();
        if (!window.closed) location.replace(settingsHref);
      })
      .catch((e) => setStatus(message(e)));
  }, []);
  return <main className="mx-auto max-w-lg space-y-4 p-6">
    <h1 className="text-xl font-bold text-text-main">Notion 계정 연결</h1>
    <p role="status" className="text-sm text-text-main">{status}</p>
    <a href={settingsHref} className="text-sm text-primary hover:underline">전역 설정 → Notion으로 돌아가기</a>
  </main>;
}

// Until a new project picks its repos, only membership is known. A saved project may have an empty repo list.
export function NotionPreview({ projectId, value, repos }: { projectId: string; value: NotionFilters; repos?: string[] }) {
  const [preview, setPreview] = useState<Preview>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    setBusy(true); setError(""); setPreview(undefined);
    try { setPreview(await request<Preview>("preview", { project_id: projectId, notion: value, repos: repos ?? [] })); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  };
  return <div className="space-y-2 border-t border-border pt-3">
    <Button size="sm" variant="secondary" loading={busy} disabled={!!notionError(value)} onClick={load}>티켓 미리보기</Button>
    {error && <p role="alert" className="text-sm text-danger-700">{error}</p>}
    {preview && <div aria-live="polite">
      <p className="mb-2 text-sm font-semibold text-text-main">조건에 맞는 티켓 {preview.count}개{repos && <> · 자동 시작 {preview.tickets.filter((t) => t.auto_start).length}개</>}</p>
      {!repos && <p className="text-xs text-text-muted">레포를 고른 뒤 확인 단계에서 자동 시작 대상도 미리볼 수 있어요.</p>}
      <ul className="max-h-72 divide-y divide-border overflow-y-auto">
        {preview.tickets.map((t) => <li key={t.gid} className="flex items-center justify-between gap-3 py-2 text-sm">
          <span className="min-w-0 break-words">{t.title}</span>
          {repos && <Badge status={t.auto_start ? "success" : "draft"}>{t.auto_start ? "자동 시작" : t.stage === "other_project" ? "다른 프로젝트" : STAGES[t.stage]?.[0] ?? t.stage}</Badge>}
        </li>)}
      </ul>
    </div>}
  </div>;
}

export function NotionSource({ projectId, value, repos, workTag, onChange }: {
  projectId: string; value: NotionFilters; repos?: string[]; workTag: string; onChange: (v: NotionFilters) => void;
}) {
  const refresh = useNotionRefresh();
  const accounts = useNotionList<Accounts>("accounts", {}, refresh);
  const connections = accounts?.data?.connections;
  const single = connections?.length === 1 ? connections[0] : undefined;
  useEffect(() => {
    if (single && !value.connection_id) onChange({ ...value, connection_id: single.id });
  }, [single, value, onChange]);
  const selected = connections?.find((c) => c.id === value.connection_id);
  const connection = selected ? { connection_id: selected.id } : null;
  const sources = useNotionList<Choice[]>("sources", connection, refresh);
  const users = useNotionList<Choice[]>("people", value.data_source_id ? connection : null, refresh);
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
  if (accounts?.error) return <p role="alert" className="py-4 text-sm text-danger-700">{accounts.error}</p>;
  if (!connections) return <p className="py-4 text-sm text-text-muted">연결 계정을 불러오는 중…</p>;
  const settingsLink = <a href={settingsHref} target="_blank" rel="noreferrer" className="font-semibold text-primary hover:underline">전역 설정 → Notion</a>;
  if (!connections.length) return <p className="py-4 text-sm text-text-muted">
    연결된 계정이 없어요. {settingsLink}에서 계정을 연결한 뒤 돌아오세요.
  </p>;
  const chooseAccount = (id: string) => onChange({ ...emptyNotion, connection_id: id });
  if (single && value.connection_id && !selected) return <div className="space-y-2 py-4">
    <p className="text-sm text-text-muted">저장된 계정을 찾을 수 없어요. {settingsLink}에서 다시 연결하거나 사용할 계정을 바꾸세요.</p>
    <Button size="sm" variant="secondary" onClick={() => chooseAccount(single.id)}>{accountName(single)} 사용</Button>
  </div>;
  return <div className="space-y-4 py-4">
    {single ? <p className="text-sm text-text-muted">{accountName(single)}</p> : (
      <Picker label="연결 계정 · 워크스페이스" value={value.connection_id} placeholder="계정 고르기…"
        choices={connections.map((c) => ({ id: c.id, name: accountName(c) }))} onChange={chooseAccount} />
    )}
    {connection && <Picker label="공유한 데이터베이스" value={value.data_source_id} choices={sources?.data ?? []}
      placeholder={!sources ? "불러오는 중…" : sources.data?.length ? "데이터 소스 고르기…" : "공유한 데이터 소스가 없어요"}
      onChange={(id) => onChange({ ...emptyNotion, connection_id: value.connection_id, data_source_id: id })} />}
    {connection && value.data_source_id && <>
      <div className="grid gap-3 sm:grid-cols-2">
        <Picker label="담당자 열" value={value.people_property || ""} placeholder="사람 속성 고르기…" choices={props.filter((p) => p.type === "people")}
          disabled={value.assignee === "all" || !schema?.data} onChange={(id) => onChange({ ...value, people_property: id })} />
        <Picker label="담당자" value={value.assignee ?? "me"} choices={members} onChange={(id) => onChange({ ...value, assignee: id })} />
      </div>
      {optionRow("tag", "태그", ["multi_select", "select"])}
      {optionRow("status", "상태", ["status", "select"])}
      <p className="text-xs text-text-muted">선택한 조건을 모두 만족하는 페이지를 가져와요. 태그 열에 <strong>{workTag}</strong>가 있고 작업 레포가 정해진 티켓이 자동 시작 대상이에요.</p>
      <NotionPreview key={JSON.stringify([projectId, value, repos, workTag])} projectId={projectId} value={value} repos={repos} />
    </>}
    {[sources, users, schema].map((r, i) => r?.error && <p key={i} role="alert" className="text-sm text-danger-700">{r.error}</p>)}
  </div>;
}
