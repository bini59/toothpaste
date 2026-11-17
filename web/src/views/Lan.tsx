import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Card, Input } from "../ui";
import { getJson, postJson } from "../api";
import { ConfirmButton } from "../components/ConfirmButton";
import { ago } from "../lib";
import type { GitHubDevice, LanMe, LanStatus, TunnelStatus } from "../types";

/** 로컬망 공유 (TP-152): the login screen a teammate on the office network sees before anything else.
 * GitHub Device Flow (read:org); the server keeps the device code and checks the org membership. */
export function Login({ onDone }: { onDone: () => void }) {
  const [dev, setDev] = useState<(GitHubDevice & { id: string }) | null>(null);
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);
  const [pub, setPub] = useState<string | null>(null);
  useEffect(() => {
    getJson<LanMe>("/api/lan/me").then((m) => setPub(m.public_url ?? null), () => {});
  }, []);
  // TP-166: with TP_PUBLIC_URL a POST is only accepted from that address or 127.0.0.1
  const elsewhere = pub && location.origin !== pub && location.hostname !== "127.0.0.1";
  useEffect(() => {
    if (!dev) return;
    let stop = false;
    let t = 0;
    const tick = (s: number) => {
      t = window.setTimeout(async () => {
        try {
          const r = await postJson<{ pending?: boolean; interval?: number; login?: string }>("/api/lan/login/poll", { id: dev.id });
          if (stop) return;
          if (r.pending) return tick(r.interval ?? s);
          onDone();
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
  }, [dev, onDone]);
  const start = async () => {
    setErr("");
    setCopied(false);
    try {
      setDev(await postJson<GitHubDevice & { id: string }>("/api/lan/login", {}));
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const copy = () => navigator.clipboard?.writeText(dev?.user_code ?? "").then(() => setCopied(true), () => setCopied(false));
  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md p-6">
        <h1 className="text-xl font-bold text-text-main">toothpaste(치약) 로그인</h1>
        <p className="mt-2 text-sm text-text-muted">
          이 대시보드는 로그인이 필요해요. GitHub 계정으로 로그인하면 허용된 사람(조직 멤버나 관리자)인지 확인해요.
          로그인한 사람의 모든 행동은 GitHub 아이디로 기록돼요.
        </p>
        {elsewhere && (
          <p className="mt-3 text-sm text-text-main">
            이 대시보드의 주소는 <a href={pub} className="font-mono font-semibold text-primary-700 underline">{pub}</a>예요. 그 주소로 접속해서 로그인하세요.
          </p>
        )}
        {dev ? (
          <div className="mt-4 rounded-lg border border-border bg-grey-50 p-4">
            <p className="text-sm text-text-main">GitHub에서 이 코드를 입력하세요. 입력하면 자동으로 로그인돼요.</p>
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <span className="font-mono text-3xl font-bold tracking-widest text-text-main">{dev.user_code}</span>
              {navigator.clipboard && <Button size="sm" variant="secondary" onClick={copy}>{copied ? "복사됨" : "복사"}</Button>}
              <a href={dev.verification_uri} target="_blank" rel="noreferrer" className="text-sm font-semibold text-primary-700 underline">
                GitHub에서 입력
              </a>
            </div>
            <p className="mt-2 text-xs text-text-muted" role="status">기다리는 중… 코드는 {Math.round(dev.expires_in / 60)}분 동안 쓸 수 있어요.</p>
          </div>
        ) : null}
        <Button className="mt-4" onClick={start}>{dev ? "새 코드 받기" : "GitHub로 로그인"}</Button>
        {err && <p role="alert" className="mt-3 rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700">{err}</p>}
      </Card>
    </main>
  );
}

/** Who this browser is: a 127.0.0.1 visitor (no login) or a logged-in LAN user; null = the login screen. */
export function useMe() {
  const [me, setMe] = useState<LanMe | null | undefined>(undefined);
  const load = useCallback(() => {
    fetch("/api/lan/me").then(async (r) => {
      const j = r.ok ? ((await r.json()) as LanMe) : null;
      setMe(j && (j.local || j.login) ? j : null);
    }, () => setMe(null));
  }, []);
  useEffect(load, [load]);
  return [me, load] as const;
}

export async function logout() {
  await postJson("/api/lan/logout", {}).catch(() => {});
  location.reload();
}

/** 설정 → 고급 "로컬망 공유": on/off and org only from this Mac (127.0.0.1), the address, the logged-in sessions.
 * Docker mode: no sharing (every visit logs in anyway), only the sessions; admins force logouts. */
export function LanShare({ onSaved }: { onSaved: () => void }) {
  const [st, setSt] = useState<LanStatus | null>(null);
  const [org, setOrg] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    getJson<LanStatus>("/api/lan/status").then((x) => { setSt(x); setOrg(x.org); }, (e: Error) => setErr(e.message));
  }, []);
  useEffect(load, [load]);
  const save = async (enabled: boolean) => {
    setErr("");
    setMsg("");
    try {
      const r = await postJson<{ restart: boolean; note?: string }>("/api/lan/share", { enabled, org });
      setMsg(r.note || (r.restart ? "대시보드를 다시 시작하는 중이에요. 잠시 뒤 새로고침하세요." : "저장했어요."));
      load();
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const kick = async (id: number) => {
    try {
      await postJson("/api/lan/sessions/delete", { id });
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  if (!st) return err ? <p role="alert" className="text-sm text-danger-700">{err}</p> : <p className="text-sm text-text-muted">불러오는 중…</p>;
  return (
    <div className="space-y-3 border-b border-border px-4 py-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-text-main">로컬망 공유</span>
        <Badge status={st.enabled ? "warning" : "draft"}>{st.enabled ? "켜짐" : "꺼짐"}</Badge>
      </div>
      {st.docker ? (
        <p className="text-sm text-text-muted">
          Docker 모드에서는 쓰지 않아요. 모든 접속이 GitHub 로그인을 거치고, 로그인할 수 있는 사람은 docker/.env의 TP_ADMINS와 TP_ORG로 정해요.
          강제 로그아웃은 TP_ADMINS의 사람만 할 수 있어요.
        </p>
      ) : (<>
      <p className="text-sm text-text-muted">
        켜면 같은 사내망의 팀원이 GitHub 로그인(조직 멤버만)으로 이 대시보드의 모든 기능을 써요. 채팅 에이전트로 이 컴퓨터에서 내 계정으로
        명령을 실행할 수도 있어요. 모든 행동은 GitHub 아이디로 로그에 남아요. 켜고 끄는 것은 이 맥(127.0.0.1)에서만 할 수 있어요.
      </p>
      <label className="flex flex-wrap items-center gap-2 text-sm text-text-main">
        허용할 GitHub 조직
        <Input className="h-9 w-56" value={org} placeholder="your-org" disabled={!st.local} onChange={(e) => setOrg(e.target.value)} />
      </label>
      <p className="text-sm text-text-main">
        접속 주소: {st.address ? <span className="font-mono">{st.address}</span> : <span className="text-text-muted">사설 IP를 찾지 못했어요</span>}
        {!st.enabled && <span className="text-text-muted"> (켜야 열려요)</span>}
      </p>
      {st.local && (
        <div className="flex flex-wrap gap-2">
          {st.enabled ? (
            <>
              <ConfirmButton size="sm" variant="secondary" confirmLabel="끄기" onConfirm={() => save(false)}
                message="끄면 대시보드가 다시 시작되고 이 컴퓨터에서만 열려요.">
                공유 끄기
              </ConfirmButton>
              {org !== st.org && org.trim() && <Button size="sm" onClick={() => save(true)}>조직 저장</Button>}
            </>
          ) : (
            <ConfirmButton size="sm" confirmLabel="켜기" disabled={!org.trim()} onConfirm={() => save(true)}
              message={`조직 ${org}의 멤버 누구나 로그인해서 채팅 에이전트로 이 컴퓨터에서 내 계정으로 명령을 실행할 수 있어요. 대시보드가 다시 시작돼요.`}>
              공유 켜기
            </ConfirmButton>
          )}
        </div>
      )}
      </>)}
      {msg && <p role="status" className="text-sm text-success-700">{msg}</p>}
      {err && <p role="alert" className="rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700">{err}</p>}
      <div>
        <p className="text-sm font-semibold text-text-main">로그인한 세션 {st.sessions.length}개</p>
        {st.sessions.length > 0 && (
          <ul className="mt-2 divide-y divide-border rounded-lg border border-border">
            {st.sessions.map((x) => (
              <li key={x.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm text-text-main">
                <span className="font-mono font-semibold">{x.login}</span>
                <span className="text-text-muted">{x.ip} · 로그인 {ago(x.created_at)} · 마지막 {ago(x.last_seen)}</span>
                {st.local && (
                  <Button size="sm" variant="secondary" className="ml-auto" onClick={() => kick(x.id)}>강제 로그아웃</Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** 설정 → 고급 "외부 공개" (TP-166, Docker mode, TP_ADMINS only; host mode: a pointer to Docker, TP-172): status only. The token and the
 * domain live in docker/.env and the Cloudflare dashboard, never in this screen. */
export function Tunnel() {
  const [st, setSt] = useState<TunnelStatus | null>(null);
  useEffect(() => {
    getJson<TunnelStatus>("/api/lan/tunnel").then(setSt, () => setSt(null));
  }, []);
  if (!st) return null;
  if (st.docker === false)
    return (
      <div className="space-y-2 border-b border-border px-4 py-4">
        <span className="text-sm font-semibold text-text-main">외부 공개</span>
        <p className="text-sm text-text-muted">
          외부 공개(Cloudflare Tunnel)는 Docker 모드에서만 쓸 수 있어요. 위의 '실행 방식'에서 Docker로 바꾸세요. 방법은{" "}
          <a href="#help" className="font-semibold text-primary hover:underline">도움말</a>의 start · 시작하기에 있는 "Docker 모드 외부 공개"를 보세요.
        </p>
      </div>
    );
  const on = st.running && st.connections > 0;
  return (
    <div className="space-y-2 border-b border-border px-4 py-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-text-main">외부 공개</span>
        <Badge status={on ? "warning" : "draft"}>{on ? `연결됨 (${st.connections})` : st.running ? "연결 안 됨" : "꺼짐"}</Badge>
      </div>
      <p className="text-sm text-text-main">
        공개 주소: {st.public_url ? <span className="font-mono">{st.public_url}</span> : <span className="text-text-muted">없음 (TP_PUBLIC_URL)</span>}
      </p>
      <p className="text-sm text-text-muted">
        Cloudflare Tunnel로 공개해요. 토큰(TUNNEL_TOKEN)과 TP_PUBLIC_URL은 docker/.env에, 도메인은 Cloudflare 대시보드에서 정하고
        <code className="mx-1 font-mono">docker compose --profile tunnel up -d</code>로 켜요.
        {!st.running && " 지금은 cloudflared가 떠 있지 않아요."}
        {st.running && !on && " cloudflared는 떠 있지만 Cloudflare에 연결되지 않았어요. 토큰을 확인하세요."}
      </p>
      <p className="text-sm text-text-muted">
        Cloudflare Access로 회사 이메일이나 IdP 조건을 걸어 GitHub 로그인 앞에 한 겹 더 잠그는 것을 강력히 권장해요. 로그인한 사람은 채팅
        에이전트로 이 컨테이너 안에서 명령을 실행할 수 있어요.
      </p>
    </div>
  );
}
