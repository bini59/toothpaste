import { useLayoutEffect, useRef, useState } from "react";
import { FileText, Paperclip, X } from "lucide-react";
import { Button, cn } from "../../ui";
import type { Upload } from "./types";

/** Same limits as chat.py: per file, and per message (the server takes at most 64MB of base64 JSON). */
const MAX_FILE = 20 * 1024 * 1024;
const MAX_FILES = 10;
const MAX_TOTAL = 45 * 1024 * 1024;

const b64 = (f: File) => new Promise<string>((ok, fail) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result).split(",", 2)[1] ?? "");
  r.onerror = () => fail(r.error);
  r.readAsDataURL(f);
});

type Attachment = { file: File; url: string | null };

function revokePreview({ url }: Attachment) {
  if (url) URL.revokeObjectURL(url);
}

export function Composer({ disabled, busy, onSend, placeholder }: { disabled: boolean; busy: boolean; onSend: (t: string, files: Upload[]) => Promise<boolean>; placeholder: string }) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const [reading, setReading] = useState(false); // base64 read + send: one at a time, files frozen meanwhile
  const locked = disabled || busy || reading;
  const picker = useRef<HTMLInputElement>(null);
  const mountedFiles = useRef<Attachment[] | null>(null);
  useLayoutEffect(() => {
    mountedFiles.current = [];
    return () => {
      mountedFiles.current?.forEach(revokePreview);
      mountedFiles.current = null;
    };
  }, []);
  const updateFiles = (next: Attachment[]) => {
    mountedFiles.current = next;
    setFiles(next);
  };
  const add = (list: FileList | File[]) => {
    const picked = Array.from(list);
    const current = mountedFiles.current;
    if (!current || !picked.length || locked) return;
    const big = picked.find((f) => f.size > MAX_FILE);
    if (big) return setError(`${big.name}: 파일이 너무 커요 (파일당 20MB까지)`);
    if (current.length + picked.length > MAX_FILES) return setError(`첨부는 한 번에 ${MAX_FILES}개까지예요`);
    setError("");
    // pasted screenshots are all "image.png"; the server keeps them apart
    const added = picked.map((file) => ({ file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : null }));
    updateFiles([...current, ...added]);
  };
  const remove = (i: number) => {
    const current = mountedFiles.current;
    if (locked || !current?.[i]) return;
    revokePreview(current[i]);
    updateFiles(current.filter((_, j) => j !== i));
  };
  const send = async () => {
    const batch = mountedFiles.current;
    if (!batch || (!text.trim() && !batch.length) || locked) return;
    if (batch.reduce((n, f) => n + f.file.size, 0) > MAX_TOTAL) return setError("첨부가 너무 커요 (한 메시지에 45MB까지)");
    setReading(true);
    setText("");
    let sent = false;
    try {
      let uploads: Upload[];
      try {
        uploads = await Promise.all(batch.map(async ({ file }) => ({ name: file.name || "file", data: await b64(file) })));
      } catch (e) {
        if (mountedFiles.current === batch) setError(`파일을 읽지 못했어요: ${(e as Error).message}`);
        return;
      }
      // The user may have switched conversations while the FileReaders were running.
      if (mountedFiles.current !== batch) return;
      sent = await onSend(text, uploads);
    } finally {
      if (mountedFiles.current === batch) {
        setReading(false);
        if (sent) {
          batch.forEach(revokePreview);
          updateFiles([]);
          setError("");
        } else if (text) {
          setText((cur) => cur ? `${text}\n${cur}` : text);
        }
      }
    }
  };
  return (
    <div
      className={cn("border-t border-border p-3", drag && "bg-primary-50")}
      onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDrag(true); } }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDrag(false); }}
      onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}
    >
      {error && <p role="alert" className="mb-2 text-sm text-danger-700">{error}</p>}
      {files.length > 0 && (
        <ul aria-label="첨부할 파일" className="mb-2 flex flex-wrap gap-2">
          {files.map((f, i) => (
            <li key={`${f.file.name}-${i}`} className="relative flex items-center gap-1 rounded-md border border-border bg-surface p-1 pr-7 text-xs text-text-main">
              {f.url ? <img src={f.url} alt={f.file.name} className="h-14 w-14 rounded object-cover" /> : <FileText className="h-4 w-4 text-text-muted" />}
              <span className="max-w-40 truncate">{f.file.name}</span>
              <button type="button" aria-label={`${f.file.name} 빼기`} disabled={locked} onClick={() => remove(i)}
                className="absolute right-1 top-1 rounded p-0.5 text-text-muted hover:bg-grey-50 hover:text-text-main">
                <X className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-end gap-2">
        <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) add(e.target.files); e.target.value = ""; }} />
        <Button variant="secondary" aria-label="파일 첨부" title="파일 첨부 (붙여넣기 · 끌어놓기도 돼요)" disabled={locked} onClick={() => picker.current?.click()}>
          <Paperclip className="h-4 w-4" />
        </Button>
        <textarea
          aria-label="메시지"
          rows={Math.min(8, Math.max(2, text.split("\n").length))}
          value={text}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => {
            // a screenshot comes as a file with no text; Office/iWork copies carry a PNG next to the text, keep the text
            if (e.clipboardData.files.length && !e.clipboardData.getData("text/plain")) {
              e.preventDefault();
              add(e.clipboardData.files);
            }
          }}
          onKeyDown={(e) => {
            // Enter sends, Shift+Enter is a new line; not while the IME is composing Hangul
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          className="min-h-10 flex-1 resize-none rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text-main outline-none transition-colors hover:border-grey-300 focus:border-primary focus:ring-2 focus:ring-primary-200 disabled:cursor-not-allowed disabled:opacity-60"
        />
        <Button onClick={send} disabled={locked || (!text.trim() && !files.length)} loading={busy || reading}>보내기</Button>
      </div>
    </div>
  );
}
