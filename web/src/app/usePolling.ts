import { useCallback, useEffect, useRef } from "react";

const REQUEST_TIMEOUT = 30_000;
const TIMEOUT_ERROR = new DOMException("요청 시간이 초과됐어요", "TimeoutError");
type Read = (signal: AbortSignal) => Promise<void>;

export function startPollingRequest(load: Read) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(TIMEOUT_ERROR), REQUEST_TIMEOUT);
  return {
    done: load(controller.signal).finally(() => clearTimeout(timeout)),
    abort() {
      clearTimeout(timeout);
      controller.abort();
    },
  };
}

export function pollingError(signal: AbortSignal, error: unknown) {
  if (!signal.aborted) return String(error);
  return signal.reason === TIMEOUT_ERROR ? TIMEOUT_ERROR.message : "";
}

type Request = ReturnType<typeof startPollingRequest>;

/** Explicit reloads supersede a pending read. Poll ticks leave it time to finish or time out. */
export function usePolling(
  load: Read,
  interval: number,
  { resetOn, refreshOnFocus = false }: { resetOn?: string; refreshOnFocus?: boolean } = {},
) {
  const start = useRef<(() => Request) | null>(null);
  const pending = useRef<Request | undefined>(undefined);
  const reload = useCallback(async () => {
    let request = start.current?.();
    while (request) {
      await request.done;
      // If a route change or another reload superseded this read, wait for the replacement snapshot.
      const latest = pending.current;
      request = latest === request ? undefined : latest;
    }
  }, []);

  useEffect(() => {
    const read = () => {
      pending.current?.abort();
      const request = startPollingRequest(load);
      pending.current = request;
      void request.done.finally(() => {
        if (pending.current === request) pending.current = undefined;
      });
      return request;
    };

    start.current = read;
    read();
    const timer = setInterval(() => { if (!pending.current) read(); }, interval);
    if (refreshOnFocus) window.addEventListener("focus", reload);
    return () => {
      clearInterval(timer);
      if (refreshOnFocus) window.removeEventListener("focus", reload);
      start.current = null;
      pending.current?.abort();
      pending.current = undefined;
    };
  }, [load, interval, resetOn, refreshOnFocus, reload]);

  return reload;
}
