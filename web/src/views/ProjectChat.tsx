import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, FileText, MessageSquare, Paperclip, Pencil, Plus, Search, Square, Trash2, X } from "lucide-react";
import Markdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { Badge, Button, Input, cn } from "../ui";
import { getJson, postJson } from "../api";
import { ago } from "../lib";
import type { ChatAttachment, ChatConversation, ChatThread, Project } from "../types";
import { Select } from "../components/Select";
import { projectHref } from "./Projects";

/** Same-page anchors (footnotes): scroll inside this message instead of touching the hash route. */
function jump(e: React.MouseEvent<HTMLAnchorElement>, href: string) {
  e.preventDefault();
  e.currentTarget.closest("[data-md]")?.querySelector(`[id="${CSS.escape(decodeURIComponent(href.slice(1)))}"]`)?.scrollIntoView({ block: "nearest" });
}

// overrides keep the classes remark-gfm sets (sr-only footnote heading, contains-task-list) via cn(p.className, ...)
export const md: Components = {
  a: ({ node: _, href = "", ...p }) => href.startsWith("#")
    ? <a {...p} href={href} onClick={(e) => jump(e, href)} className={cn(p.className, "text-primary underline underline-offset-2 hover:text-primary-700")} />
    : <a {...p} href={href} target="_blank" rel="noreferrer" className={cn(p.className, "text-primary underline underline-offset-2 hover:text-primary-700")} />,
  // no <img>: it would load a remote URL by itself, and a steered agent reply could leak data in that URL; a link needs a click
  img: ({ src, alt }) => src ? <a href={String(src)} target="_blank" rel="noreferrer" className="text-primary underline underline-offset-2 hover:text-primary-700">{alt || String(src)}</a> : <>{alt}</>,
  p: ({ node: _, ...p }) => <p {...p} className={cn(p.className, "my-2")} />,
  h1: ({ node: _, ...p }) => <h1 {...p} className={cn("mt-3 mb-1 text-base font-bold", p.className)} />,
  // remark-gfm's hidden "Footnotes" heading; spelled out so Tailwind generates sr-only
  h2: ({ node: _, ...p }) => <h2 {...p} className={p.className?.includes("sr-only") ? "sr-only" : cn("mt-3 mb-1 text-base font-bold", p.className)} />,
  h3: ({ node: _, ...p }) => <h3 {...p} className={cn("mt-3 mb-1 font-bold", p.className)} />,
  h4: ({ node: _, ...p }) => <h4 {...p} className={cn("mt-2 mb-1 font-semibold", p.className)} />,
  h5: ({ node: _, ...p }) => <h5 {...p} className={cn("mt-2 mb-1 font-semibold", p.className)} />,
  h6: ({ node: _, ...p }) => <h6 {...p} className={cn("mt-2 mb-1 font-semibold text-text-muted", p.className)} />,
  ul: ({ node: _, ...p }) => <ul {...p} className={cn("my-2 space-y-0.5", p.className?.includes("contains-task-list") ? "pl-1" : "list-disc pl-5", p.className)} />,
  ol: ({ node: _, ...p }) => <ol {...p} className={cn("my-2 list-decimal space-y-0.5 pl-5", p.className)} />,
  blockquote: ({ node: _, ...p }) => <blockquote {...p} className={cn("my-2 border-l-2 border-border pl-3 text-text-muted", p.className)} />,
  hr: () => <hr className="my-3 border-border" />,
  code: ({ node: _, ...p }) => <code {...p} className={cn("rounded border border-border bg-grey-50 px-1 font-mono text-[0.85em]", p.className)} />,
  // the [&_code] resets beat the inline-code look above for fenced blocks
  pre: ({ node: _, ...p }) => <pre {...p} className={cn("my-2 overflow-x-auto rounded-md border border-border bg-grey-50 p-2 font-mono text-xs [&_code]:border-0 [&_code]:bg-transparent [&_code]:p-0 [&_code]:text-xs", p.className)} />,
  table: ({ node: _, ...p }) => <div className="my-2 overflow-x-auto"><table {...p} className={cn("border-collapse text-xs", p.className)} /></div>,
  // keep-all: Hangul wraps at spaces, not between every syllable, so a wide table scrolls instead of turning into a column of letters
  th: ({ node: _, ...p }) => <th {...p} className={cn("whitespace-nowrap border border-border bg-grey-50 px-2 py-1 text-left font-semibold", p.className)} />,
  td: ({ node: _, ...p }) => <td {...p} className={cn("border border-border px-2 py-1 align-top [word-break:keep-all]", p.className)} />,
};

