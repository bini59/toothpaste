import { Badge, Card } from "../ui";
import { VERDICT, ago } from "../lib";
import type { State } from "../types";

export function Reviews({ s }: { s: State }) {
  const tk = Object.fromEntries(s.tickets.map((t) => [t.gid, t]));
  if (!s.reviews.length) return <Card className="p-5 text-center text-sm text-text-muted">아직 리뷰가 없어요.</Card>;
  return (
    <div className="flex flex-col gap-3">
      {s.reviews.map((v) => {
        const t = tk[v.ticket_gid];
        const p = s.prs.find((x) => x.url === v.pr_url);
        const [label, status] = VERDICT[v.verdict] ?? [v.verdict, "draft"];
        return (
          <Card key={v.id} className="p-5">
            <div className="mb-2.5 flex flex-wrap items-center gap-2 text-sm">
              <Badge status={status}>{label}</Badge>
              <b className="font-semibold">{t?.key}</b>
              <span className="text-text-main">{p?.title}</span>
              <a href={v.pr_url} target="_blank" rel="noreferrer" className="text-primary hover:text-primary-700 hover:underline">
                PR #{p?.number}
              </a>
              {v.comment_url ? (
                <a href={v.comment_url} target="_blank" rel="noreferrer" className="text-primary hover:text-primary-700 hover:underline">
                  코멘트
                </a>
              ) : (
                <Badge status="warning">미게시</Badge>
              )}
              <span className="text-xs text-text-muted">
                {(v.head_sha || "").slice(0, 7)} · {ago(v.created_at)}
              </span>
            </div>
            <p className="text-sm">
              <b className="font-semibold">요약:</b> {v.summary}
            </p>
            <details className="mt-2">
              <summary className="cursor-pointer text-xs font-medium text-text-muted hover:text-text-main">전체 리뷰</summary>
              <pre className="mt-2 max-h-[420px] overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-grey-50 p-3 font-mono text-xs leading-relaxed">{v.body}</pre>
            </details>
          </Card>
        );
      })}
    </div>
  );
}
