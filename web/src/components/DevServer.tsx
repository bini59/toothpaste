import { useEffect, useState } from "react";
import { Button, Card, Input } from "../ui";
import { ConfirmButton } from "./ConfirmButton";
import { PortConflict, devCheck, devStart, post } from "../api";
import { ago, milestoneRepo } from "../lib";
import type { DevCheck, DevConflict, State, Ticket } from "../types";

const url = (port: number) => `http://${location.hostname}:${port}`;

function Procs({ c }: { c: DevConflict }) {
  return (
    <ul className="mt-1.5 space-y-1">
      {c.busy.map((b, i) => (
        <li key={b.pid ?? i} className="break-all font-mono text-[11px] leading-snug text-text-main">
          {b.pid ? <b>pid {b.pid}</b> : null} {b.cmd}
          {b.cwd && <div className="text-text-muted">{b.cwd}</div>}
        </li>
      ))}
    </ul>
  );
}

/** Port in use: warn, then kill those processes and start there, or start on another port */
function Busy({ c, fixed, ours, busy, onStart }: { c: DevConflict; fixed: boolean; ours: boolean; busy: string | null; onStart: (port: number, kill: boolean) => void }) {
  const [port, setPort] = useState(String(c.free));
  useEffect(() => setPort(String(c.free)), [c.free]);
  const n = Number(port);
  return (
    <div className="rounded-lg border border-warning-200 bg-warning-50 p-3">
      <p className="text-xs font-semibold text-warning-700">
        {c.port} 포트가 이미 열려 있어요{ours ? " (다른 마일스톤 dev 서버)" : ""}
      </p>
      {c.busy.length > 0 && <Procs c={c} />}
      <div className="mt-3 flex flex-col gap-2">
        {!ours && c.busy.some((b) => b.pid) && (
          <ConfirmButton size="sm" variant="secondary" disabled={!!busy} loading={busy === "kill"} confirmLabel="종료하고 띄우기"
            message={<>위 프로세스를 종료하고 {c.port} 포트에 띄워요. 그 프로세스에서 하던 작업은 사라져요.</>}
            onConfirm={() => onStart(c.port, true)}>
            기존 프로세스 죽이고 {c.port}에 띄우기
          </ConfirmButton>
        )}
        <div className="flex items-center gap-2">
          <Input className="h-8 w-24 text-sm" inputMode="numeric" value={port} disabled={fixed}
            onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))} />
          <Button size="sm" disabled={!!busy || fixed || !(n >= 1024 && n <= 65535)} loading={busy === "start"} onClick={() => onStart(n, false)}>
            다른 포트에 띄우기
          </Button>
        </div>
        {fixed && <p className="text-xs text-text-muted">dev 스크립트가 포트를 고정해서 다른 포트로는 못 띄워요. 설정 &gt; 기본 &gt; dev 서버 명령에 {"{port}"}를 넣으면 돼요.</p>}
      </div>
    </div>
  );
}

function Preflight({ c, busy, onStart, onClose }: { c: DevCheck; busy: string | null; onStart: (port: number, kill: boolean) => void; onClose: () => void }) {
  const conflict: DevConflict | null = c.busy.length || c.ours ? { port: c.port, busy: c.busy, free: c.free ?? c.port + 1 } : null;
  return (
    <div className="space-y-3 text-xs">
      {c.notes.map((n) => <p key={n} className="text-text-muted">{n}</p>)}
      <div>
        <div className="font-semibold text-text-main">env</div>
        <p className="mt-0.5 text-text-muted">
          {c.env.files.length ? c.env.files.join(", ") : "env 파일 없음"}
          {c.env.copied.length > 0 && <> · 메인 체크아웃에서 복사: {c.env.copied.join(", ")}</>}
        </p>
        {c.env.warnings.length > 0 ? (
          <ul className="mt-1 space-y-0.5">
            {c.env.warnings.map((w) => <li key={w} className="text-warning-700">⚠ {w}</li>)}
          </ul>
        ) : (
          c.env.files.length > 0 && <p className="mt-0.5 text-success-700">.env.example 키가 모두 채워져 있어요</p>
        )}
      </div>
      <div>
        <div className="font-semibold text-text-main">명령</div>
        <code className="mt-0.5 block break-all font-mono text-[11px] text-text-main">{c.cmd.replaceAll("{port}", String(c.port))}</code>
        <p className="mt-0.5 text-text-muted">{c.how}</p>
      </div>
      {conflict ? (
        <Busy c={conflict} fixed={c.fixed} ours={c.ours} busy={busy} onStart={onStart} />
      ) : (
        <Button size="sm" disabled={!!busy} loading={busy === "start"} onClick={() => onStart(c.port, false)}>
          {c.port} 포트에 띄우기
        </Button>
      )}
      <button type="button" className="text-text-muted hover:text-text-main hover:underline" onClick={onClose}>닫기</button>
    </div>
  );
}

