import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Input, cn } from "../../ui";
import { getJson, post, postConfig } from "../../api";
import { ConfirmButton } from "../../components/ConfirmButton";
import { Select } from "../../components/Select";
import type { AsanaStatus } from "../../types";
import { Block, Section, inputCls, selectCls } from "./Form";

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
