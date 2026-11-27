import { useEffect, useRef, useState } from "react";
import { Bot, FileText } from "lucide-react";
import Markdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { md } from "../../components/markdown";
import { ago } from "../../lib";
import { cn } from "../../ui";
import type { Project } from "../../types";
import type { ChatAttachment, ChatMessage, ChatThread, Draft } from "./types";

/** Messages are Markdown (GFM; a single newline is a line break, as typed). Raw HTML is not rendered: react-markdown
 * shows it as text, and its default urlTransform drops javascript: links. */
function Body({ text }: { text: string }) {
  return (
    <div data-md className="min-w-0 break-words [&>:first-child]:mt-0 [&>:last-child]:mb-0">
      <Markdown remarkPlugins={[remarkGfm, remarkBreaks]} components={md}>{text}</Markdown>
    </div>
  );
}

function Attachments({ items }: { items: ChatAttachment[] }) {
  if (!items.length) return null;
  return (
    <div className="mb-1 flex flex-wrap items-start gap-2">
      {items.map((a) => a.image ? (
        <a key={a.url} href={a.url} target="_blank" rel="noreferrer" title={a.name}>
          <img src={a.url} alt={a.name} className="h-24 max-w-48 rounded-md border border-border object-cover" />
        </a>
      ) : (
        <a key={a.url} href={a.url} target="_blank" rel="noreferrer" className="flex max-w-full items-center gap-1 rounded-md border border-border bg-surface px-2 py-1 text-xs text-primary hover:underline">
          <FileText className="h-4 w-4 shrink-0" />
          <span className="wrap-anywhere">{a.name}</span>
          <span className="shrink-0 text-text-muted">({Math.ceil(a.size / 1024)}KB)</span>
        </a>
      ))}
    </div>
  );
}

function UserBubble({ text, attachments = [] }: { text: string; attachments?: ChatAttachment[] }) {
  return (
    <div className="flex justify-end">
      <div className="min-w-0 max-w-[85%] rounded-lg bg-primary-50 px-3 py-2 text-text-main"><Attachments items={attachments} /><Body text={text} /></div>
    </div>
  );
}

/** The pending bubble shows the attachment count until the saved files arrive. */
export function PendingMessage({ draft }: { draft: Draft }) {
  const { text, attachments } = draft;
  return <UserBubble text={attachments ? `${text}${text ? "\n" : ""}(첨부 ${attachments}개)` : text} />;
}

/** Time since the turn started, ticking every second (claude -p prints nothing until it is done). */
function Elapsed({ since }: { since: string }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const iv = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(iv);
  }, []);
  const s = Math.max(0, Math.floor((Date.now() - new Date(since).getTime()) / 1000));
  return <span className="tabular-nums">{s < 60 ? `${s}초` : `${Math.floor(s / 60)}분 ${s % 60}초`} 경과</span>;
}

function Message({ message: m, log }: { message: ChatMessage; log?: string }) {
  return m.role === "user" ? (
    <UserBubble text={m.text ?? ""} attachments={m.attachments} />
  ) : m.role === "compaction" ? (
    <div role="status" className="flex items-center gap-3 py-2 text-xs text-text-muted">
      <span aria-hidden="true" className="flex-1 border-t border-border" />
      {m.text ?? "대화를 압축하는 중이에요"}
      <span aria-hidden="true" className="flex-1 border-t border-border" />
    </div>
  ) : m.text === null ? (
    <div className="max-w-[85%] rounded-lg border border-border bg-surface px-3 py-2 text-text-muted">
      <Attachments items={m.attachments} />
      <p className="flex items-center gap-2"><Bot className="h-4 w-4 animate-pulse" /> 답을 쓰는 중… <Elapsed since={m.ts} /> · 다른 화면으로 가도 계속 실행돼요</p>
      {log && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs">실행 로그</summary>
          <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-grey-50 p-2 font-mono text-[11px] text-text-main">{log}</pre>
        </details>
      )}
    </div>
  ) : (
    <div className={cn("max-w-[85%] rounded-lg border bg-surface px-3 py-2 text-text-main", m.failed ? "border-danger-200" : "border-border")}>
      <Attachments items={m.attachments} />
      <Body text={m.text} />
      <p className="mt-1 text-[11px] text-text-muted">{m.agent ?? "agent"} · {ago(m.finished_at ?? m.ts)}{m.failed && <span className="text-danger-700"> · 중단됨</span>}</p>
    </div>
  );
}

export function Empty({ project }: { project: Project }) {
  return (
    <div className="space-y-1 p-2 text-text-muted">
      <p>{project.name}에 대해 물어보세요. 에이전트({project.agent})가 {project.repo ?? "기본 레포"} 체크아웃에서{project.repos.length > 1 ? ` 다른 레포 ${project.repos.length - 1}개와 함께` : ""} 읽고 답해요.</p>
      <p className="text-xs">코드 질문, 원인 조사, 티켓 초안 작성에 쓸 수 있어요. 체크아웃은 읽기만 하고, 빌드나 테스트는 /tmp에 복사해서 해요. Asana는 바꾸지 않고, GitHub 이슈는 요청할 때만 만들어요.</p>
    </div>
  );
}

export function Messages({ project, thread, sending }: { project: Project; thread: ChatThread; sending: Draft | null }) {
  const end = useRef<HTMLDivElement>(null);
  const count = thread.messages.length;
  const lastText = thread.messages.at(-1)?.text;
  useEffect(() => {
    // Keep a block body: Chromium can return a Promise from scrollIntoView (TP-71).
    end.current?.scrollIntoView({ block: "end" });
  }, [count, lastText, sending]);
  return (
    <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3 text-sm">
      {count === 0 && sending === null && <Empty project={project} />}
      {thread.messages.map((message) => <Message key={message.id} message={message} log={thread.busy?.log} />)}
      {sending !== null && !thread.busy && <PendingMessage draft={sending} />}
      <div ref={end} />
    </div>
  );
}
