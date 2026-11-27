import { useCallback } from "react";
import type { Project } from "../types";
import { ChatThread, NewThread } from "./chat/ChatThread";
import { ConversationList } from "./chat/ConversationList";
import { useChatConversations } from "./chat/useChat";
import { projectHref } from "./Projects";

export function ProjectChat({ project, conv, onChanged }: { project: Project; conv: string; onChanged: () => void }) {
  const { value: list, error, reload } = useChatConversations(project.id);
  const changed = useCallback(() => { void reload(); onChanged(); }, [reload, onChanged]);
  return (
    // below lg: the list above 새 대화, or only the open conversation
    <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
      <ConversationList projectId={project.id} conv={conv} list={list} error={error} />
      <section className="flex min-h-0 min-w-0 flex-1 flex-col">
        {conv && <a href={projectHref(project.id)} className="border-b border-border px-3 py-2 text-xs text-primary lg:hidden">← 대화 목록</a>}
        {conv ? <ChatThread key={conv} project={project} id={conv} onChanged={changed} /> : <NewThread project={project} onCreated={changed} />}
      </section>
    </div>
  );
}
