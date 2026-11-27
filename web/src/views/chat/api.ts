import { getJson, postJson } from "../../api";
import type { ChatConversation, ChatThread, Upload } from "./types";

export async function getConversations(projectId: string, signal: AbortSignal): Promise<ChatConversation[]> {
  const { conversations } = await getJson<{ conversations: ChatConversation[] }>(
    `/api/chat/conversations?project=${encodeURIComponent(projectId)}`, signal,
  );
  return conversations;
}

export function getThread(id: string, signal: AbortSignal) {
  return getJson<ChatThread>(`/api/chat/messages?id=${encodeURIComponent(id)}`, signal);
}

export const markRead = (id: string, upto: number) => postJson("/api/chat/read", { id, upto });
export const sendMessage = (id: string, text: string, files: Upload[]) => postJson("/api/chat/send", { id, text, files });
export const renameConversation = (id: string, title: string) => postJson("/api/chat/rename", { id, title });
export const stopConversation = (id: string) => postJson("/api/chat/stop", { id });
export const deleteConversation = (id: string) => postJson("/api/chat/delete", { id });

export async function startConversation(project: string, repo: string | undefined, text: string, files: Upload[]) {
  const { id } = await postJson<{ id: string }>("/api/chat/new", { project, repo });
  try {
    await sendMessage(id, text, files);
    return id;
  } catch (error) {
    // A rejected first message would otherwise leave an empty conversation.
    await deleteConversation(id).catch(() => {});
    throw error;
  }
}
