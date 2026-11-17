import { useEffect, useState } from "react";
import { Badge } from "../ui";
import { fetchAutomations } from "../api";
import { Table } from "../components/Table";
import { ROLES, ago, ofRole, type Role, type Status } from "../lib";
import type { AutomationsResponse, State } from "../types";

const kindStatus = (k: string): Status =>
  k === "error" || k === "violation" || k === "agent_timeout" ? "danger" : k === "dispatch" || k === "tagged" || k === "asana_write" ? "info" : k === "stage" ? "success" : "draft";
const runStatus = (st: string): Status => (/skip/.test(st) ? "warning" : /fail|error/.test(st) ? "danger" : /complete|done|success/.test(st) ? "success" : "info");

export function Log({ s, role }: { s: State; role?: Role }) {
  const [a, setA] = useState<AutomationsResponse | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    fetchAutomations().then(setA).catch((e) => setErr(String(e)));
  }, [s]);
  const list = (a?.automations || []).filter((x) => /ticket-pilot/i.test(x.name) && (!role || ROLES[role].re.test(x.name)));
  const events = role ? s.events.filter((e) => ofRole(role, e)) : s.events;
  const usage = Object.fromEntries((a?.items || []).map((i) => [i.automationId, i.usageSummary || {}]));
  return (
    <>
      {/* events first: they come with the state, the automations table loads after and would push them down (TP-174) */}
      <section>
        <h2 className="mb-3 text-base font-bold text-text-main sm:text-lg">이벤트</h2>
        {events.length ? (
          <Table head={["시각", "종류", "누가", "내용"]} cols={["13.5rem", "10.5rem", "8rem", undefined]}>
            {events.map((e) => (
            <tr key={e.id}>
              <td className="whitespace-nowrap tabular-nums text-text-muted">{new Date(e.ts).toLocaleString("ko-KR")}</td>
              <td>
                <Badge status={kindStatus(e.kind)}>{e.kind}</Badge>
              </td>
              <td className="truncate font-mono text-xs" title={e.actor ?? undefined}>{e.actor ?? <span className="text-text-muted">tp</span>}</td>
              <td className="break-words">{e.message}</td>
            </tr>
            ))}
          </Table>
        ) : (
          <p className="text-sm text-text-muted">최근 이벤트 중 {role ? ROLES[role].label : ""} 이벤트가 없어요.</p>
        )}
      </section>
      <section className="mt-8">
        <h2 className="mb-3 text-base font-bold text-text-main sm:text-lg">자동화 (tp tick)</h2>
        {err ? (
          <p className="text-sm text-text-muted">불러오기 실패: {err}</p>
        ) : !a ? (
          <p className="text-sm text-text-muted">불러오는 중…</p>
        ) : list.length ? (
          <>
            <Table head={["이름", "주기", "상태", "다음 실행", "마지막 실행", "실행 기록"]}>
              {list.map((x) => {
                const u = usage[x.id] || {};
                return (
                  <tr key={x.id}>
                    <td className="font-medium">{x.name}</td>
                    <td>
                      <code className="rounded-md bg-grey-100 px-1.5 py-0.5 font-mono text-xs">{x.rrule}</code>
                    </td>
                    <td>{x.enabled ? <Badge status="success">켜짐</Badge> : <Badge>꺼짐</Badge>}</td>
                    <td className="whitespace-nowrap">{x.nextRunAt ? new Date(x.nextRunAt).toLocaleString("ko-KR") : "-"}</td>
                    <td className="whitespace-nowrap">
                      {x.lastRunAt ? ago(x.lastRunAt) : "-"} {u.lastRunStatus && <Badge status={runStatus(u.lastRunStatus)}>{u.lastRunStatus}</Badge>}
                    </td>
                    <td className="text-text-muted">
                      {u.knownRuns ?? 0}회 · ${(u.estimatedCostUsd ?? 0).toFixed(2)}
                    </td>
                  </tr>
                );
              })}
            </Table>
            {a.captured_at && <p className="mt-1.5 text-xs text-text-muted">스냅샷 {ago(a.captured_at)}</p>}
          </>
        ) : (
          <p className="text-sm text-text-muted">{a.error || "toothpaste automation이 없어요."}</p>
        )}
      </section>
    </>
  );
}