/** Messages are Markdown (GFM; a single newline is a line break, as typed). Raw HTML is not rendered: react-markdown
 * shows it as text, and its default urlTransform drops javascript: links. */
function Body({ text }: { text: string }) {
  return (
    <div data-md className="min-w-0 break-words [&>:first-child]:mt-0 [&>:last-child]:mb-0">
      <Markdown remarkPlugins={[remarkGfm, remarkBreaks]} components={md}>{text}</Markdown>
    </div>
  );
}

/** Same limits as chat.py: per file, and per message (the server takes at most 64MB of base64 JSON). */
const MAX_FILE = 20 * 1024 * 1024;
const MAX_FILES = 10;
const MAX_TOTAL = 45 * 1024 * 1024;
export type Upload = { name: string; data: string };

const b64 = (f: File) => new Promise<string>((ok, fail) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result).split(",", 2)[1] ?? "");
  r.onerror = () => fail(r.error);
  r.readAsDataURL(f);
});

function Attachments({ items }: { items: ChatAttachment[] }) {
  if (!items.length) return null;
  return (
    <div className="mb-1 flex flex-wrap items-start gap-2">
      {items.map((a) => a.image ? (
        <a key={a.url} href={a.url} target="_blank" rel="noreferrer" title={a.name}>
          <img src={a.url} alt={a.name} className="h-24 max-w-48 rounded-md border border-border object-cover" />
        </a>
      ) : (
        <a key={a.url} href={a.url} target="_blank" rel="noreferrer" className="flex items-center gap-1 rounded-md border border-border bg-surface px-2 py-1 text-xs text-primary hover:underline">
          <FileText className="h-4 w-4" /> {a.name} <span className="text-text-muted">({Math.ceil(a.size / 1024)}KB)</span>
        </a>
      ))}
    </div>
  );
}

