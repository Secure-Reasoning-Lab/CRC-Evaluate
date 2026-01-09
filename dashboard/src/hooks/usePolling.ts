'use client';

import { useState, useEffect, useCallback } from 'react';

interface UsePollingOptions<T> {
  fetcher: () => Promise<T>;
  interval?: number;
  enabled?: boolean;
  onSuccess?: (data: T) => void;
  onError?: (error: Error) => void;
}

interface UsePollingResult<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
  refresh: () => Promise<void>;
}

export function usePolling<T>({
  fetcher,
  interval = 30000, // Default 30 seconds
  enabled = true,
  onSuccess,
  onError,
}: UsePollingOptions<T>): UsePollingResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await fetcher();
      setData(result);
      setError(null);
      onSuccess?.(result);
    } catch (err) {
      const error = err instanceof Error ? err : new Error('Unknown error');
      setError(error);
      onError?.(error);
    } finally {
      setLoading(false);
    }
  }, [fetcher, onSuccess, onError]);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    // Initial fetch
    refresh();

    // Set up polling interval
    const intervalId = setInterval(refresh, interval);

    return () => {
      clearInterval(intervalId);
    };
  }, [enabled, interval, refresh]);

  return { data, loading, error, refresh };
}

// Hook for checking active experiments
export function useActiveExperiments() {
  return usePolling<{ active_experiments: string[]; timestamp: string }>({
    fetcher: async () => {
      const res = await fetch('/api/status');
      if (!res.ok) {
        throw new Error('Failed to fetch status');
      }
      return res.json();
    },
    interval: 30000, // Check every 30 seconds
  });
}
