import { useCallback, useEffect, useRef, useState } from "react";
import { fetchState, getJson, post, postJson } from "../api";
import type { AgentUsage, InboxNotification, Project, State } from "../types";
import type { Tab } from "./navigation";
import { pollingError, startPollingRequest, usePolling } from "./usePolling";

export function useDashboardState(tab: Tab) {
  const [s, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [syncing, setSyncing] = useState(false);
  const read = useCallback(async (signal: AbortSignal) => {
    try {
      const next = await fetchState(signal);
      if (!signal.aborted) {
        setState(next);
        setError("");
      }
    } catch (e) {
      const message = pollingError(signal, e);
      if (message) setError(message);
    }
  }, []);
  // The office animates people between stages and polls faster. Every top-level tab visit reloads state.
  const load = usePolling(read, tab === "office" ? 15000 : 60000, { resetOn: tab });

  const act = async (path: string, body: unknown) => {
    try { await post(path, body); } catch (e) { alert((e as Error).message); }
    await load();
  };
  const refresh = async () => {
    setSyncing(true);
    try {
      await act("/api/refresh", {});
    } finally {
      setSyncing(false);
    }
  };
  return { s, error, syncing, load, act, refresh };
}

export function useAgentUsage() {
  const [usage, setUsage] = useState<AgentUsage>({});
  const read = useCallback(async (signal: AbortSignal) => {
    const next = await getJson<AgentUsage>("/api/agents/usage", signal).catch(() => ({}));
    if (!signal.aborted) setUsage(next);
  }, []);
  const loadUsage = usePolling(read, 60000, { refreshOnFocus: true });
  return { usage, loadUsage };
}

type ProjectSummary = { projects: Project[]; notifications_unread: number };

export function useDashboardProjects() {
  const [summary, setSummary] = useState<ProjectSummary | null>(null);
  const [projectsError, setProjectsError] = useState("");
  const read = useCallback(async (signal: AbortSignal) => {
    try {
      const next = await getJson<ProjectSummary>("/api/projects", signal);
      if (!signal.aborted) {
        setSummary(next);
        setProjectsError("");
      }
    } catch (e) {
      const message = pollingError(signal, e);
      if (message) setProjectsError(message);
    }
  }, []);
  const loadProjects = usePolling(read, 15000);
  useInboxNotifications(summary, loadProjects);
  return {
    projects: summary?.projects ?? null,
    nAlerts: summary?.notifications_unread || 0,
    projectsError,
    loadProjects,
  };
}

function useInboxNotifications(summary: ProjectSummary | null, onRead: () => void) {
  const baselined = useRef(false);
  const pending = useRef<ReturnType<typeof startPollingRequest> | undefined>(undefined);
  useEffect(() => () => {
    pending.current?.abort();
    pending.current = undefined;
  }, []);

  useEffect(() => {
    if (!summary) return;
    if (!summary.notifications_unread) {
      baselined.current = true;
      return;
    }
    // An alert can arrive while another is read, so check on every successful poll with unread alerts.
    if (pending.current) return;
    const baseline = !baselined.current;
    const request = startPollingRequest(async (signal) => {
      try {
        const { notifications } = await getJson<{ notifications: InboxNotification[] }>("/api/chat/inbox", signal);
        if (signal.aborted) return;
        notifyNew(notifications, baseline, onRead);
        baselined.current = true;
      } catch {
        // Try again on the next project poll.
      }
    });
    pending.current = request;
    void request.done.finally(() => {
      if (pending.current === request) pending.current = undefined;
    });
  }, [summary, onRead]);
}

/** An initial load advances the shared watermark without showing the existing alerts (TP-134). */
function notifyNew(notifications: InboxNotification[], baseline: boolean, onRead: () => void) {
  const seen = Number(localStorage.getItem("tp.notified") || 0);
  const fresh = notifications.filter((a) => a.id > seen).sort((a, b) => a.id - b.id);
  if (!fresh.length) return;
  localStorage.setItem("tp.notified", String(fresh[fresh.length - 1].id));
  if (baseline || typeof Notification === "undefined" || Notification.permission !== "granted") return;
  for (const a of fresh) {
    let note: Notification;
    try {
      note = new Notification(a.title, { body: a.body, icon: "/icon.svg", tag: `tp-${a.id}` });
    } catch {
      return; // Android Chrome needs a service worker for notifications.
    }
    note.onclick = () => {
      note.close();
      window.focus();
      if (/^https?:/.test(a.href)) window.open(a.href, "_blank", "noreferrer");
      else location.hash = a.href.replace(/^#/, "");
      postJson("/api/notifications/read", { id: a.id }).catch(() => {}).finally(onRead);
    };
  }
}