/** 마일스톤 dev 서버 (devserver.py): one per milestone ticket (repo), on its milestone branch */
export function DevServerPanel({ t, s, onReload }: { t: Ticket; s: State; onReload: () => void }) {
  const d = (s.dev_servers ?? []).find((x) => x.gid === t.gid);
  const [check, setCheck] = useState<DevCheck | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const starting = !!d && d.alive && !d.listening;
  useEffect(() => {
    if (!starting) return;
    const id = setInterval(onReload, 3000);
    return () => clearInterval(id);
  }, [starting, onReload]);

  // busy: the running action, its button shows the spinner
  const run = async (label: "check" | "start" | "kill" | "stop", f: () => Promise<void>) => {
    setBusy(label);
    setErr(null);
    try { await f(); } catch (e) { setErr((e as Error).message); }
    setBusy(null);
  };
  const doCheck = () => run("check", async () => {
    const c = await devCheck(t.gid);
    if (c.running) { setCheck(null); onReload(); return; }
    setCheck(c);
  });
  const doStart = (port: number, kill: boolean) => run(kill ? "kill" : "start", async () => {
    try {
      await devStart(t.gid, port, kill);
      setCheck(null);
      onReload();
    } catch (e) {
      if (e instanceof PortConflict && check) setCheck({ ...check, port: e.info.port, busy: e.info.busy, free: e.info.free, ours: false });
      throw e;
    }
  });
  const doStop = () => run("stop", async () => { await post("/api/dev/stop", { gid: t.gid }); onReload(); });

  return (
    <Card className="p-4">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-sm font-semibold text-text-main">{milestoneRepo(t.gid).split("_").at(-1)}</span>
        {d && (
          <span className={d.listening ? "text-xs font-medium text-success-700" : d.alive ? "text-xs text-text-muted" : "text-xs font-medium text-danger-700"}>
            {d.listening ? "실행 중" : d.alive ? "시작하는 중…" : "멈췄어요"}
          </span>
        )}
      </div>
      {d ? (
        <div className="space-y-2 text-xs">
          {d.listening ? (
            <a href={url(d.port)} target="_blank" rel="noreferrer" className="font-medium text-primary hover:underline">{url(d.port)}</a>
          ) : (
            <span className="text-text-muted">:{d.port}</span>
          )}
          <p className="break-all text-text-muted">
            <code className="font-mono text-[11px]">{d.cmd}</code> · {ago(d.started)} 시작
          </p>
          {d.tail && <pre className="max-h-48 overflow-auto rounded-lg bg-grey-100 p-2 font-mono text-[11px] leading-snug text-text-main">{d.tail}</pre>}
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" disabled={!!busy} loading={busy === "stop"} onClick={doStop}>{d.alive ? "중지" : "지우기"}</Button>
            {!d.alive && <Button size="sm" disabled={!!busy} loading={busy === "check"} onClick={async () => { await post("/api/dev/stop", { gid: t.gid }); await doCheck(); }}>다시 띄우기</Button>}
          </div>
        </div>
      ) : check ? (
        <Preflight c={check} busy={busy} onStart={doStart} onClose={() => setCheck(null)} />
      ) : (
        <Button size="sm" disabled={!!busy} loading={busy === "check"} onClick={doCheck}>dev 서버 띄우기</Button>
      )}
      {err && <p className="mt-2 text-xs text-danger-700">{err}</p>}
    </Card>
  );
}

