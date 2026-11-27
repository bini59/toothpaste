export interface ChatConversation {
  id: string;
  repo: string;
  project_id: string | null;
  title: string;
  tool: string | null;
  created_at: string;
  updated_at: string;
  running?: number;
  unread?: number;
  /** the last assistant message seen (chat_conversations.read_id) */
  read_id?: number | null;
  preview?: string;
  session?: string | null;
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant" | "compaction";
  text: string | null; // null = the turn is still running
  ts: string;
  failed: boolean;
  finished_at: string | null;
  agent: string | null;
  attachments: ChatAttachment[];
}
export interface ChatAttachment {
  name: string;
  size: number;
  type: string;
  image: boolean;
  url: string;
}
export interface ChatThread {
  conversation: ChatConversation;
  messages: ChatMessage[];
  busy: { since: string; log: string } | null;
}

export type Upload = { name: string; data: string };
export type Draft = { text: string; attachments: number };
