import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./client";

export interface QueryState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Load a resource and keep it fresh.
 *
 * `onFailure` receives every error so an expired session can clear the shell rather than
 * leaving a signed-in page that silently fails.
 */
export function useApiQuery<T>(
  path: string,
  options: { pollMs?: number; onFailure?: (error: unknown) => void } = {},
): QueryState<T> {
  const { pollMs, onFailure } = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const failureRef = useRef(onFailure);
  failureRef.current = onFailure;

  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function load() {
      try {
        const result = await api.get<T>(path);
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      } catch (failure) {
        failureRef.current?.(failure);
        if (!cancelled) {
          setError(failure instanceof Error ? failure.message : String(failure));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
          if (pollMs !== undefined) {
            timer = setTimeout(load, pollMs);
          }
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    };
  }, [path, pollMs, tick]);

  return { data, error, loading, reload };
}
