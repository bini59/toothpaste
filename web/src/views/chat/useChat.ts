import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { deleteConversation, getConversations, getThread, markRead, renameConversation, sendMessage, stopConversation } from "./api";
import { pollChat } from "./pollChat";
import type { ChatThread, Draft, Upload } from "./types";

const idleDelay = () => 10_000;
const threadDelay = (thread: ChatThread | null) => thread?.busy ? 2_500 : idleDelay();

function useChatResource<T>(read: (signal: AbortSignal) => Promise<T>, nextDelay: (value: T | null) => number = idleDelay) {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState("");
  const poll = useRef<ReturnType<typeof pollChat<T>> | null>(null);
  useLayoutEffect(() => {
    const current = pollChat(read, (result) => {
      setValue(result);
      setError("");
    }, (e) => setError(String(e)), nextDelay);
    poll.current = current;
    void current.reload();
    return () => {
      current.dispose();
      poll.current = null;
    };
  }, [read, nextDelay]);
  const reload = useCallback(() => poll.current?.reload(), []);
  return { value, error, reload };
}

export function useChatConversations(projectId: string) {
  const read = useCallback((signal: AbortSignal) => getConversations(projectId, signal), [projectId]);
  return useChatResource(read);
}

function useReadReceipt(id: string, thread: ChatThread | null, onChanged: () => void) {
  const [visible, setVisible] = useState(() => document.visibilityState === "visible");
  const marked = useRef(0);
  useEffect(() => {
    const updateVisibility = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", updateVisibility);
    return () => document.removeEventListener("visibilitychange", updateVisibility);
  }, []);
  useEffect(() => {
    // TP-103: leave finished replies unread while the tab is in the background.
    if (!thread || !visible || document.visibilityState !== "visible") return;
    const seen = thread.messages.reduce((n, m) => m.role === "assistant" && m.text !== null ? Math.max(n, m.id) : n, 0);
    if (seen <= Math.max(thread.conversation.read_id ?? 0, marked.current)) return;
    marked.current = seen;
    void markRead(id, seen).then(onChanged, () => {
      // An older failure must not clear the marker for a newer receipt.
      if (marked.current === seen) marked.current = 0;
    });
  }, [id, onChanged, thread, visible]);
}

export function useChatThread(id: string, onChanged: () => void) {
  const read = useCallback((signal: AbortSignal) => getThread(id, signal), [id]);
  const { value: thread, error: loadError, reload } = useChatResource(read, threadDelay);
  const [error, setError] = useState("");
  const [sending, setSending] = useState<Draft | null>(null);
  const busy = !!thread?.busy;
  const compacting = busy && thread?.messages.at(-1)?.role === "compaction";
  const wasBusy = useRef(busy);
  useEffect(() => {
    if (wasBusy.current && !busy) onChanged();
    wasBusy.current = busy;
  }, [busy, onChanged]);
  useReadReceipt(id, thread, onChanged);

  async function act(action: () => Promise<unknown>) {
    try {
      await action();
      setError("");
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      await reload();
      onChanged();
    }
  }

  async function send(text: string, files: Upload[]) {
    setSending({ text, attachments: files.length });
    try {
      return await act(() => sendMessage(id, text, files));
    } finally {
      setSending(null);
    }
  }

  return {
    thread, busy, compacting, loadError, error, sending, send,
    rename: (title: string) => act(() => renameConversation(id, title)),
    stop: () => act(() => stopConversation(id)),
    remove: () => act(() => deleteConversation(id)),
  };
}
