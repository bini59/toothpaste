import { cn } from "../../ui";
import { same } from "./values";

/** [address key, label, the top-level setting keys the section edits (for its unsaved mark)] */
export type SectionDef = readonly [string, string, readonly string[]];
/** the section an address names, "" when it names none (then wide screens show the first, narrow ones the list) */
export const sectionOf = (defs: readonly SectionDef[], key: string) => (defs.some((x) => x[0] === key) ? key : "");
/**
 * The chat page's layout for settings (TP-125): the section list on the left, the one chosen section on the right.
 * Below lg: only the list, or only the chosen section with a link back to the list.
 */
export function Sectioned({ label, defs, cur, href, d, orig, children }: {
  label: string; defs: readonly SectionDef[]; cur: string; href: (key: string) => string; d: unknown; orig: unknown; children: React.ReactNode;
}) {
  const a = d as Record<string, unknown>, b = orig as Record<string, unknown>;
  return (
    <div className="lg:flex lg:items-start lg:gap-6">
      <nav aria-label={label} className={cn("space-y-0.5 lg:sticky lg:top-20 lg:w-52 lg:shrink-0", cur && "hidden lg:block")}>
        {defs.map(([key, name, keys], i) => {
          const dirty = keys.some((k) => !same(a[k], b[k]));
          return (
            <a key={key} href={href(key)} aria-current={key === cur ? "page" : undefined}
              className={cn("flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                key === cur ? "bg-primary-50 text-primary-700"
                  // no section in the address: wide screens show the first one
                  : !cur && i === 0 ? "text-text-main hover:bg-grey-50 lg:bg-primary-50 lg:text-primary-700" : "text-text-main hover:bg-grey-50")}>
              <span className="min-w-0 flex-1 truncate">{name}</span>
              {dirty && <span className="h-2 w-2 shrink-0 rounded-full bg-warning-500" role="img" aria-label="저장 안 한 변경" title="저장 안 한 변경" />}
            </a>
          );
        })}
      </nav>
      <div className={cn("min-w-0 flex-1", !cur && "hidden lg:block")}>
        {cur && <a href={href("")} className="mb-3 inline-block text-xs font-semibold text-primary-700 lg:hidden">← {label}</a>}
        {children}
      </div>
    </div>
  );
}
