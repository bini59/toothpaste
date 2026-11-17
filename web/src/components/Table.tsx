import { Card, cn } from "../ui";

/** Data table shell (Card + grey-50 header). cols: fixed column widths (CSS lengths, the rest share what is left) with
 * table-layout: fixed, for tables that refresh in place so their columns never jump (TP-174). */
export function Table({ head, children, className, cols }: { head: React.ReactNode[]; children: React.ReactNode; className?: string; cols?: (string | undefined)[] }) {
  return (
    <Card className={cn("overflow-x-auto", className)}>
      <table className={cn("w-full border-collapse text-left", cols && "min-w-[52rem] table-fixed")}>
        {cols && <colgroup>{cols.map((w, i) => <col key={i} style={w ? { width: w } : undefined} />)}</colgroup>}
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={i} className="whitespace-nowrap border-b border-border bg-grey-50 px-4 py-2.5 text-xs font-semibold text-text-muted">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="[&_td]:border-b [&_td]:border-border [&_td]:px-4 [&_td]:py-2.5 [&_td]:align-top [&_td]:text-sm [&_tr:last-child_td]:border-b-0 [&_tr:hover_td]:bg-grey-50">
          {children}
        </tbody>
      </table>
    </Card>
  );
}
