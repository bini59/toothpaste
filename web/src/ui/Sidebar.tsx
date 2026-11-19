import { ChevronDown } from "lucide-react";
import { useState } from "react";
import { cn } from "./cn";

/** status: shown only as a tooltip and to screen readers (the icon carries it visually), so the label never changes length.
 * placeholder: an invisible, inert row that holds the place of an item still loading. */
export type SidebarItem = { label: string; icon: React.ComponentType<{ className?: string }>; href: string; active?: boolean; onClick?: () => void; status?: string; placeholder?: boolean };
const named = (it: SidebarItem) => (it.status ? `${it.label} · ${it.status}` : it.label);
/** a placeholder gets no href (not focusable) and aria-hidden; the callers add `invisible` */
const inert = (it: SidebarItem) => (it.placeholder ? { "aria-hidden": true } : { href: it.href, onClick: it.onClick });
/** a collapsible category of items */
export type SidebarGroup = { label: string; items: SidebarItem[] };
export type SidebarEntry = SidebarItem | SidebarGroup;

const isGroup = (e: SidebarEntry): e is SidebarGroup => "items" in e;
const slide = "transition-[width,transform] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]";

function NavLink({ item, small }: { item: SidebarItem; small?: boolean }) {
  const Icon = item.icon;
  return (
    <a {...inert(item)} aria-current={item.active ? "page" : undefined}
      className={cn("flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-sm", item.placeholder && "invisible",
        item.active ? "bg-primary-50 font-semibold text-primary-700" : "font-medium text-text-muted hover:bg-grey-50 hover:text-text-main")}>
      <Icon className={cn("shrink-0", small ? "h-4 w-4" : "h-5 w-5")} />
      <span className="truncate" title={named(item)}>
        {item.label}{item.status && <span className="sr-only"> · {item.status}</span>}
      </span>
    </a>
  );
}

function Group({ group }: { group: SidebarGroup }) {
  const [open, setOpen] = useState(true);
  return (
    <li>
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
        className="flex w-full cursor-pointer items-center justify-between rounded-md px-3 pb-1 pt-3 text-[11px] font-semibold uppercase tracking-wider text-text-muted transition-colors hover:text-text-main">
        <span className="truncate">{group.label}</span>
        <ChevronDown className={cn("h-3.5 w-3.5 shrink-0 transition-transform", !open && "-rotate-90")} />
      </button>
      {open && (
        <ul className="ml-4 flex flex-col gap-0.5 border-l border-border pl-2">
          {group.items.map((it) => <li key={it.label}><NavLink item={it} small /></li>)}
        </ul>
      )}
    </li>
  );
}

/**
 * Fixed sidebar on desktop (the logo button collapses it to an icon rail), overlay drawer on mobile.
 * Flat items and collapsible groups; the rail lists every item flat.
 */
export function Sidebar({ serviceName, version, logo, items, collapsed, onCollapsedChange, mobileOpen, onMobileClose, className, footer }: {
  serviceName: string; version: string; logo: React.ReactNode; items: SidebarEntry[];
  collapsed: boolean; onCollapsedChange: (collapsed: boolean) => void;
  mobileOpen: boolean; onMobileClose: () => void; className?: string; footer?: React.ReactNode;
}) {
  const flat = items.flatMap((e) => (isGroup(e) ? e.items : [e]));
  return (
    <>
      <div aria-hidden={!mobileOpen} onClick={onMobileClose}
        className={cn("fixed inset-0 z-50 bg-black/40 backdrop-blur-sm lg:hidden", slide, mobileOpen ? "opacity-100" : "pointer-events-none opacity-0")} />
      <aside className={cn("fixed inset-y-0 left-0 z-50 flex w-[260px] max-w-[85vw] flex-col border-r border-border bg-surface lg:max-w-none",
        // transform, not Tailwind's translate-x (the translate property): `slide` transitions transform
        collapsed ? "lg:w-[72px]" : "lg:w-[260px]", mobileOpen ? "transform-none" : "[transform:translateX(-100%)] lg:transform-none", slide, className)}>
        <div className="flex h-14 shrink-0 items-center gap-2 border-b border-border px-3">
          <button type="button" onClick={() => onCollapsedChange(!collapsed)} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-expanded={!collapsed}
            className={cn("shrink-0 cursor-pointer rounded-lg transition-opacity hover:opacity-70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-200", collapsed && "lg:mx-auto")}>
            {logo}
          </button>
          <div className={cn("flex min-w-0 flex-1 items-baseline gap-1.5", collapsed && "lg:hidden")}>
            <span className="truncate text-sm font-bold tracking-tight text-text-main">{serviceName}</span>
            <span className="shrink-0 text-[10px] font-semibold text-text-muted">v{version}</span>
          </div>
        </div>
        <nav className={cn("flex-1 overflow-y-auto px-2 py-2", collapsed && "lg:hidden")}>
          <ul className="flex flex-col gap-1">
            {items.map((e) => isGroup(e) ? <Group key={e.label} group={e} /> : <li key={e.label}><NavLink item={e} /></li>)}
          </ul>
        </nav>
        <nav className={cn("hidden flex-1 overflow-y-auto px-2 py-2", collapsed && "lg:block")}>
          <ul className="flex flex-col gap-1">
            {flat.map((it) => {
              const Icon = it.icon;
              return (
                <li key={it.label}>
                  <a {...inert(it)} title={named(it)} aria-label={named(it)} aria-current={it.active ? "page" : undefined}
                    className={cn("flex cursor-pointer justify-center rounded-lg py-2.5", it.placeholder && "invisible", it.active ? "bg-primary-50 text-primary-700" : "text-text-muted hover:bg-grey-50 hover:text-text-main")}>
                    <Icon className="h-5 w-5 shrink-0" />
                  </a>
                </li>
              );
            })}
          </ul>
        </nav>
        {footer}
      </aside>
    </>
  );
}
