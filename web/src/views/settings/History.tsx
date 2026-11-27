import { Table } from "../../components/Table";
import type { HistoryRow } from "../../types";
import { fmt, show } from "./values";

export const ACTOR: Record<string, string> = { dashboard: "대시보드", cli: "CLI", import: "가져오기" };

export function History({ rows, scope }: { rows: HistoryRow[]; scope?: boolean }) {
  if (!rows.length) return <p className="text-sm text-text-muted">아직 바뀐 적이 없어요.</p>;
  return (
    <Table head={["시각", "누가", ...(scope ? ["범위"] : []), "항목", "이전 → 새 값"]}>
      {rows.map((h) => (
        <tr key={h.id}>
          <td className="whitespace-nowrap text-text-muted">{fmt(h.ts)}</td>
          <td className="whitespace-nowrap">{ACTOR[h.actor] ?? h.actor}</td>
          {scope && <td className="whitespace-nowrap">{h.scope}</td>}
          <td className="font-mono text-xs">{h.key}</td>
          <td className="max-w-[520px] break-all font-mono text-xs">
            <span className="text-danger-700">{show(h.old)}</span> → <span className="text-success-700">{show(h.new)}</span>
          </td>
        </tr>
      ))}
    </Table>
  );
}
