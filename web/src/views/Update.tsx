import { useCallback, useEffect, useState } from "react";
import { Button, Card } from "../ui";
import { getJson, postJson } from "../api";
import { ago } from "../lib";
import type { UpdateInfo } from "../types";

// tp update's last update.log line (update.py main): "<time> 완료: old -> new" | "<time> 최신이에요 (v)" | "<time> 실패: why"
const END = /^\S+ (완료|최신이에요|실패)/;
const POLL_MS = 2000;
const GIVE_UP_MS = 15 * 60 * 1000;

/** 업데이트 page (TP-140), also 설정 → 업데이트 (TP-153): current -> latest release, its notes, 지금 확인 (fetch the tags
 * now instead of waiting for tp tick's 6-hourly check) and the button that runs `tp update` detached. The dashboard
 * restarts mid-way, so it polls /api/update (failures while it is down are expected) until update.log ends.
 * onDone reloads the app state (the sidebar's 업데이트 item) after a check or an update. */
export function Update({ onDone }: { onDone: () => void }) {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [err, setErr] = useState("");
  const [since, setSince] = useState<number | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [result, setResult] = useState<"" | "ok" | "fail">("");
  const [checking, setChecking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [job, setJob] = useState<string | null>(null);
  const dockerUpdate = info?.docker_update;
  const dockerRunning = !!job || ["queued", "pulling", "switching"].includes(dockerUpdate?.state ?? "");

  const load = useCallback(() => getJson<UpdateInfo>("/api/update").then((d) => { setInfo(d); setErr(""); }).catch((e) => setErr(String(e))), []);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (since == null || result) return;
    const t0 = Date.now();
    const id = setInterval(() => {
      getJson<UpdateInfo>(`/api/update?since=${since}`).then((d) => {
        const log = d.log ?? [];
        setLines(log);
        const end = log.map((l) => END.exec(l)?.[1]).filter(Boolean).at(-1);
        if (end) { setInfo(d); setResult(end === "실패" ? "fail" : "ok"); onDone(); }
      }).catch(() => {}); // the dashboard is restarting
      if (Date.now() - t0 > GIVE_UP_MS) setResult("fail");
    }, POLL_MS);
    return () => clearInterval(id);
  }, [since, result, onDone]);

  useEffect(() => {
    if (!dockerRunning) return;
    const id = setInterval(() => {
      getJson<UpdateInfo>("/api/update").then((d) => {
        if (job && d.docker_update?.id !== job) return;
        setInfo(d);
        const state = d.docker_update?.state;
        if (state === "done" || state === "failed") {
          setJob(null);
          onDone();
        }
      }).catch(() => {}); // the dashboard is restarting
    }, POLL_MS);
    const timeout = setTimeout(() => {
      setJob(null);
      setInfo((prev) => prev && { ...prev, docker_update: {} });
      setErr("Docker 업데이트 상태를 확인하지 못했어요. 잠시 뒤 페이지를 새로고침해 주세요.");
    }, 45 * 60 * 1000);
    return () => { clearInterval(id); clearTimeout(timeout); };
  }, [dockerRunning, job, onDone]);

  const start = () => {
    setErr(""); setLines([]); setResult(""); setStarting(true);
    postJson<{ since?: number; job?: { id: string; tag: string } }>("/api/update", {}).then((r) => {
      if (r.job) {
        const started = r.job;
        setJob(started.id);
        setInfo((prev) => prev && { ...prev, docker_update: { ...started, state: "queued", message: "업데이트를 요청했어요." } });
      } else if (r.since !== undefined) {
        setSince(r.since);
      }
    }).catch((e) => setErr((e as Error).message)).finally(() => setStarting(false));
  };

  const check = () => {
    setErr(""); setSince(null); setResult(""); setLines([]); setChecking(true);
    postJson<UpdateInfo>("/api/update/check", {}).then((d) => { setInfo(d); onDone(); })
      .catch((e) => setErr(`확인하지 못했어요: ${(e as Error).message}`)).finally(() => setChecking(false));
  };

  // the card's frame while loading (heading, version line, check row), so what sits below does not jump (TP-174)
  if (!info) return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card className="p-5">
        <h2 className="text-base font-bold text-text-main">{err ? `불러오지 못했어요: ${err}` : "불러오는 중…"}</h2>
        <p className="mt-1 text-sm text-text-muted">&nbsp;</p>
        <div className="mt-1 h-8" />
      </Card>
    </div>
  );
  const running = starting || dockerRunning || (since != null && !result);
  const hasNew = info.available;
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card className="p-5">
        <h2 className="text-base font-bold text-text-main">
          {result === "ok" ? "업데이트했어요" : hasNew ? `${info.current} → ${info.latest}` : "최신 버전이에요"}
        </h2>
        <p className="mt-1 text-sm text-text-muted">지금 버전 {info.current} · 최신 릴리즈 {info.latest ?? "없음"}</p>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-text-muted">
          <span title={info.checked_at ? new Date(info.checked_at).toLocaleString("ko-KR") : undefined}>
            마지막 확인 {info.checked_at ? ago(info.checked_at) : "아직 없음"} · 6시간마다 자동 확인
          </span>
          {!running && result !== "ok" && (
            <Button size="sm" variant="secondary" onClick={check} loading={checking}>지금 확인</Button>
          )}
        </div>
        {result === "ok" && <p className="mt-3 text-sm text-text-main">새 화면을 보려면 페이지를 새로고침하세요.</p>}
        {result === "fail" && (
          <p className="mt-3 text-sm text-danger-700">업데이트하지 못했어요: {lines.at(-1) ?? "update.log에 기록이 없어요"}</p>
        )}
        {err && <p className="mt-3 text-sm text-danger-700">{err}</p>}
        {dockerUpdate?.message && (
          <p role="status" className={dockerUpdate.state === "failed" ? "mt-3 text-sm text-danger-700" : "mt-3 text-sm text-text-main"}>
            {dockerUpdate.message}
          </p>
        )}
        {hasNew && !result && (
          info.command
            ? <div className="mt-3 text-sm text-text-main">
                호스트에서 Docker 업데이트 관리자를 한 번 설치하면 이 화면에서 업데이트할 수 있어요.
                <pre className="mt-2 overflow-x-auto rounded-lg border border-border bg-grey-50 p-3 font-mono text-xs">{info.command}</pre>
              </div>
            : info.blocked && !dockerRunning
            ? <p className="mt-3 text-sm text-warning-700">업데이트할 수 없어요: {info.blocked}</p>
            : <Button className="mt-3" onClick={start} loading={running}>업데이트</Button>
        )}
        {(result === "ok" || dockerUpdate?.state === "done") && <Button className="mt-3" variant="secondary" onClick={() => location.reload()}>새로고침</Button>}
      </Card>
      {lines.length > 0 && (
        <Card className="p-5">
          <h3 className="mb-2 text-sm font-bold text-text-main">진행 기록 (update.log)</h3>
          <pre className="overflow-x-auto whitespace-pre-wrap text-xs text-text-muted">{lines.join("\n")}</pre>
        </Card>
      )}
      {hasNew && info.notes.length > 0 && !result && (
        <Card className="p-5">
          <h3 className="mb-2 text-sm font-bold text-text-main">릴리즈 노트</h3>
          {info.notes.map((n) => (
            <div key={n.tag} className="mb-3 last:mb-0">
              <p className="text-sm font-semibold text-text-main">{n.tag}</p>
              {n.items.length ? (
                <ul className="mt-1 list-disc pl-5 text-sm text-text-muted">{n.items.map((it, i) => <li key={i}>{it}</li>)}</ul>
              ) : (
                <p className="mt-1 text-sm text-text-muted">노트를 읽지 못했어요 (gh release view {n.tag})</p>
              )}
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}
