import { useEffect, useRef, useState } from "react";
import { postJson } from "../api";
import { Select } from "../components/Select";
import { Table } from "../components/Table";
import { STAGES } from "../lib";
import type { NotionFilters } from "../types";
import { Badge, Button, Input } from "../ui";

type Choice = { id: string; name: string };
type Property = Choice & { type: string; options: Choice[]; data_source_id?: string };
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
  if (!!value.contains_property !== !!value.contains_text?.trim()) return "문자열을 검사할 열과 포함할 문자열을 함께 입력하세요";
  if (value.project_data_source_id && !value.project_page_id) return "선택한 DB에서 프로젝트 행을 골라 주세요";
  if (!!value.milestone_data_source_id !== !!value.milestone_property) return "마일스톤 DB와 작업 DB의 마일스톤 관계 열을 함께 골라 주세요";
  if (value.milestone_data_source_id === value.data_source_id) {
    if (!value.milestone_kind_property || !value.milestone_kind_option) {
      return "작업 DB를 쓰려면 마일스톤 판별 열과 마일스톤 태그를 함께 골라 주세요";
    }
    if (value.milestone_kind_property === value.status_property &&
        (value.stage_statuses?.done || Object.values(value.stage_statuses ?? {}).includes(value.milestone_kind_option))) {
      return "단계별 상태를 쓰면 마일스톤 태그가 바뀌어요. 상태 열과 마일스톤 판별 열을 따로 골라 주세요";
    }
  }
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

function OptionalSettings({ title, children }: { title: string; children: React.ReactNode }) {
  return <details>
    <summary className="cursor-pointer rounded-md px-2 py-3 text-sm font-semibold text-text-main hover:bg-grey-50">
      {title}
    </summary>
    <div className="space-y-3 px-2 pb-4">{children}</div>
  </details>;
}

