import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, Card, cn } from "../ui";
import { RefreshCw } from "lucide-react";
import { fetchSystemJobLog, fetchSystemJobs } from "../api";
import { Table } from "../components/Table";
import { ago, type Status } from "../lib";
import type { SystemJob, SystemJobLog, SystemJobsResponse } from "../types";

const SOURCES: Record<SystemJob["source"], string> = { launchd: "launchd", cron: "cron", codex: "Codex 앱" };
const fmtSize = (n: number | null) => (n == null ? "없음" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);

function status(j: SystemJob): [string, Status] {
  switch (j.status) {
    case "running":
      return [`실행 중 · pid ${j.pid}`, "success"];
    case "idle":
      return ["대기 · 마지막 정상", "success"];
    case "failed":
      return [`마지막 실패 · exit ${j.last_exit}`, "danger"];
    case "disabled":
      return ["꺼짐", "draft"];
    case "not_loaded":
      return ["로드 안 됨", "warning"];
    case "scheduled":
      return ["예약됨", "info"];
    case "paused":
      return ["일시정지", "draft"];
    default:
      return ["읽기 실패", "danger"];
  }
}
const lastWrite = (j: SystemJob) => j.logs.map((l) => l.mtime).filter(Boolean).sort().at(-1) ?? null;

export function SystemJobs({ sub }: { sub: string }) {
  const [data, setData] = useState<SystemJobsResponse | null>(null);
  const [err, setErr] = useState("");
  const [source, setSource] = useState<"" | SystemJob["source"]>("");
  const [showApps, setShowApps] = useState(false);
  const [loading, setLoading] = useState(false);
  const load = useCallback(() => {
    setLoading(true);
    fetchSystemJobs()
      .then((d) => { setData(d); setErr(""); })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const jobs = (data?.jobs || []).filter((j) => (showApps || !j.app_item) && (!source || j.source === source));
  const hidden = (data?.jobs || []).filter((j) => j.app_item).length;
  const selected = data?.jobs.find((j) => j.id === sub) || null;
  const open = (id: string, i: number) => { location.hash = `jobs/${encodeURIComponent(id)}`; sessionStorage.setItem("tp.jobs.file", String(i)); };

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {(["", "launchd", "cron", "codex"] as const).map((k) => (
          <Button key={k || "all"} size="sm" variant={source === k ? "primary" : "secondary"} onClick={() => setSource(k)}>
            {k ? SOURCES[k] : "전체"}
            {/* the count's place is kept before the data arrives (TP-174) */}
            <span className="min-w-[2ch] text-xs tabular-nums opacity-70">{data ? (data.jobs || []).filter((j) => (showApps || !j.app_item) && (!k || j.source === k)).length : ""}</span>
          </Button>
        ))}
        <label className="ml-1 inline-flex cursor-pointer items-center gap-2 text-sm text-text-main">
          <input type="checkbox" className="h-4 w-4 accent-primary" checked={showApps} onChange={(e) => setShowApps(e.target.checked)} />
          <span>앱 설치 항목도 보기 (<span className="tabular-nums">{hidden}</span>)</span>
        </label>
        <div className="ml-auto flex items-center gap-2">
          <span className="min-w-[5.5rem] text-right text-xs tabular-nums text-text-muted">{data && `수집 ${ago(data.collected_at)}`}</span>
          <Button size="sm" variant="secondary" onClick={load} disabled={loading}>
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} /> 다시 수집
          </Button>
        </div>
      </div>
      {err && <p className="mb-2 text-sm text-danger-700">불러오지 못했어요: {err}</p>}
      {data?.errors.map((e) => <p key={e} className="mb-2 text-sm text-warning-700">{e}</p>)}
      {!data ? (
        !err && <p className="text-sm text-text-muted">불러오는 중…</p>
      ) : jobs.length ? (
        <Table head={["이름", "종류", "주기", "상태", "마지막 로그 기록", "로그"]}>
          {jobs.map((j) => {
            const [label, st] = status(j);
            return (
              <tr key={j.id} className={cn(j.id === sub && "[&_td]:bg-grey-50")}>
                <td className="max-w-[16rem]">
                  <div className="font-medium text-text-main">{j.name}</div>
                  <div className="truncate font-mono text-xs text-text-muted" title={j.command}>{j.command || j.file}</div>
                  {j.error && <div className="text-xs text-danger-700">{j.error}</div>}
                </td>
                <td className="whitespace-nowrap">
                  <Badge>{SOURCES[j.source]}</Badge>
                </td>
                <td className="whitespace-nowrap">{j.schedule}</td>
                <td className="whitespace-nowrap">
                  <Badge status={st}>{label}</Badge>
                </td>
                <td className="whitespace-nowrap text-text-muted">{ago(lastWrite(j))}</td>
                <td className="whitespace-nowrap">
                  {j.logs.length ? (
                    <div className="flex flex-wrap gap-x-3 gap-y-1">
                      {j.logs.map((l, i) => (
                        <button key={l.path} type="button" className="text-xs text-primary hover:underline disabled:text-text-muted disabled:no-underline"
                          disabled={l.size == null} onClick={() => open(j.id, i)} title={l.path}>
                          {l.label} <span className="text-text-muted">({fmtSize(l.size)})</span>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <span className="text-xs text-text-muted">설정된 로그 파일 없음</span>
                  )}
                </td>
              </tr>
            );
          })}
        </Table>
      ) : (
        <p className="text-sm text-text-muted">해당하는 자동화가 없어요.</p>
      )}
      {sub && (selected ? <LogPanel key={selected.id} job={selected} /> : data && <p className="mt-4 text-sm text-text-muted">자동화를 찾지 못했어요: {sub}</p>)}
    </>
  );
}

function LogPanel({ job }: { job: SystemJob }) {
  const [i, setI] = useState(() => Math.min(Number(sessionStorage.getItem("tp.jobs.file") || 0), Math.max(job.logs.length - 1, 0)));
  const [log, setLog] = useState<SystemJobLog | null>(null);
  const [err, setErr] = useState("");
  const [follow, setFollow] = useState(true);
  const pre = useRef<HTMLPreElement>(null);
  const load = useCallback(() => {
    if (!job.logs.length) return;
    fetchSystemJobLog(job.id, i).then((l) => { setLog(l); setErr(""); }).catch((e) => setErr(String(e)));
  }, [job.id, i]);
  useEffect(() => {
    load();
    if (!follow) return;
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load, follow]);
  useEffect(() => {
    if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [log, follow]);
  useEffect(() => {
    pre.current?.closest("section")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  return (
    <section className="mt-6 scroll-mt-20">
      <Card className="p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h2 className="text-base font-bold text-text-main">{job.name}</h2>
          <Badge>{SOURCES[job.source]}</Badge>
          <div className="ml-auto flex items-center gap-3">
            <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-text-main">
              <input type="checkbox" className="h-4 w-4 accent-primary" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
              5초마다 따라가기
            </label>
            <Button size="sm" variant="secondary" onClick={load}>
              <RefreshCw className="h-4 w-4" /> 새로고침
            </Button>
            <a href="#jobs" className="text-sm text-primary hover:underline">닫기</a>
          </div>
        </div>
        {job.logs.length > 1 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {job.logs.map((l, k) => (
              <Button key={l.path} size="sm" variant={k === i ? "primary" : "secondary"} onClick={() => { setI(k); sessionStorage.setItem("tp.jobs.file", String(k)); }}>
                {l.label}
              </Button>
            ))}
          </div>
        )}
        {job.logs.length ? (
          <>
            <p className="mb-2 break-all font-mono text-xs text-text-muted">
              {job.logs[i]?.path} · {fmtSize(log?.size ?? job.logs[i]?.size ?? null)} · 마지막 기록 {ago(log?.mtime ?? job.logs[i]?.mtime)}
              {log?.truncated && " · 마지막 64KB만 표시"}
            </p>
            {(err || log?.error) && <p className="mb-2 text-sm text-danger-700">{err || log?.error}</p>}
            <pre ref={pre} className="max-h-[36rem] overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-grey-50 p-3 font-mono text-xs text-text-main">
              {log ? log.log || "(비어 있음)" : "불러오는 중…"}
            </pre>
          </>
        ) : (
          <p className="text-sm text-text-muted">이 자동화에는 설정된 로그 파일이 없어요.</p>
        )}
        {job.source === "codex" && (
          <p className="mt-2 text-xs text-text-muted">Codex 앱 자동화의 실행 기록은 Codex 앱의 채팅에 남아요. 여기서는 자동화 폴더에 있는 파일(memory.md 등)만 보여요.</p>
        )}
      </Card>
    </section>
  );
}
