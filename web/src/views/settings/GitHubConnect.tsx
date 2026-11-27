import { useCallback, useEffect, useState } from "react";
import { Badge, Button } from "../../ui";
import { getJson, post, postJson } from "../../api";
import { ConfirmButton } from "../../components/ConfirmButton";
import type { GitHubDevice, GitHubStatus } from "../../types";
import { Block, Section } from "./Form";

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
