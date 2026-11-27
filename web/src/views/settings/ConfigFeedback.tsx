import { Table } from "../../components/Table";
import type { ConfigChange } from "../../types";
import { show } from "./values";

/** before -> after rows for the confirm step (also used for imports) */
export function ChangeTable({ changes }: { changes: ConfigChange[] }) {
  return (
    <Table head={["범위", "항목", "이전 값", "새 값"]}>
      {changes.map((c, i) => (
        <tr key={i}>
          <td className="whitespace-nowrap text-text-muted">{c.scope}</td>
          <td className="font-mono text-xs">{c.key}</td>
          <td className="max-w-[320px] break-all font-mono text-xs text-danger-700">{show(c.old)}</td>
          <td className="max-w-[320px] break-all font-mono text-xs text-success-700">{show(c.new)}</td>
        </tr>
      ))}
    </Table>
  );
}
export function Errors({ errors }: { errors: string[] }) {
  if (!errors.length) return null;
  return (
    <div role="alert" className="rounded-lg border border-danger-200 bg-danger-50 p-3 text-sm text-danger-700">
      <p className="font-semibold">저장하지 않았어요. 아래를 고쳐 주세요.</p>
      <ul className="mt-1 list-disc pl-5">
        {errors.map((e) => (
          <li key={e}>{e}</li>
        ))}
      </ul>
    </div>
  );
}
