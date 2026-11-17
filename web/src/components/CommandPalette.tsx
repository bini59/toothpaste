import { useEffect, useId, useMemo, useRef, useState } from "react";
import { FolderGit2, Search, Star, type LucideIcon } from "lucide-react";
import { cn } from "../ui";
import { getJson } from "../api";
import type { Project } from "../types";
import { projectHref } from "../views/Projects";

export interface PaletteTab { key: string; label: string; group: string | null; icon: LucideIcon }
interface Item { id: string; label: string; hint: string; href: string; icon: LucideIcon }

/** starred projects, shared with the sidebar (TP-79): a JSON array of project ids (repo names before TP-91, App migrates them) */
const starred = (): string[] => {
  try {
    const v = JSON.parse(localStorage.getItem("tp.projects.starred") || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch { return []; }
};
const bare = (name: string) => name.replace(/^\d+[_-]/, "").toLowerCase();

/** Cmd/Ctrl+K: type a few letters of a project or screen, Enter to go there.
 * ponytail: case-insensitive substring match, a fuzzy matcher when names stop being findable this way */
export function CommandPalette({ open, onClose, tabs }: { open: boolean; onClose: () => void; tabs: PaletteTab[] }) {
  const ref = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const listId = useId();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState("");
  const downOnBackdrop = useRef(false);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (!open) { setQ(""); setSel(0); } // reset while closed, so the next open starts empty before any keystroke
    if (open && !d.open) {
      d.showModal(); // native modal: Esc closes, the page behind is inert (focus stays in the dialog)
      input.current?.focus();
      getJson<{ projects: Project[] }>("/api/projects").then((r) => { setProjects(r.projects); setError(""); })
        .catch((e) => setError(String(e)));
    } else if (!open && d.open) d.close();
  }, [open]);

  const items = useMemo((): Item[] => {
    const s = q.trim().toLowerCase();
    const stars = new Set(starred());
    const proj = (p: Project): Item => ({ id: `p:${p.id}`, label: p.name, hint: stars.has(p.id) ? "별표" : "프로젝트", href: projectHref(p.id), icon: stars.has(p.id) ? Star : FolderGit2 });
    const tab = (t: PaletteTab): Item => ({ id: `t:${t.key}`, label: t.label, hint: t.group ? `화면 · ${t.group}` : "화면", href: `#${t.key}`, icon: t.icon });
    if (!s) {
      // empty input: starred projects, then the 5 most recently chatted, the other projects, then every screen
      const recent = projects.filter((p) => !stars.has(p.id) && p.last_at)
        .sort((a, b) => (b.last_at ?? "").localeCompare(a.last_at ?? "")).slice(0, 5);
      const first = [...projects.filter((p) => stars.has(p.id)), ...recent];
      return [...first, ...projects.filter((p) => !first.includes(p))].map(proj).concat(tabs.map(tab));
    }
    // a project is found by its name, id or one of its repos
    const ps = projects.filter((p) => [p.name, p.id, ...p.repos.map((r) => r.name)].some((n) => n.toLowerCase().includes(s)))
      .sort((a, b) => Number(!bare(a.name).startsWith(s)) - Number(!bare(b.name).startsWith(s)));
    const ts = tabs.filter((t) => t.label.toLowerCase().includes(s) || t.key.includes(s) || t.group?.includes(s));
    return [...ps.map(proj), ...ts.map(tab)];
  }, [q, projects, tabs]);
  const cur = Math.min(sel, items.length - 1);
  useEffect(() => { if (open) document.getElementById(`${listId}-${cur}`)?.scrollIntoView({ block: "nearest" }); }, [open, cur, listId]);

  const go = (it: Item | undefined) => {
    if (!it) return;
    location.hash = it.href;
    onClose();
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return; // 한글 IME 조합 중 Enter/화살표는 조합 확정용
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = items.length;
      if (n) setSel((cur + (e.key === "ArrowDown" ? 1 : n - 1)) % n);
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(items[cur]);
    }
  };

  return (
    <dialog ref={ref} aria-label="빠른 이동"
      // the close event is async: ignore one that lands after a quick reopen (Cmd+K right after navigating)
      onClose={() => { if (!ref.current?.open) onClose(); }}
      // backdrop click closes; a drag that started inside (e.g. selecting the query) and ends outside does not
      onMouseDown={(e) => { downOnBackdrop.current = e.target === e.currentTarget; }}
      onClick={(e) => { if (downOnBackdrop.current && e.target === e.currentTarget) onClose(); }}
      className="mx-auto mt-[12vh] w-[min(560px,calc(100vw-2rem))] rounded-xl border border-border bg-surface p-0 text-text-main shadow-xl backdrop:bg-black/40">
      {open && (
        <>
          <div className="flex items-center gap-2 border-b border-border px-4">
            <Search className="h-4 w-4 shrink-0 text-text-muted" />
            <input ref={input} value={q} onChange={(e) => { setQ(e.target.value); setSel(0); }} onKeyDown={onKey}
              placeholder="프로젝트나 화면 이름…" aria-label="프로젝트나 화면 검색"
              role="combobox" aria-expanded={items.length > 0} aria-controls={listId} aria-autocomplete="list"
              aria-activedescendant={items[cur] ? `${listId}-${cur}` : undefined}
              className="h-12 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-text-muted" />
            <kbd className="rounded border border-border px-1.5 text-[11px] text-text-muted">Esc</kbd>
          </div>
          <ul id={listId} role="listbox" aria-label="이동할 곳" className="max-h-[50vh] overflow-y-auto p-2">
            {items.map((it, i) => (
              <li key={it.id} id={`${listId}-${i}`} role="option" aria-selected={i === cur}
                onMouseMove={() => i !== cur && setSel(i)} onClick={() => go(it)}
                className={cn("flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-sm",
                  i === cur ? "bg-primary-50 text-primary-700" : "text-text-main")}>
                <it.icon className="h-4 w-4 shrink-0 text-text-muted" />
                <span className="min-w-0 flex-1 truncate">{it.label}</span>
                <span className="shrink-0 text-xs text-text-muted">{it.hint}</span>
              </li>
            ))}
          </ul>
          {(error || !items.length) && (
            <p role="status" className={cn("px-3 pb-6 pt-4 text-center text-sm", error ? "text-danger-700" : "text-text-muted")}>
              {error ? `프로젝트를 불러오지 못했어요: ${error}` : "찾는 게 없어요"}
            </p>
          )}
        </>
      )}
    </dialog>
  );
}
