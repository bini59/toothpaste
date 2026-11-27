const REQUEST_TIMEOUT = 30_000;

export function pollChat<T>(
  read: (signal: AbortSignal) => Promise<T>,
  onValue: (value: T) => void,
  onError: (error: unknown) => void,
  nextDelay: (value: T | null) => number,
) {
  let delay = nextDelay(null);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let request: AbortController | undefined;
  let disposed = false;

  async function reload() {
    if (disposed) return;
    clearTimeout(timer);
    // An explicit refresh supersedes an older snapshot. Polls wait for the current request.
    request?.abort();
    const current = new AbortController();
    request = current;
    // A hung GET must not stop the next snapshot from loading.
    timer = setTimeout(reload, REQUEST_TIMEOUT);
    try {
      const value = await read(current.signal);
      if (current.signal.aborted) return;
      delay = nextDelay(value);
      onValue(value);
    } catch (error) {
      if (!current.signal.aborted) onError(error);
    } finally {
      if (!current.signal.aborted) {
        clearTimeout(timer);
        request = undefined;
        timer = setTimeout(reload, delay);
      }
    }
  }

  return {
    reload,
    dispose() {
      disposed = true;
      clearTimeout(timer);
      request?.abort();
    },
  };
}
