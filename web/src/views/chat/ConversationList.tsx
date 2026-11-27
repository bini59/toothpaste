import { useState } from "react";
import { MessageSquare, Plus, Search } from "lucide-react";
import { ago } from "../../lib";
import { Input, cn } from "../../ui";
import { projectHref } from "../Projects";
import type { ChatConversation } from "./types";

export function UnreadBadge({ n }: { n: number }) {
  return <span className="shrink-0 rounded-full bg-primary px-1.5 text-[11px] font-semibold leading-[18px] text-white" aria-label={`안 읽은 답 ${n}개`}>{n}</span>;
}

/** 오늘 / 어제 / 이번 주 (from Monday) / 이전, by local date */
const GROUPS = ["오늘", "어제", "이번 주", "이전"] as const;
function group(v: string) {
  const day = new Date(); day.setHours(0, 0, 0, 0);
  const t = new Date(v).getTime();
  if (t >= day.getTime()) return 0;
  const yesterday = new Date(day); yesterday.setDate(day.getDate() - 1);
  if (t >= yesterday.getTime()) return 1;
  const monday = new Date(day); monday.setDate(day.getDate() - ((day.getDay() + 6) % 7));
  return t >= monday.getTime() ? 2 : 3;
}

export function ConversationList({ projectId, conv, list, error }: {
  projectId: string; conv: string; list: ChatConversation[] | null; error: string;
}) {
  const [query, setQuery] = useState("");
  // title and preview only, as the list API gives them (newest first)
  const q = query.trim().toLowerCase();
  const shown = list?.filter((c) => !q || `${c.title}\n${c.preview ?? ""}`.toLowerCase().includes(q)) ?? [];
  const groups = GROUPS.map((label, i) => ({ label, items: shown.filter((c) => group(c.updated_at) === i) })).filter((g) => g.items.length);
  return (
    <aside className={cn("min-w-0 flex-col border-border max-lg:max-h-[45%] max-lg:border-b lg:w-[280px] lg:shrink-0 lg:border-r", conv ? "hidden lg:flex" : "flex")}>
      <div className="space-y-2 border-b border-border p-3">
        <a href={projectHref(projectId)} aria-current={!conv ? "page" : undefined}
          className={cn("flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-semibold transition-colors", !conv ? "bg-primary-50 text-primary-700" : "text-text-main hover:bg-grey-50")}>
          <Plus className="h-4 w-4" /> 새 대화
        </a>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" />
          <Input type="search" aria-label="대화 검색" placeholder="제목 · 내용 검색" className="h-8 pl-8 text-sm" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
      </div>
      <nav aria-label="대화 목록" className="min-h-0 flex-1 overflow-y-auto p-2">
        {error && <p className="px-3 py-2 text-xs text-danger-700">{error}</p>}
        {list && !shown.length && <p className="px-3 py-6 text-center text-xs text-text-muted">{q ? "검색 결과가 없어요" : "아직 대화가 없어요"}</p>}
        {groups.map((g) => (
          <section key={g.label} className="mb-2">
            <h2 className="px-3 pb-1 pt-2 text-[11px] font-medium text-text-muted">{g.label}</h2>
            {g.items.map((c) => (
              <a key={c.id} href={projectHref(projectId, "chat", c.id)} aria-current={c.id === conv ? "page" : undefined}
                className={cn("block rounded-lg px-3 py-2 text-sm transition-colors", c.id === conv ? "bg-primary-50 text-primary-700" : "text-text-main hover:bg-grey-50")}>
                <span className="flex items-center gap-1.5">
                  <MessageSquare className="h-4 w-4 shrink-0 text-text-muted" />
                  <span className={cn("min-w-0 flex-1 truncate", c.unread ? "font-bold" : "font-medium")}>{c.title}</span>
                  {!!c.running && <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-primary" aria-label="답 쓰는 중" />}
                  {!!c.unread && <UnreadBadge n={c.unread} />}
                  <span className="shrink-0 text-[11px] text-text-muted">{ago(c.updated_at)}</span>
                </span>
                {c.preview && <span className="mt-0.5 block truncate pl-[22px] text-xs text-text-muted">{c.preview}</span>}
              </a>
            ))}
          </section>
        ))}
      </nav>
    </aside>
  );
}
