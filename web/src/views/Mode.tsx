import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Card, Input } from "../ui";
import { getJson, postJson } from "../api";
import { ConfirmButton } from "../components/ConfirmButton";
import type { ModeInfo } from "../types";

const LABEL = { macos: "macOS 직접", linux: "Linux 직접", docker: "Docker" } as const;
// tp mode's last lines (mode.py switch): "끝났어요 (...)" | "실패: why" | "바꿀 수 없어요 (...)"
const END = /^(끝났어요|실패:|바꿀 수 없어요)/;
const POLL_MS = 2000;

/** 설정 → 실행 방식 (TP-165): the current way, the Docker environment check, and the switch (`tp mode docker|host`):
 * 미리보기 first, then the run detached like 업데이트. Host -> Docker stops this dashboard mid-way and the container's
 * one answers on the same address with a login, so polling ends at a 401 (or keeps failing while it builds). */
export function ModeSection() {
  const [info, setInfo] = useState<ModeInfo | null>(null);
  const [err, setErr] = useState("");
  const [admins, setAdmins] = useState("");
  const [preview, setPreview] = useState<{ ok: boolean; text: string; to: "docker" | "host" } | null>(null);
  const [since, setSince] = useState<number | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [result, setResult] = useState<"" | "ok" | "fail" | "login" | "away">("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => getJson<ModeInfo>("/api/mode").then((d) => { setInfo(d); setErr(""); }).catch((e) => setErr(String(e))), []);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (since == null || (result && result !== "away")) return;
    const id = setInterval(async () => {
      try {
        const r = await fetch(`/api/mode?since=${since}`);
        if (r.status === 401) return setResult("login"); // the container's dashboard is up
        const d: ModeInfo = await r.json();
        const log = d.log ?? [];
        setLines(log);
        const end = log.map((l) => END.exec(l)?.[1]).filter(Boolean).at(-1);
        if (end) { load(); setResult(end === "끝났어요" ? "ok" : "fail"); }
      } catch {
        setResult("away"); // this dashboard was stopped; the next one is still starting
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [since, result, load]);

  const to = info?.state && !info.state.finished ? info.state.to : info?.current === "docker" ? "host" : "docker";
  const showPreview = () => {
    setErr(""); setBusy(true);
    postJson<{ ok: boolean; text: string }>("/api/mode/preview", { to, admins: admins.trim() || undefined })
      .then((p) => setPreview({ ...p, to })).catch((e) => setErr((e as Error).message)).finally(() => setBusy(false));
  };
  const start = () => {
    setErr(""); setLines([]); setResult("");
    postJson<{ since: number }>("/api/mode", { to: preview!.to, admins: admins.trim() || undefined })
      .then((r) => setSince(r.since)).catch((e) => setErr((e as Error).message));
  };

  if (!info) return <p className="text-sm text-text-muted">{err ? `불러오지 못했어요: ${err}` : "불러오는 중…"}</p>;
  const unfinished = info.state && !info.state.finished ? info.state : null;
  const allOk = !!info.checks && info.checks.every((c) => c.ok);
  const running = since != null && (!result || result === "away");
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card className="p-5">
        <h2 className="text-base font-bold text-text-main">
          지금 방식: <Badge status={info.current === "docker" ? "info" : "draft"}>{LABEL[info.current]}</Badge>
        </h2>
        <p className="mt-1 text-sm text-text-muted">
          호스트에 직접 설치(launchd / systemd)하거나 Docker 컨테이너로 돌려요. 바꾸면 DB, 토큰, 저장소, 에이전트 로그인이 함께 옮겨가요.
          Docker에서는 이 주소도 GitHub 로그인이 필요해요.
        </p>
        {unfinished && (
          <p className="mt-3 text-sm text-warning-700">
            {unfinished.to === "docker" ? "Docker로" : "호스트로"} 바꾸다 멈췄어요 (끝난 단계: {unfinished.done.join(", ") || "없음"}).
            다시 실행하면 이어서 해요.
          </p>
        )}
        {info.command && (
          <div className="mt-3 text-sm text-text-main">
            컨테이너 안에서는 docker를 쓸 수 없어서 호스트에서 바꿔요. 호스트의 저장소 폴더에서:
            <pre className="mt-2 overflow-x-auto rounded-lg border border-border bg-grey-50 p-3 font-mono text-xs">{info.command}</pre>
          </div>
        )}
      </Card>

      {info.checks && (
        <Card className="p-5">
          <h3 className="mb-2 text-sm font-bold text-text-main">Docker 환경 점검</h3>
          <ul className="flex flex-col gap-2">
            {info.checks.map((c) => (
              <li key={c.id} className="text-sm">
                <span className={c.ok ? "font-semibold text-success-700" : "font-semibold text-danger-700"}>{c.ok ? "통과" : "실패"}</span>
                <span className="ml-2 text-text-main">{c.title}</span>
                <span className="ml-2 text-text-muted">{c.detail}</span>
                {c.fix && <p className="mt-0.5 pl-10 text-xs text-text-muted">→ {c.fix}</p>}
              </li>
            ))}
          </ul>
          <Button size="sm" variant="secondary" className="mt-3" onClick={load} disabled={running}>다시 점검</Button>
        </Card>
      )}

      {!info.command && !running && result !== "ok" && result !== "login" && (
        <Card className="p-5">
          <h3 className="text-sm font-bold text-text-main">{to === "docker" ? "Docker로 바꾸기" : "호스트로 돌아가기"}</h3>
          {to === "docker" && (
            <label className="mt-2 block text-sm text-text-muted">
              관리자 GitHub 아이디 (TP_ADMINS, 쉼표로 구분)
              <Input className="mt-1 max-w-sm" value={admins} placeholder="비우면 연결된 GitHub 계정" onChange={(e) => setAdmins(e.target.value)} />
            </label>
          )}
          <Button className="mt-3" variant="secondary" onClick={showPreview} disabled={to === "docker" && !allOk} loading={busy}>
            {unfinished ? "이어서 하기: 미리보기" : "미리보기"}
          </Button>
          {to === "docker" && !allOk && <p className="mt-2 text-xs text-text-muted">Docker 환경 점검을 모두 통과해야 바꿀 수 있어요.</p>}
          {preview && (
            <>
              <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-grey-50 p-3 font-mono text-xs">{preview.text}</pre>
              {preview.ok && (
                <ConfirmButton className="mt-3" confirmLabel="바꾸기"
                  message="대시보드가 멈췄다가 새 방식으로 다시 떠요. 진행 중인 작업이 없는지 확인했어요."
                  onConfirm={start}>
                  {preview.to === "docker" ? "Docker로 바꾸기" : "호스트로 돌아가기"}
                </ConfirmButton>
              )}
            </>
          )}
        </Card>
      )}

      {(since != null || err) && (
        <Card className="p-5">
          {running && <p className="text-sm text-text-main">바꾸는 중… {result === "away" && "이 대시보드는 멈췄어요. 새 대시보드가 뜨기를 기다려요 (이미지 빌드는 몇 분 걸려요)."}</p>}
          {result === "login" && (
            <div className="text-sm text-text-main">
              Docker에서 대시보드가 떴어요. 같은 주소({location.origin})에서 관리자 GitHub 계정으로 로그인하세요.
              <Button className="mt-3 block" onClick={() => location.reload()}>로그인 화면으로</Button>
            </div>
          )}
          {result === "ok" && (
            <div className="text-sm text-text-main">
              바꿨어요. 새로고침하세요.
              <Button className="mt-3 block" variant="secondary" onClick={() => location.reload()}>새로고침</Button>
            </div>
          )}
          {result === "fail" && <p className="text-sm text-danger-700">바꾸지 못했어요. 아래 기록의 안내대로 고친 뒤 다시 실행하면 이어서 해요.</p>}
          {err && <p className="text-sm text-danger-700">{err}</p>}
          {lines.length > 0 && (
            <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap text-xs text-text-muted">{lines.join("\n")}</pre>
          )}
          {since != null && <p className="mt-2 text-xs text-text-muted">전체 기록: ~/tp-work/logs/mode.log</p>}
        </Card>
      )}
    </div>
  );
}