function Composer({ disabled, busy, onSend, placeholder }: { disabled: boolean; busy: boolean; onSend: (t: string, files: Upload[]) => Promise<boolean>; placeholder: string }) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<{ file: File; url: string | null }[]>([]);
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const [reading, setReading] = useState(false); // base64 read + send: one at a time, files frozen meanwhile
  const locked = disabled || busy || reading;
  const picker = useRef<HTMLInputElement>(null);
  const urls = useRef(files);
  urls.current = files;
  useEffect(() => () => urls.current.forEach((f) => f.url && URL.revokeObjectURL(f.url)), []);
  const add = (list: FileList | File[]) => {
    const picked = Array.from(list);
    if (!picked.length || locked) return;
    const big = picked.find((f) => f.size > MAX_FILE);
    if (big) return setError(`${big.name}: 파일이 너무 커요 (파일당 20MB까지)`);
    if (files.length + picked.length > MAX_FILES) return setError(`첨부는 한 번에 ${MAX_FILES}개까지예요`);
    setError("");
    // pasted screenshots are all "image.png"; the server keeps them apart
    setFiles((cur) => [...cur, ...picked.map((file) => ({ file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : null }))]);
  };
  const remove = (i: number) => !locked && setFiles((cur) => {
    const u = cur[i]?.url;
    if (u) URL.revokeObjectURL(u);
    return cur.filter((_, j) => j !== i);
  });
  const send = async () => {
    if ((!text.trim() && !files.length) || locked) return;
    if (files.reduce((n, f) => n + f.file.size, 0) > MAX_TOTAL) return setError("첨부가 너무 커요 (한 메시지에 45MB까지)");
    setReading(true);
    try {
      let up: Upload[];
      try {
        up = await Promise.all(files.map(async (f) => ({ name: f.file.name || "file", data: await b64(f.file) })));
      } catch (e) {
        return setError(`파일을 읽지 못했어요: ${(e as Error).message}`);
      }
      // text is cleared right away (the message shows in the thread while it is sent) and is back in the box if
      // sending failed; the files stay in the box, frozen, until the send went through
      const sent = text;
      setText("");
      if (await onSend(sent, up)) {
        files.forEach((f) => f.url && URL.revokeObjectURL(f.url));
        setFiles([]);
        setError("");
      } else {
        setText((cur) => (cur ? `${sent}\n${cur}` : sent));
      }
    } finally {
      setReading(false);
    }
  };
  return (
    <div
      className={cn("border-t border-border p-3", drag && "bg-primary-50")}
      onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDrag(true); } }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDrag(false); }}
      onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}
    >
      {error && <p role="alert" className="mb-2 text-sm text-danger-700">{error}</p>}
      {files.length > 0 && (
        <ul aria-label="첨부할 파일" className="mb-2 flex flex-wrap gap-2">
          {files.map((f, i) => (
            <li key={`${f.file.name}-${i}`} className="relative flex items-center gap-1 rounded-md border border-border bg-surface p-1 pr-7 text-xs text-text-main">
              {f.url ? <img src={f.url} alt={f.file.name} className="h-14 w-14 rounded object-cover" /> : <FileText className="h-4 w-4 text-text-muted" />}
              <span className="max-w-40 truncate">{f.file.name}</span>
              <button type="button" aria-label={`${f.file.name} 빼기`} disabled={locked} onClick={() => remove(i)}
                className="absolute right-1 top-1 rounded p-0.5 text-text-muted hover:bg-grey-50 hover:text-text-main">
                <X className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-end gap-2">
        <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) add(e.target.files); e.target.value = ""; }} />
        <Button variant="secondary" aria-label="파일 첨부" title="파일 첨부 (붙여넣기 · 끌어놓기도 돼요)" disabled={locked} onClick={() => picker.current?.click()}>
          <Paperclip className="h-4 w-4" />
        </Button>
        <textarea
          aria-label="메시지"
          rows={Math.min(8, Math.max(2, text.split("\n").length))}
          value={text}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => {
            // a screenshot comes as a file with no text; Office/iWork copies carry a PNG next to the text, keep the text
            if (e.clipboardData.files.length && !e.clipboardData.getData("text/plain")) {
              e.preventDefault();
              add(e.clipboardData.files);
            }
          }}
          onKeyDown={(e) => {
            // Enter sends, Shift+Enter is a new line; not while the IME is composing Hangul
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          className="min-h-10 flex-1 resize-none rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text-main outline-none transition-colors hover:border-grey-300 focus:border-primary focus:ring-2 focus:ring-primary-200 disabled:cursor-not-allowed disabled:opacity-60"
        />
        <Button onClick={send} disabled={locked || (!text.trim() && !files.length)} loading={busy || reading}>보내기</Button>
      </div>
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

/** What the thread shows of a message while it is sent (the files themselves appear after the reload). */
const pending = (text: string, files: Upload[]) => (files.length ? `${text}${text ? "\n" : ""}(첨부 ${files.length}개)` : text);

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

function useVisible() {
  const [v, setV] = useState(() => document.visibilityState === "visible");
  useEffect(() => {
    const on = () => setV(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return v;
}

function Thread({ project, id, onChanged }: { project: Project; id: string; onChanged: () => void }) {
  const [t, setT] = useState<ChatThread | null>(null);
  const [error, setError] = useState(""); // of an action (send, stop, ...): the reload right after must not clear it
  const [loadError, setLoadError] = useState("");
  const [sending, setSending] = useState<string | null>(null); // the message being sent, shown until the reload
  const [editing, setEditing] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const load = useCallback(() => getJson<ChatThread>(`/api/chat/messages?id=${encodeURIComponent(id)}`)
    .then((r) => { setT(r); setLoadError(""); }).catch((e) => setLoadError(String(e))), [id]);
  const busy = !!t?.busy;
  const compacting = busy && t?.messages.at(-1)?.role === "compaction";
  useEffect(() => {
    load();
    const iv = setInterval(load, busy ? 2500 : 10000);
    return () => clearInterval(iv);
  }, [load, busy]);
  const count = t?.messages.length ?? 0;
  const lastText = t?.messages[count - 1]?.text;
  // a block body: scrollIntoView returns a Promise in current Chromium, and an effect must not return it (React
  // would call it as the cleanup, throw, and unmount the whole app: TP-71)
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [count, lastText, sending]);
  // a finished turn changes the list (preview, running dot)
  const wasBusy = useRef(busy);
  useEffect(() => { if (wasBusy.current && !busy) onChanged(); wasBusy.current = busy; }, [busy, onChanged]);
  // TP-103: replies count as read only while this thread is on screen (a background tab leaves them unread)
  const visible = useVisible();
  const seen = t?.messages.reduce((n, m) => (m.role === "assistant" && m.text !== null ? Math.max(n, m.id) : n), 0) ?? 0;
  const marked = useRef(0);
  useEffect(() => {
    if (!visible || !seen || seen <= Math.max(t?.conversation.read_id ?? 0, marked.current)) return;
    marked.current = seen;
    postJson("/api/chat/read", { id, upto: seen }).then(onChanged, () => { marked.current = 0; });
  }, [visible, seen, t?.conversation.read_id, id, onChanged]);

  const act = async (path: string, body: object) => {
    try { await postJson(path, { id, ...body }); setError(""); return true; } catch (e) { setError((e as Error).message); return false; }
    finally { await load(); onChanged(); }
  };
  const send = async (text: string, files: Upload[]) => { setSending(pending(text, files)); try { return await act("/api/chat/send", { text, files }); } finally { setSending(null); } };
  const remove = async () => {
    if (!confirm("이 대화를 지울까요? 실행 중인 답도 멈춰요.")) return;
    if (await act("/api/chat/delete", {})) location.hash = projectHref(project.id);
  };

  if (!t) return <div className="p-4 text-sm text-text-muted">{loadError || "불러오는 중…"}</div>;
  const c = t.conversation;
  return (
    <>
      <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
        {editing !== null ? (
          <form className="flex min-w-0 flex-1 gap-2" onSubmit={async (e) => { e.preventDefault(); if (await act("/api/chat/rename", { title: editing })) setEditing(null); }}>
            <Input aria-label="대화 제목" className="h-8 min-w-0 flex-1 text-sm" value={editing} maxLength={80} autoFocus onChange={(e) => setEditing(e.target.value)} />
            <Button size="sm" type="submit">저장</Button>
            <Button size="sm" variant="secondary" type="button" onClick={() => setEditing(null)}>취소</Button>
          </form>
        ) : (
          <>
            <h2 className="min-w-0 flex-1 truncate text-sm font-bold text-text-main">{c.title}</h2>
            {c.tool && <Badge status="draft">{c.tool}</Badge>}
            <Button size="sm" variant="secondary" aria-label="제목 바꾸기" onClick={() => setEditing(c.title)}><Pencil className="h-4 w-4" /></Button>
            {busy && !compacting && <Button size="sm" variant="secondary" onClick={() => act("/api/chat/stop", {})}><Square className="h-4 w-4" /> 멈추기</Button>}
            <Button size="sm" variant="secondary" aria-label="대화 지우기" onClick={remove}><Trash2 className="h-4 w-4" /></Button>
          </>
        )}
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3 text-sm">
        {t.messages.length === 0 && sending === null && <Empty project={project} />}
        {t.messages.map((m) => m.role === "user" ? (
          <UserBubble key={m.id} text={m.text ?? ""} attachments={m.attachments} />
        ) : m.role === "compaction" ? (
          <div key={m.id} role="status" className="flex items-center gap-3 py-2 text-xs text-text-muted">
            <span aria-hidden="true" className="flex-1 border-t border-border" />
            {m.text ?? "대화를 압축하는 중이에요"}
            <span aria-hidden="true" className="flex-1 border-t border-border" />
          </div>
        ) : m.text === null ? (
          <div key={m.id} className="max-w-[85%] rounded-lg border border-border bg-surface px-3 py-2 text-text-muted">
            <Attachments items={m.attachments} />
            <p className="flex items-center gap-2"><Bot className="h-4 w-4 animate-pulse" /> 답을 쓰는 중… <Elapsed since={m.ts} /> · 다른 화면으로 가도 계속 실행돼요</p>
            {t.busy?.log && (
              <details className="mt-2">
                <summary className="cursor-pointer text-xs">실행 로그</summary>
                <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-grey-50 p-2 font-mono text-[11px] text-text-main">{t.busy.log}</pre>
              </details>
            )}
          </div>
        ) : (
          <div key={m.id} className={cn("max-w-[85%] rounded-lg border bg-surface px-3 py-2 text-text-main", m.failed ? "border-danger-200" : "border-border")}>
            <Attachments items={m.attachments} />
            <Body text={m.text} />
            <p className="mt-1 text-[11px] text-text-muted">{m.agent ?? "agent"} · {ago(m.finished_at ?? m.ts)}{m.failed && <span className="text-danger-700"> · 중단됨</span>}</p>
          </div>
        ))}
        {/* once the reload shows the running turn, the message is in t.messages */}
        {sending !== null && !busy && <UserBubble text={sending} />}
        <div ref={end} />
      </div>
      {(error || loadError) && <p role="alert" className="px-3 text-sm text-danger-700">{error || loadError}</p>}
      <Composer disabled={busy || !project.path_ok} busy={sending !== null} onSend={send}
        placeholder={compacting ? "대화를 압축하는 중이에요" : busy ? "답을 기다리는 중이에요" : "메시지 (Enter 보내기 · Shift+Enter 줄바꿈)"} />
    </>
  );
}

function Empty({ project }: { project: Project }) {
  return (
    <div className="space-y-1 p-2 text-text-muted">
      <p>{project.name}에 대해 물어보세요. 에이전트({project.agent})가 {project.repo ?? "기본 레포"} 체크아웃에서{project.repos.length > 1 ? ` 다른 레포 ${project.repos.length - 1}개와 함께` : ""} 읽고 답해요.</p>
      <p className="text-xs">코드 질문, 원인 조사, 티켓 초안 작성에 쓸 수 있어요. 체크아웃은 읽기만 하고, 빌드나 테스트는 /tmp에 복사해서 해요. Asana는 바꾸지 않고, GitHub 이슈는 요청할 때만 만들어요.</p>
    </div>
  );
}

/** A new conversation is created on its first message, so an empty one never piles up in the list. */
function NewThread({ project, onCreated }: { project: Project; onCreated: () => void }) {
  const [sending, setSending] = useState<string | null>(null);
  // 미분류: its repos are unrelated, so a conversation picks one (a real project chats in its default repo)
  const [repo, setRepo] = useState(project.repo ?? "");
  const pick = project.unsorted && project.repos.length > 1;
  const ok = project.repos.find((r) => r.name === repo)?.path_ok ?? project.path_ok;
  const [error, setError] = useState("");
  const send = async (text: string, files: Upload[]) => {
    setSending(pending(text, files));
    setError("");
    try {
      const { id } = await postJson<{ id: string }>("/api/chat/new", { project: project.id, repo: pick ? repo : undefined });
      try {
        await postJson("/api/chat/send", { id, text, files });
      } catch (e) {
        // a rejected first message (bad file name, too big) would leave an empty "새 대화" behind
        await postJson("/api/chat/delete", { id }).catch(() => {});
        throw e;
      }
      onCreated();
      location.hash = projectHref(project.id, "chat", id);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setSending(null);
    }
  };
  return (
    <>
      <div className="flex items-center gap-2 border-b border-border p-3">
        <h2 className="text-sm font-bold text-text-main">새 대화</h2>
        {pick && (
          <Select size="sm" value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="대화할 레포">
            {project.repos.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
          </Select>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-3 text-sm">{sending === null ? <Empty project={project} /> : <UserBubble text={sending} />}</div>
      {error && <p role="alert" className="px-3 text-sm text-danger-700">{error}</p>}
      <Composer disabled={!ok} busy={sending !== null} onSend={send} placeholder="메시지 (Enter 보내기 · Shift+Enter 줄바꿈)" />
    </>
  );
}

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

export function ProjectChat({ project, conv, onChanged }: { project: Project; conv: string; onChanged: () => void }) {
  const [list, setList] = useState<ChatConversation[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const load = useCallback(() => getJson<{ conversations: ChatConversation[] }>(`/api/chat/conversations?project=${encodeURIComponent(project.id)}`)
    .then((r) => { setList(r.conversations); setError(""); }).catch((e) => setError(String(e))), [project.id]);
  useEffect(() => {
    load();
    const iv = setInterval(load, 10000);
    return () => clearInterval(iv);
  }, [load]);
  const changed = useCallback(() => { load(); onChanged(); }, [load, onChanged]);
  // title and preview only, as the list API gives them (newest first)
  const q = query.trim().toLowerCase();
  const shown = list?.filter((c) => !q || `${c.title}\n${c.preview ?? ""}`.toLowerCase().includes(q)) ?? [];
  const groups = GROUPS.map((label, i) => ({ label, items: shown.filter((c) => group(c.updated_at) === i) })).filter((g) => g.items.length);
  return (
    // below lg: the list above 새 대화, or only the open conversation
    <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
      <aside className={cn("min-w-0 flex-col border-border max-lg:max-h-[45%] max-lg:border-b lg:w-[280px] lg:shrink-0 lg:border-r", conv ? "hidden lg:flex" : "flex")}>
        <div className="space-y-2 border-b border-border p-3">
          <a href={projectHref(project.id)} aria-current={!conv ? "page" : undefined}
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
                <a key={c.id} href={projectHref(project.id, "chat", c.id)} aria-current={c.id === conv ? "page" : undefined}
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
      <section className="flex min-h-0 min-w-0 flex-1 flex-col">
        {conv && <a href={projectHref(project.id)} className="border-b border-border px-3 py-2 text-xs text-primary lg:hidden">← 대화 목록</a>}
        {conv ? <Thread key={conv} project={project} id={conv} onChanged={changed} /> : <NewThread project={project} onCreated={changed} />}
      </section>
    </div>
  );
}
