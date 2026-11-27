import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Button, Card } from "../../ui";

const rowHeight = 48;
const headerHeight = 36;

export function MetricsTable<T>({ label, columns, rows, rowKey, renderRow }: {
  label: string; columns: { label: string; className?: string }[]; rows: T[];
  rowKey: (row: T) => number | string; renderRow: (row: T) => ReactNode;
}) {
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(1);
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      setPerPage(Math.max(1, Math.floor((entry.contentRect.height - headerHeight) / rowHeight)));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const pages = Math.max(1, Math.ceil(rows.length / perPage));
  const page = Math.min(Math.floor(offset / perPage), pages - 1);
  const prev = () => setOffset((page - 1) * perPage);
  const next = () => setOffset((page + 1) * perPage);

  return (
    <Card className="flex min-h-[126px] min-w-0 flex-1 flex-col">
      <div ref={host} className="min-h-0 flex-1">
        <table aria-label={label} className="w-full table-fixed text-left text-sm tabular-nums">
          <thead className="bg-grey-50 text-xs text-text-muted">
            <tr style={{ height: headerHeight }}>
              {columns.map((col) => <th key={col.label} scope="col" className={col.className}>{col.label}</th>)}
            </tr>
          </thead>
          <tbody className="text-text-main">
            {rows.slice(page * perPage, (page + 1) * perPage).map((row) =>
              <tr key={rowKey(row)} className="border-t border-border" style={{ height: rowHeight }}>{renderRow(row)}</tr>)}
          </tbody>
        </table>
      </div>
      <nav aria-label={`${label} 페이지`} className="flex shrink-0 items-center justify-between gap-2 border-t border-border px-2 py-1 sm:px-3">
        <Button size="sm" variant="secondary" onClick={prev} disabled={page === 0} aria-label="이전 페이지">이전</Button>
        <span className="text-center text-xs tabular-nums text-text-muted" aria-live="polite">
          {page + 1} / {pages} 페이지 · {rows.length.toLocaleString("ko-KR")}개
        </span>
        <Button size="sm" variant="secondary" onClick={next} disabled={page + 1 === pages} aria-label="다음 페이지">다음</Button>
      </nav>
    </Card>
  );
}

export function MetricsDialog({ title, children, onClose }: {
  title: string; children: ReactNode; onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog ref={ref} aria-labelledby={titleId} onClose={(e) => { if (!e.currentTarget.open) onClose(); }}
      className="m-auto max-h-[calc(100dvh-2rem)] w-[min(37rem,calc(100vw-2rem))] overflow-y-auto rounded-xl border border-border bg-surface p-4 text-text-main shadow-xl backdrop:bg-grey-900 backdrop:opacity-40 sm:p-5">
      <div className="mb-4 flex items-start justify-between gap-3">
        <h2 id={titleId} className="min-w-0 text-base font-bold [overflow-wrap:anywhere]">{title}</h2>
        <Button size="sm" variant="secondary" onClick={() => ref.current?.close()}>닫기</Button>
      </div>
      {children}
    </dialog>
  );
}

export function MetricsStatus({ text, error }: { text: string; error?: boolean }) {
  return <Card role={error ? "alert" : "status"} className={`p-4 text-sm ${error ? "text-danger-700" : "text-text-muted"}`}>{text}</Card>;
}
