import { useEffect, useState } from "react";
import { getJson } from "../api";
import { Badge, Button, Card } from "../ui";

type Compaction = {
  id: number;
  title: string | null;
  finished_at: string;
  failed: boolean;
  continued: boolean;
  before_context_tokens: number | null;
  first_context_tokens: number | null;
  first_savings_tokens: number | null;
  cost_tokens: number | null;
  net_savings_tokens: number | null;
  turns: number;
  measured: number;
};

const PERIODS = [
  { value: "7", label: "7일" },
  { value: "30", label: "30일" },
  { value: "90", label: "90일" },
  { value: "all", label: "전체" },
];
const fmt = new Intl.NumberFormat("ko-KR");
const signed = new Intl.NumberFormat("ko-KR", { signDisplay: "exceptZero" });
const tokens = (n: number | null, delta = false) => n === null ? "—" : (delta ? signed : fmt).format(n);
const sum = (rows: Compaction[], key: "cost_tokens" | "net_savings_tokens") =>
  rows.some((row) => row[key] !== null) ? rows.reduce((total, row) => total + (row[key] ?? 0), 0) : null;

export function ChatCompactions() {
  const [days, setDays] = useState("30");
  const [rows, setRows] = useState<Compaction[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    getJson<{ rows: Compaction[] }>(`/api/chat/compactions?days=${days}`).then(
      (data) => { if (active) setRows(data.rows); },
      (e) => { if (active) setError(String(e)); },
    );
    return () => { active = false; };
  }, [days]);

  const noFollowup = rows?.filter((row) => !row.failed && !row.continued) ?? [];
  const failures = rows?.filter((row) => row.failed).length ?? 0;
  const missingCost = rows?.filter((row) => row.cost_tokens === null).length ?? 0;
  const missingEstimate = rows?.filter((row) => row.net_savings_tokens === null).length ?? 0;
  const missingNoFollowup = noFollowup.filter((row) => row.cost_tokens === null).length;
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-bold text-text-main sm:text-2xl">채팅 압축</h1>
        <div role="group" aria-label="압축 기간" className="flex flex-wrap items-center gap-1">
          {PERIODS.map(({ value, label }) => (
            <Button key={value} size="sm" variant={days === value ? "primary" : "secondary"}
              aria-pressed={days === value} onClick={() => {
                if (days === value) return;
                setRows(null);
                setError("");
                setDays(value);
              }}>{label}</Button>
          ))}
        </div>
      </div>
      {rows === null ? (
        <Card className="p-4 text-sm text-text-muted" role={error ? "alert" : "status"}>
          {error ? `불러오지 못했어요 (${error})` : "불러오는 중…"}
        </Card>
      ) : (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 xl:grid-cols-4">
            <Metric label="압축 횟수" value={`${fmt.format(rows.length)}회`}
              detail={`성공 ${fmt.format(rows.length - failures)}회 · 실패 ${fmt.format(failures)}회`} />
            <Metric label="압축 비용" value={tokens(sum(rows, "cost_tokens"))}
              detail={missingCost ? `토큰 · 비용 미기록 ${fmt.format(missingCost)}회` : "토큰"} />
            <Metric label="추정 순절감량" value={tokens(sum(rows, "net_savings_tokens"), true)}
              detail={missingEstimate ? `토큰 · 추정 불가 ${fmt.format(missingEstimate)}회` : "토큰"} />
            <Metric label="후속 대화 없음" value={`${fmt.format(noFollowup.length)}회`}
              detail={noFollowup.length ? `비용 ${tokens(sum(noFollowup, "cost_tokens"))} 토큰${missingNoFollowup ? ` · 미기록 ${fmt.format(missingNoFollowup)}회` : ""}` : undefined} />
          </div>
          <Card className="overflow-hidden">
            <div className="p-4 sm:px-5">
              <h2 className="text-base font-semibold text-text-main">압축 기록</h2>
              <p className="mt-1 text-sm text-text-muted">
                각 후속 턴의 마지막 호출에서 줄어든 입력 컨텍스트를 더하고 압축 비용을 빼요. 모든 수치는 토큰 단위예요.
              </p>
            </div>
            {rows.length === 0 ? <p className="border-t border-border p-4 text-sm text-text-muted sm:px-5">이 기간에 완료된 압축이 없어요.</p> : (
              <div className="overflow-x-auto" role="region" aria-label="압축 기록 표" tabIndex={0}>
                <table className="w-full whitespace-nowrap border-t border-border text-sm tabular-nums">
                  <thead className="bg-grey-50 text-xs text-text-muted">
                    <tr>
                      {["완료 시각", "대화", "상태", "압축 전", "압축 후 첫 턴", "첫 턴 절감", "압축 비용", "추정 순절감량"].map((label, i) => (
                        <th key={label} scope="col" className={`px-4 py-2 font-medium ${i < 3 ? "text-left" : "text-right"}`}>{label}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="text-text-main">
                    {rows.map((row) => (
                      <tr key={row.id} className="border-t border-border">
                        <td className="px-4 py-3 text-text-muted">{new Date(row.finished_at).toLocaleString("ko-KR")}</td>
                        <td className="max-w-60 truncate px-4 py-3 font-medium" title={row.title ?? undefined}>{row.title || "제목 없는 대화"}</td>
                        <td className="px-4 py-3">
                          <Badge status={row.failed ? "danger" : "success"}>{row.failed ? "실패" : "성공"}</Badge>
                          {!row.failed && !row.continued && <span className="ml-2 text-xs text-text-muted">후속 대화 없음</span>}
                        </td>
                        <td className="px-4 py-3 text-right">{tokens(row.before_context_tokens)}</td>
                        <td className="px-4 py-3 text-right">{tokens(row.first_context_tokens)}</td>
                        <td className="px-4 py-3 text-right">{tokens(row.first_savings_tokens, true)}</td>
                        <td className="px-4 py-3 text-right">{tokens(row.cost_tokens)}</td>
                        <td className="px-4 py-3 text-right font-medium">
                          {tokens(row.net_savings_tokens, true)}
                          {row.turns > 0 && <div className="text-xs font-normal text-text-muted">측정 {row.measured}/{row.turns}턴</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

function Metric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <Card className="p-4">
      <h2 className="text-sm font-medium text-text-muted">{label}</h2>
      <p className="mt-1 text-2xl font-bold tabular-nums text-text-main">{value}</p>
      {detail && <p className="mt-1 text-xs text-text-muted">{detail}</p>}
    </Card>
  );
}
