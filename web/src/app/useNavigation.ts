import { useEffect, useState } from "react";
import { post } from "../api";
import type { Project } from "../types";
import { parseHash, routeReplacement } from "./navigation";

export function useHashNavigation(projects: Project[] | null, onNavigate: () => void) {
  const [route, setRoute] = useState(() => parseHash(location.hash));

  useEffect(() => {
    let last = parseHash(location.hash);
    setRoute(last);
    // Record visits two levels deep (projects/<id>, settings/repos) for TP-136.
    const recordView = (tab: string) => void post("/api/view", { tab }).catch(() => {});
    recordView(last.view);
    const onHashChange = () => {
      const next = parseHash(location.hash);
      if (next.view !== last.view) recordView(next.view);
      setRoute(next);
      onNavigate();
      if (!(next.scrollScope === last.scrollScope && last.scrollScope.endsWith("/settings"))) {
        window.scrollTo(0, 0);
      }
      last = next;
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [onNavigate]);

  const replacement = routeReplacement(route, projects);
  useEffect(() => {
    if (replacement && location.hash === route.hash) location.replace(replacement);
  }, [replacement, route.hash]);

  return replacement ? parseHash(replacement) : route;
}

export function useCommandPalette() {
  const [open, setOpen] = useState(false);
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
  useEffect(() => {
    // Window-level so it works in the chat composer. On Mac, Ctrl+K remains a textarea line-kill.
    const onKeyDown = (e: KeyboardEvent) => {
      // With the Korean input source e.key can be "ㅏ" for KeyK.
      if ((isMac ? e.metaKey : e.ctrlKey) && !e.altKey && !e.shiftKey && (e.code === "KeyK" || e.key.toLowerCase() === "k")) {
        e.preventDefault();
        if (!e.repeat) setOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isMac]);

  return { open, setOpen, isMac };
}
