import { ChevronRight, Menu } from "lucide-react";
import { cn } from "./cn";

export type BreadcrumbItem = { label: string; href?: string };

/** Sticky page header: mobile menu button, breadcrumb trail (last item is the current page), right-aligned actions. */
export function Header({ breadcrumbs, onMenuClick, actions }: { breadcrumbs: BreadcrumbItem[]; onMenuClick: () => void; actions?: React.ReactNode }) {
  return (
    <header className="sticky top-0 z-40 flex h-14 items-center gap-3 border-b border-border bg-surface/80 px-4 backdrop-blur-sm sm:px-6">
      <button type="button" onClick={onMenuClick} aria-label="Open navigation"
        className="-ml-1 cursor-pointer rounded-lg p-1.5 text-text-muted hover:bg-grey-50 hover:text-text-main lg:hidden">
        <Menu className="h-5 w-5" />
      </button>
      <nav aria-label="Breadcrumb" className="flex min-w-0 flex-1 items-center">
        <ol className="flex items-center gap-1.5 overflow-hidden">
          {breadcrumbs.map((b, i) => {
            const last = i === breadcrumbs.length - 1;
            return (
              <li key={b.label} className="flex items-center gap-1.5">
                {i > 0 && <ChevronRight className="h-3.5 w-3.5 shrink-0 text-text-muted" />}
                {last || !b.href ? (
                  <span aria-current={last ? "page" : undefined} className={cn("truncate text-sm", last ? "font-semibold text-text-main" : "text-text-muted")}>{b.label}</span>
                ) : (
                  <a href={b.href} className="truncate text-sm text-text-muted transition-colors hover:text-primary">{b.label}</a>
                )}
              </li>
            );
          })}
        </ol>
      </nav>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  );
}
