import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api/client";

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (value: T) => void;
}

/** Loads a resource once, with a `reload` for after mutations. */
export function useResource<T>(load: () => Promise<T>, deps: unknown[] = []): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const value = await load();
      if (alive.current) {
        setData(value);
        setError(null);
      }
    } catch (caught) {
      if (alive.current) setError(errorMessage(caught));
    } finally {
      if (alive.current) setLoading(false);
    }
    // `load` is expected to be stable for the given deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { data, error, loading, reload, setData };
}

/** Re-runs `tick` on an interval, pausing while the tab is hidden. */
export function usePoll(tick: () => void, intervalMs: number) {
  useEffect(() => {
    const run = () => {
      if (!document.hidden) tick();
    };
    const handle = window.setInterval(run, intervalMs);
    return () => window.clearInterval(handle);
  }, [tick, intervalMs]);
}