function rowChoices(rows: Choice[]) {
  const seen = new Set<string>(), repeated = new Set<string>();
  for (const { name } of rows) {
    if (seen.has(name)) repeated.add(name);
    seen.add(name);
  }
  return rows.map((r) => repeated.has(r.name) ? { ...r, name: `${r.name} · ${r.id}` } : r);
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
  const validation = notionError(value);
  const load = async () => {
    setBusy(true); setError(""); setPreview(undefined);
    try { setPreview(await request<Preview>("preview", { project_id: projectId, notion: value, repos: repos ?? [] })); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  };
  return <div className="space-y-2 border-t border-border pt-3">
    <Button size="sm" variant="secondary" loading={busy} disabled={!!validation} onClick={load}>티켓 미리보기</Button>
    {validation && <p className="text-xs text-text-muted">{validation}</p>}
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
  const rows = useNotionList<Choice[]>("rows", connection && value.project_data_source_id
    ? { ...connection, data_source_id: value.project_data_source_id } : null, refresh);
  const props = schema?.data ?? [];
  const relationsTo = (source?: string) => props.filter((p) => p.type === "relation" && p.data_source_id === source);
  const chooseProjectSource = (source: string) => {
    const relations = relationsTo(source).filter((p) => p.id !== value.parent_property);
    const project = relations.find((p) => p.name === "프로젝트" || p.name.toLowerCase() === "project")
      ?? (source !== value.data_source_id && relations.length === 1 ? relations[0] : undefined);
    onChange({ ...value, project_data_source_id: source || undefined, project_page_id: undefined, project_property: project?.id });
  };
  const members = [{ id: "me", name: "연결한 나" }, { id: "all", name: "전체" }, ...(users?.data ?? [])];
  const statusOptions = props.find((p) => p.id === value.status_property)?.options ?? [];
  const stagePicker = (stage: string, label = STAGES[stage][0]) => (
    <Picker key={stage} label={label} choices={statusOptions} value={value.stage_statuses?.[stage] || ""}
      placeholder="반영 안 함" disabled={!schema?.data} onChange={(id) => {
        const stages = { ...value.stage_statuses };
        if (id) stages[stage] = id;
        else delete stages[stage];
        onChange({ ...value, stage_statuses: stages });
      }} />
  );
  const optionRow = (role: "tag" | "status", label: string, types: string[]) => {
    const prop = `${role}_property` as const, option = `${role}_option` as const;
    return <div className="grid gap-3 sm:grid-cols-2">
      <Picker label={`${label} 열`} choices={props.filter((p) => types.includes(p.type))} value={value[prop] || ""}
        placeholder="사용 안 함" disabled={!schema?.data} onChange={(id) => onChange({
          ...value, [prop]: id, [option]: "", ...(role === "status" && { stage_statuses: {} }),
        })} />
      <Picker label={`가져올 ${label}`} choices={props.find((p) => p.id === value[prop])?.options ?? []} value={value[option] || ""}
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
      <fieldset className="space-y-3">
        <legend className="mb-3 text-sm font-semibold text-text-main">가져오기 조건</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          <Picker label="담당자 열" value={value.people_property || ""} placeholder="사람 속성 고르기…" choices={props.filter((p) => p.type === "people")}
            disabled={!schema?.data} onChange={(id) => onChange({ ...value, people_property: id })} />
          <Picker label="가져올 담당자" value={value.assignee ?? "me"} choices={members} onChange={(id) => onChange({ ...value, assignee: id })} />
        </div>
        {optionRow("tag", "태그", ["multi_select", "select"])}
        {optionRow("status", "상태", ["status", "select"])}
        <p className="text-xs text-text-muted">해당 프로젝트에서 담당자·태그·상태 조건을 모두 만족하는 페이지를 가져와요. 태그 열에 <strong>{workTag}</strong>가 있고 작업 레포가 정해진 티켓이 자동 시작 대상이에요.</p>
      </fieldset>
      <NotionPreview key={JSON.stringify([projectId, value, repos, workTag])} projectId={projectId} value={value} repos={repos} />
      <div className="divide-y divide-border border-y border-border">
        <OptionalSettings title="프로젝트 소속과 하위 작업">
          <Picker label="상위 항목 관계 열" value={value.parent_property ?? ""} placeholder="사용 안 함"
            choices={relationsTo(value.data_source_id).filter((p) => p.id !== value.project_property)} disabled={!schema?.data}
            onChange={(id) => onChange({ ...value, parent_property: id || undefined })} />
          <div className="grid gap-3 sm:grid-cols-2">
            <Picker label="프로젝트 행이 있는 DB" value={value.project_data_source_id ?? ""} placeholder="사용 안 함"
              choices={sources?.data ?? []} disabled={!schema?.data} onChange={chooseProjectSource} />
            {value.project_data_source_id && <Picker label="프로젝트 행" value={value.project_page_id ?? ""}
              placeholder={rows?.data ? "행 고르기…" : "불러오는 중…"} choices={rowChoices(rows?.data ?? [])} disabled={!rows?.data}
              onChange={(id) => onChange({ ...value, project_page_id: id || undefined })} />}
          </div>
          {value.project_data_source_id && <Picker label="작업 DB의 프로젝트 관계 열" value={value.project_property ?? ""}
            choices={relationsTo(value.project_data_source_id).filter((p) => p.id !== value.parent_property)}
            placeholder="사용 안 함"
            disabled={!schema?.data} onChange={(id) => onChange({ ...value, project_property: id || undefined })} />}
          <div className="grid gap-3 sm:grid-cols-2">
            <Picker label="문자열을 검사할 열" value={value.contains_property ?? ""} placeholder="사용 안 함"
              choices={props.filter((p) => ["title", "rich_text", "formula", "unique_id"].includes(p.type))}
              disabled={!schema?.data} onChange={(id) => onChange({ ...value, contains_property: id || undefined,
                contains_text: id ? value.contains_text : undefined })} />
            <label className="block min-w-0 space-y-1">
              <span className="block text-sm font-semibold text-text-main">포함할 문자열</span>
              <Input value={value.contains_text ?? ""} placeholder="예: TP-" disabled={!value.contains_property || !schema?.data}
                onChange={(e) => onChange({ ...value, contains_text: e.target.value })} />
            </label>
          </div>
          <p className="text-xs text-text-muted">직접 연결된 프로젝트 행, 상위 작업의 프로젝트, 문자열 일치 순으로 소속을 정해요.</p>
        </OptionalSettings>
        <OptionalSettings title="마일스톤">
          <div className="grid gap-3 sm:grid-cols-2">
            <Picker label="마일스톤 DB" value={value.milestone_data_source_id ?? ""} placeholder="사용 안 함"
              choices={sources?.data ?? []} disabled={!sources?.data || !schema?.data}
              onChange={(id) => {
                const columns = id ? relationsTo(id) : [];
                onChange({ ...value, milestone_data_source_id: id || undefined,
                  milestone_property: columns.length === 1 ? columns[0].id : undefined,
                  milestone_kind_property: undefined, milestone_kind_option: undefined });
              }} />
            {value.milestone_data_source_id && <Picker label="작업 DB의 마일스톤 관계 열" value={value.milestone_property ?? ""}
              choices={relationsTo(value.milestone_data_source_id)} placeholder="관계 열 고르기…" disabled={!schema?.data}
              onChange={(id) => onChange({ ...value, milestone_property: id || undefined })} />}
          </div>
          {value.milestone_data_source_id && schema?.data && !relationsTo(value.milestone_data_source_id).length &&
            <p className="text-xs text-text-muted">작업 DB에 선택한 마일스톤 DB를 가리키는 관계 열을 추가하세요.</p>}
          {value.milestone_data_source_id === value.data_source_id && <div className="grid gap-3 sm:grid-cols-2">
            <Picker label="마일스톤 판별 열" value={value.milestone_kind_property ?? ""} placeholder="판별 열 고르기…"
              choices={props.filter((p) => p.type === "select" || p.type === "multi_select")} disabled={!schema?.data}
              onChange={(id) => onChange({ ...value, milestone_kind_property: id || undefined, milestone_kind_option: undefined })} />
            <Picker label="마일스톤 태그" value={value.milestone_kind_option ?? ""} placeholder="태그 고르기…"
              choices={props.find((p) => p.id === value.milestone_kind_property)?.options ?? []}
              disabled={!schema?.data || !value.milestone_kind_property}
              onChange={(id) => onChange({ ...value, milestone_kind_option: id || undefined })} />
          </div>}
        </OptionalSettings>
        <OptionalSettings title="마감일">
          <Picker label="마감일 열" value={value.date_property || ""} placeholder="날짜 열이 하나면 자동 선택"
            choices={props.filter((p) => p.type === "date")} disabled={!schema?.data}
            onChange={(id) => onChange({ ...value, date_property: id })} />
        </OptionalSettings>
        <OptionalSettings title="작업 단계별 Notion 상태">
          {value.status_property ? <>
            <div className="grid gap-3 sm:grid-cols-2">
              {stagePicker("in_progress", "작업 시작 · 작업 중")}
              {stagePicker("done", "운영 배포 확인")}
            </div>
            <details>
              <summary className="cursor-pointer text-sm text-primary">다른 작업 단계</summary>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                {["dispatched", "blocked", "agent_done", "pr_open", "rework", "conflict", "reviewed", "approving"].map((s) => stagePicker(s))}
                {stagePicker("staged", "스테이징 반영")}
                {stagePicker("merged", "머지 후 배포 대기")}
                {stagePicker("deploying")}
                {stagePicker("closed", "PR 닫힘")}
              </div>
            </details>
          </> : <p className="text-xs text-text-muted">가져오기 조건에서 상태 열을 고르세요. 가져올 상태는 전체로 둘 수 있어요.</p>}
        </OptionalSettings>
      </div>
    </>}
    {[sources, users, schema, rows].map((r, i) => r?.error && <p key={i} role="alert" className="text-sm text-danger-700">{r.error}</p>)}
  </div>;
}
