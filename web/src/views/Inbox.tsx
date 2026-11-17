import { useCallback, useEffect, useState } from "react";
import { Bell, Bot, CheckCheck, CircleHelp, MessageSquare, Milestone, Rocket } from "lucide-react";
import { Button } from "../ui";
import { getJson, postJson } from "../api";
import { ago } from "../lib";
import type { InboxItem, InboxNotification } from "../types";
import { UnreadBadge } from "./ProjectChat";
import { projectHref } from "./Projects";

const KIND = {
  review: { label: "AI 리뷰", icon: Bot },
  ms_ready: { label: "마일스톤", icon: Milestone },
  deploy: { label: "배포", icon: Rocket },
  blocked: { label: "답변 필요", icon: CircleHelp },
} as const;
type Row = { at: string; chat?: InboxItem; alert?: InboxNotification };

/** 수신함 (TP-103): every project's conversations with unread replies and the unread pipeline alerts (TP-105), newest
 * first. Opening a conversation marks it read there (ProjectChat's Thread), clicking an alert marks it read here;
 * 모두 읽음 marks only what is listed here. */
export function Inbox({ onChanged }: { onChanged: () => void }) {
  const [items, setItems] = useState<InboxItem[] | null>(null);
  const [alerts, setAlerts] = useState<InboxNotification[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // browser notifications (TP-134, App's notifyNew); undefined: this browser has none
  const [perm, setPerm] = useState(() => (typeof Notification === "undefined" ? undefined : Notification.permission));
  const load = useCallback(() => getJson<{ items: InboxItem[]; notifications: InboxNotification[] }>("/api/chat/inbox")
    .then((r) => { setItems(r.items); setAlerts(r.notifications); setError(""); }).catch((e) => setError(String(e))), []);
  useEffect(() => {
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, [load]);
  const rows: Row[] = [...(items ?? []).map((c) => ({ at: c.at, chat: c })), ...alerts.map((a) => ({ at: a.ts, alert: a }))]
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  const refresh = async () => { await load(); onChanged(); };
  const readAll = async () => {
    if (!rows.length) return;
    setBusy(true);
    try {
      await postJson("/api/chat/read-all", {
        items: (items ?? []).map((i) => ({ id: i.id, upto: i.last_id })),
        notifications_upto: alerts.length ? Math.max(...alerts.map((a) => a.id)) : null,
      });
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      await refresh();
    }
  };
  // the link navigates on its own (a PR in a new tab, a dashboard page here); the read mark goes along
  const readAlert = (id: number) => postJson("/api/notifications/read", { id }).catch(() => {}).finally(refresh);
  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-3 flex items-center gap-2">
        <h1 className="flex-1 whitespace-nowrap text-base font-bold text-text-main sm:text-lg">수신함</h1>
        {perm === "default" && (
          <Button size="sm" variant="secondary" onClick={() => Notification.requestPermission().then(setPerm)}>
            <Bell className="h-4 w-4" /> 알림 켜기
          </Button>
        )}
        {perm === "denied" && <span className="text-xs text-text-muted">브라우저 알림이 차단됨 (사이트 설정에서 허용)</span>}
        <Button size="sm" variant="secondary" onClick={readAll} disabled={busy || !rows.length}>
          <CheckCheck className="h-4 w-4" /> 모두 읽음
        </Button>
      </div>
      {error && <p role="alert" className="mb-2 text-sm text-danger-700">{error}</p>}
      {!items ? (
        !error && <p className="text-sm text-text-muted">불러오는 중…</p>
      ) : !rows.length ? (
        <p className="rounded-lg border border-border bg-surface px-4 py-8 text-center text-sm text-text-muted">안 읽은 답이나 알림이 없어요</p>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
          {rows.map(({ chat: i, alert: a }) => a ? (
            <AlertRow key={`n${a.id}`} a={a} onOpen={() => readAlert(a.id)} />
          ) : i && (
            <li key={`c${i.id}`}>
              <a href={projectHref(i.project_id, "chat", i.id)} className="block px-4 py-3 transition-colors hover:bg-grey-50">
                <span className="flex items-center gap-2 text-sm">
                  <MessageSquare className="h-4 w-4 shrink-0 text-text-muted" />
                  <span className="shrink-0 text-xs font-medium text-text-muted">{i.project}</span>
                  <span className="line-clamp-2 min-w-0 flex-1 font-bold break-words text-text-main" title={i.title}>{i.title}</span>
                  <UnreadBadge n={i.unread} />
                  <span className="shrink-0 text-[11px] text-text-muted">{ago(i.at)}</span>
                </span>
                <span className="mt-1 block truncate pl-6 text-xs text-text-muted">
                  {i.failed && <span className="text-danger-700">중단됨 · </span>}{i.preview}
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AlertRow({ a, onOpen }: { a: InboxNotification; onOpen: () => void }) {
  const { label, icon: Icon } = KIND[a.kind] ?? KIND.review;
  const external = /^https?:/.test(a.href);
  return (
    <li className="flex items-start gap-2 pr-4 transition-colors hover:bg-grey-50">
      <a href={a.href} onClick={onOpen} onAuxClick={onOpen} {...(external ? { target: "_blank", rel: "noreferrer" } : {})} className="block min-w-0 flex-1 py-3 pl-4">
        <span className="flex items-center gap-2 text-sm">
          <Icon className="h-4 w-4 shrink-0 text-primary" />
          <span className="shrink-0 text-xs font-medium text-text-muted">{label}</span>
          <span className="line-clamp-2 min-w-0 flex-1 font-bold break-words text-text-main" title={a.title}>{a.title}</span>
          <span className="shrink-0 text-[11px] text-text-muted">{ago(a.ts)}</span>
        </span>
        <span className="mt-1 block truncate pl-6 text-xs text-text-muted" title={a.body}>{a.body}</span>
      </a>
      {a.kind === "review" && a.pr_url && a.pr_url !== a.href && (
        <a href={a.pr_url} target="_blank" rel="noreferrer" className="mt-3 shrink-0 text-[11px] text-text-muted underline hover:text-text-main">PR</a>
      )}
      {(a.kind === "review" || a.kind === "blocked") && a.ticket_url && (
        <a href={a.ticket_url} target="_blank" rel="noreferrer" className="mt-3 shrink-0 text-[11px] text-text-muted underline hover:text-text-main">티켓</a>
      )}
    </li>
  );
}
