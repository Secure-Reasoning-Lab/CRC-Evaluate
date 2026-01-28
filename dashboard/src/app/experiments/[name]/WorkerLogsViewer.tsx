'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';

interface WorkerLogInfo {
  name: string;
  size: number;
  modifiedAt: string;
}

interface WorkerLogsListResponse {
  logs: WorkerLogInfo[];
  workerLogsDir: string;
  error?: string;
}

interface WorkerLogContentResponse {
  name: string;
  content: string;
  path: string;
}

interface WorkerLogsViewerProps {
  experimentName: string;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function HighlightedWorkerLog({ content }: { content: string }) {
  const lines = content.split('\n');

  return (
    <pre
      style={{
        margin: 0,
        fontSize: '12px',
        fontFamily: 'monospace',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      {lines.map((line, idx) => {
        const isError =
          line.includes('| ERROR') ||
          line.includes('| CRITICAL') ||
          line.includes('| FATAL') ||
          line.includes('[ERROR]') ||
          line.includes('[CRITICAL]') ||
          line.includes('[FATAL]');
        const isWarning =
          line.includes('| WARNING') ||
          line.includes('| WARN') ||
          line.includes('[WARNING]') ||
          line.includes('[WARN]');
        const isDebug = line.includes('| DEBUG');

        if (isError) {
          return (
            <div
              key={idx}
              style={{
                backgroundColor: '#fecaca',
                color: '#991b1b',
                padding: '1px 4px',
              }}
            >
              {line}
            </div>
          );
        } else if (isWarning) {
          return (
            <div
              key={idx}
              style={{
                backgroundColor: '#fef08a',
                color: '#854d0e',
                padding: '1px 4px',
              }}
            >
              {line}
            </div>
          );
        } else if (isDebug) {
          return (
            <div
              key={idx}
              style={{
                color: '#6b7280',
                padding: '1px 4px',
              }}
            >
              {line}
            </div>
          );
        }
        return (
          <div key={idx} style={{ padding: '1px 4px' }}>
            {line}
          </div>
        );
      })}
    </pre>
  );
}

export function WorkerLogsViewer({ experimentName }: WorkerLogsViewerProps) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [logsList, setLogsList] = useState<WorkerLogsListResponse | null>(null);
  const [selectedLog, setSelectedLog] = useState<string | null>(null);
  const [logContent, setLogContent] = useState<WorkerLogContentResponse | null>(null);
  const [loadingContent, setLoadingContent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchLogsList = async () => {
    if (logsList) return;

    setLoading(true);
    setError(null);

    try {
      const response = await fetch(`/api/experiments/${experimentName}/worker-logs`);
      if (!response.ok) {
        throw new Error('Failed to fetch worker logs list');
      }
      const result: WorkerLogsListResponse = await response.json();
      setLogsList(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch worker logs');
    } finally {
      setLoading(false);
    }
  };

  const fetchLogContent = async (logName: string) => {
    setSelectedLog(logName);
    setLoadingContent(true);
    setLogContent(null);

    try {
      const response = await fetch(
        `/api/experiments/${experimentName}/worker-logs?log=${encodeURIComponent(logName)}`
      );
      if (!response.ok) {
        throw new Error('Failed to fetch log content');
      }
      const result: WorkerLogContentResponse = await response.json();
      setLogContent(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch log content');
    } finally {
      setLoadingContent(false);
    }
  };

  const handleOpenChange = (isOpen: boolean) => {
    setOpen(isOpen);
    if (isOpen) {
      fetchLogsList();
    }
  };

  const handleBack = () => {
    setSelectedLog(null);
    setLogContent(null);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          Worker Logs
        </Button>
      </DialogTrigger>
      <DialogContent
        className="max-w-6xl"
        style={{
          display: 'flex',
          flexDirection: 'column',
          height: '85vh',
          overflow: 'hidden',
        }}
      >
        <DialogHeader style={{ flexShrink: 0 }}>
          <DialogTitle>
            {selectedLog ? (
              <div className="flex items-center gap-2">
                <button
                  onClick={handleBack}
                  className="text-primary hover:underline"
                  style={{ cursor: 'pointer', background: 'none', border: 'none', padding: 0 }}
                >
                  ← Back
                </button>
                <span className="text-muted-foreground">/</span>
                <span>{selectedLog}</span>
              </div>
            ) : (
              `Worker Logs - ${experimentName}`
            )}
          </DialogTitle>
        </DialogHeader>

        <div
          style={{
            flex: 1,
            overflowY: 'auto',
            minHeight: 0,
          }}
        >
          {loading && (
            <div className="flex items-center justify-center py-8">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
            </div>
          )}

          {error && <div className="text-red-500 py-4">{error}</div>}

          {/* Log List View */}
          {!selectedLog && logsList && (
            <div className="space-y-4 pr-2">
              {/* Directory Info */}
              <div
                className="bg-muted rounded p-3 text-sm font-mono truncate"
                title={logsList.workerLogsDir}
              >
                {logsList.workerLogsDir}
              </div>

              {logsList.logs.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground">
                  No worker logs found.
                </div>
              ) : (
                <div className="space-y-2">
                  {logsList.logs.map((log) => (
                    <div
                      key={log.name}
                      className="border rounded-lg p-4 hover:bg-muted/50 cursor-pointer"
                      onClick={() => fetchLogContent(log.name)}
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-primary">{log.name}</span>
                        <span className="text-sm text-muted-foreground">
                          {formatBytes(log.size)}
                        </span>
                      </div>
                      <div className="text-xs text-muted-foreground mt-1">
                        Modified: {new Date(log.modifiedAt).toLocaleString()}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Log Content View */}
          {selectedLog && (
            <div className="pr-2">
              {loadingContent ? (
                <div className="flex items-center justify-center py-8">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
                </div>
              ) : logContent ? (
                <div className="space-y-2">
                  <div
                    className="text-xs text-muted-foreground font-mono truncate"
                    title={logContent.path}
                  >
                    Source: {logContent.path}
                  </div>
                  <div className="bg-muted rounded p-4">
                    <HighlightedWorkerLog content={logContent.content} />
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
