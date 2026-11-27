import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Card, cn } from "../ui";
import { getJson } from "../api";
import { md } from "../components/markdown";

/** 도움말 (#help, TP-150): docs/guide.md from GET /api/guide, the same text `tp guide` prints */
export function Help() {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => { getJson<{ text: string }>("/api/guide").then((d) => setText(d.text)).catch((e) => setErr(String(e))); }, []);
  return (
    <Card className={cn("mx-auto max-w-3xl p-4 text-sm text-text-main sm:p-6", text == null && "min-h-[70vh]")}>
      {err ? <p role="alert" className="text-danger-700">도움말을 불러오지 못했어요: {err}</p>
        : text == null ? <p className="text-text-muted">불러오는 중…</p>
        : <div data-md className="break-words [&>:first-child]:mt-0"><Markdown remarkPlugins={[remarkGfm]} components={md}>{text}</Markdown></div>}
    </Card>
  );
}
