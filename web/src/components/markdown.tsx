import type { Components } from "react-markdown";
import { cn } from "../ui";

/** Same-page anchors (footnotes): scroll inside this message instead of touching the hash route. */
function jump(e: React.MouseEvent<HTMLAnchorElement>, href: string) {
  e.preventDefault();
  e.currentTarget.closest("[data-md]")?.querySelector(`[id="${CSS.escape(decodeURIComponent(href.slice(1)))}"]`)?.scrollIntoView({ block: "nearest" });
}

// overrides keep the classes remark-gfm sets (sr-only footnote heading, contains-task-list) via cn(p.className, ...)
export const md: Components = {
  a: ({ node: _, href = "", ...p }) => href.startsWith("#")
    ? <a {...p} href={href} onClick={(e) => jump(e, href)} className={cn(p.className, "text-primary underline underline-offset-2 hover:text-primary-700")} />
    : <a {...p} href={href} target="_blank" rel="noreferrer" className={cn(p.className, "text-primary underline underline-offset-2 hover:text-primary-700")} />,
  // no <img>: it would load a remote URL by itself, and a steered agent reply could leak data in that URL; a link needs a click
  img: ({ src, alt }) => src ? <a href={String(src)} target="_blank" rel="noreferrer" className="text-primary underline underline-offset-2 hover:text-primary-700">{alt || String(src)}</a> : <>{alt}</>,
  p: ({ node: _, ...p }) => <p {...p} className={cn(p.className, "my-2")} />,
  h1: ({ node: _, ...p }) => <h1 {...p} className={cn("mt-3 mb-1 text-base font-bold", p.className)} />,
  // remark-gfm's hidden "Footnotes" heading; spelled out so Tailwind generates sr-only
  h2: ({ node: _, ...p }) => <h2 {...p} className={p.className?.includes("sr-only") ? "sr-only" : cn("mt-3 mb-1 text-base font-bold", p.className)} />,
  h3: ({ node: _, ...p }) => <h3 {...p} className={cn("mt-3 mb-1 font-bold", p.className)} />,
  h4: ({ node: _, ...p }) => <h4 {...p} className={cn("mt-2 mb-1 font-semibold", p.className)} />,
  h5: ({ node: _, ...p }) => <h5 {...p} className={cn("mt-2 mb-1 font-semibold", p.className)} />,
  h6: ({ node: _, ...p }) => <h6 {...p} className={cn("mt-2 mb-1 font-semibold text-text-muted", p.className)} />,
  ul: ({ node: _, ...p }) => <ul {...p} className={cn("my-2 space-y-0.5", p.className?.includes("contains-task-list") ? "pl-1" : "list-disc pl-5", p.className)} />,
  ol: ({ node: _, ...p }) => <ol {...p} className={cn("my-2 list-decimal space-y-0.5 pl-5", p.className)} />,
  blockquote: ({ node: _, ...p }) => <blockquote {...p} className={cn("my-2 border-l-2 border-border pl-3 text-text-muted", p.className)} />,
  hr: () => <hr className="my-3 border-border" />,
  code: ({ node: _, ...p }) => <code {...p} className={cn("rounded border border-border bg-grey-50 px-1 font-mono text-[0.85em]", p.className)} />,
  // the [&_code] resets beat the inline-code look above for fenced blocks
  pre: ({ node: _, ...p }) => <pre {...p} className={cn("my-2 overflow-x-auto rounded-md border border-border bg-grey-50 p-2 font-mono text-xs [&_code]:border-0 [&_code]:bg-transparent [&_code]:p-0 [&_code]:text-xs", p.className)} />,
  table: ({ node: _, ...p }) => <div className="my-2 overflow-x-auto"><table {...p} className={cn("border-collapse text-xs", p.className)} /></div>,
  // keep-all: Hangul wraps at spaces, not between every syllable, so a wide table scrolls instead of turning into a column of letters
  th: ({ node: _, ...p }) => <th {...p} className={cn("whitespace-nowrap border border-border bg-grey-50 px-2 py-1 text-left font-semibold", p.className)} />,
  td: ({ node: _, ...p }) => <td {...p} className={cn("border border-border px-2 py-1 align-top [word-break:keep-all]", p.className)} />,
};
