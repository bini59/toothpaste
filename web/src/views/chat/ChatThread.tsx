import { useLayoutEffect, useRef, useState } from "react";
import { Pencil, Square, Trash2 } from "lucide-react";
import { Select } from "../../components/Select";
import type { Project } from "../../types";
import { Badge, Button, Input } from "../../ui";
import { projectHref } from "../Projects";
import { startConversation } from "./api";
import { Composer } from "./Composer";
import { Empty, Messages, PendingMessage } from "./Messages";
import type { Draft, Upload } from "./types";
import { useChatThread } from "./useChat";

function useChatNavigation() {
  const mounted = useRef(false);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  return (from: string, to: string) => {
    // A mutation can finish after the user opens a different conversation.
    if (mounted.current && location.hash === from) location.hash = to;
  };
}

export function ChatThread({ project, id, onChanged }: { project: Project; id: string; onChanged: () => void }) {
  const { thread, busy, compacting, loadError, error, sending, send, rename, stop, remove } = useChatThread(id, onChanged);
  const [editing, setEditing] = useState<string | null>(null);
  const navigate = useChatNavigation();
  const deleteConversation = async () => {
    if (!confirm("이 대화를 지울까요? 실행 중인 답도 멈춰요.")) return;
    const hash = location.hash;
    if (await remove()) navigate(hash, projectHref(project.id));
  };

  if (!thread) return <div className="p-4 text-sm text-text-muted">{loadError || "불러오는 중…"}</div>;
  const c = thread.conversation;
  return (
    <>
      <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
        {editing !== null ? (
          <form className="flex min-w-0 flex-1 gap-2" onSubmit={async (e) => { e.preventDefault(); if (await rename(editing)) setEditing(null); }}>
            <Input aria-label="대화 제목" className="h-8 min-w-0 flex-1 text-sm" value={editing} maxLength={80} autoFocus onChange={(e) => setEditing(e.target.value)} />
            <Button size="sm" type="submit">저장</Button>
            <Button size="sm" variant="secondary" type="button" onClick={() => setEditing(null)}>취소</Button>
          </form>
        ) : (
          <>
            <h2 className="min-w-0 flex-1 truncate text-sm font-bold text-text-main">{c.title}</h2>
            {c.tool && <Badge status="draft">{c.tool}</Badge>}
            <Button size="sm" variant="secondary" aria-label="제목 바꾸기" onClick={() => setEditing(c.title)}><Pencil className="h-4 w-4" /></Button>
            {busy && !compacting && <Button size="sm" variant="secondary" onClick={stop}><Square className="h-4 w-4" /> 멈추기</Button>}
            <Button size="sm" variant="secondary" aria-label="대화 지우기" onClick={deleteConversation}><Trash2 className="h-4 w-4" /></Button>
          </>
        )}
      </div>
      <Messages project={project} thread={thread} sending={sending} />
      {(error || loadError) && <p role="alert" className="px-3 text-sm text-danger-700">{error || loadError}</p>}
      <Composer disabled={busy || !project.path_ok} busy={sending !== null} onSend={send}
        placeholder={compacting ? "대화를 압축하는 중이에요" : busy ? "답을 기다리는 중이에요" : "메시지 (Enter 보내기 · Shift+Enter 줄바꿈)"} />
    </>
  );
}

/** A new conversation is created on its first message, so an empty one never piles up in the list. */
export function NewThread({ project, onCreated }: { project: Project; onCreated: () => void }) {
  const [sending, setSending] = useState<Draft | null>(null);
  const navigate = useChatNavigation();
  // 미분류 has unrelated repos, so new conversations can pick one.
  const [repo, setRepo] = useState(project.repo ?? "");
  const pick = project.unsorted && project.repos.length > 1;
  const ok = project.repos.find((r) => r.name === repo)?.path_ok ?? project.path_ok;
  const [error, setError] = useState("");
  const send = async (text: string, files: Upload[]) => {
    const hash = location.hash;
    setSending({ text, attachments: files.length });
    setError("");
    try {
      const id = await startConversation(project.id, pick ? repo : undefined, text, files);
      onCreated();
      navigate(hash, projectHref(project.id, "chat", id));
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
      <div className="min-h-0 flex-1 overflow-y-auto p-3 text-sm">{sending === null ? <Empty project={project} /> : <PendingMessage draft={sending} />}</div>
      {error && <p role="alert" className="px-3 text-sm text-danger-700">{error}</p>}
      <Composer disabled={!ok} busy={sending !== null} onSend={send} placeholder="메시지 (Enter 보내기 · Shift+Enter 줄바꿈)" />
    </>
  );
}
