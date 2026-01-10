'use client';

import * as React from 'react';
import type { LLMLogsFile, LLMMessage, ExecutionInfo } from '@/lib/data/trials';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';

// Helper to extract text from message content (handles both string and array formats)
export function getMessageContent(content: unknown): string {
  if (typeof content === 'string') {
    return content;
  }
  if (Array.isArray(content)) {
    return content
      .map((block) => {
        if (typeof block === 'string') return block;
        if (block && typeof block === 'object' && 'text' in block) {
          return (block as { text: string }).text;
        }
        return '';
      })
      .join('\n');
  }
  if (content && typeof content === 'object' && 'text' in content) {
    return (content as { text: string }).text;
  }
  return String(content || '');
}

// CRS Logs Modal Component
export function CRSLogsModal({
  open,
  onOpenChange,
  logs,
  loading,
  error,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  logs: string | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[95vw] w-[1400px] max-h-[90vh]">
        <DialogHeader>
          <DialogTitle>CRS Output Logs</DialogTitle>
          <DialogDescription>
            Full CRS execution output from crs-output.log
          </DialogDescription>
        </DialogHeader>
        <div className="overflow-auto max-h-[calc(90vh-120px)]">
          {loading ? (
            <p className="text-center text-muted-foreground py-8">
              Loading CRS logs...
            </p>
          ) : error ? (
            <p className="text-center text-muted-foreground py-8">{error}</p>
          ) : logs ? (
            <pre className="bg-muted rounded p-4 text-sm font-mono whitespace-pre-wrap break-words">
              {logs}
            </pre>
          ) : (
            <p className="text-center text-muted-foreground py-8">
              No logs available
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// LLM Logs Modal Component
export function LLMLogsModal({
  open,
  onOpenChange,
  logs,
  loading,
  error,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  logs: LLMLogsFile | null;
  loading: boolean;
  error: string | null;
}) {
  const getConversationFromLatestLog = (): LLMMessage[] => {
    if (!logs || logs.logs.length === 0) return [];

    let latestLog = null;
    let maxMessages = 0;

    for (const log of logs.logs) {
      const messages = log.proxy_server_request?.messages || [];
      if (messages.length > maxMessages) {
        maxMessages = messages.length;
        latestLog = log;
      }
    }

    return latestLog?.proxy_server_request?.messages || [];
  };

  const messages = getConversationFromLatestLog();

  const getLatestResponse = (): string | null => {
    if (!logs || logs.logs.length === 0) return null;
    const lastLog = logs.logs[logs.logs.length - 1];
    if (lastLog?.response?.choices?.[0]?.message?.content) {
      return getMessageContent(lastLog.response.choices[0].message.content);
    }
    return null;
  };

  const latestResponse = getLatestResponse();
  const totalSpend = logs?.logs.reduce((sum, log) => sum + log.spend, 0) || 0;
  const totalTokens =
    logs?.logs.reduce((sum, log) => sum + log.total_tokens, 0) || 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[95vw] w-[1400px] max-h-[90vh]">
        <DialogHeader>
          <DialogTitle>LLM Conversation Logs</DialogTitle>
          <DialogDescription>
            {logs
              ? `${logs.total_requests} requests | ${totalTokens.toLocaleString()} tokens | $${totalSpend.toFixed(4)} total`
              : 'Full conversation history from llm-logs.json'}
          </DialogDescription>
        </DialogHeader>
        <div className="overflow-auto max-h-[calc(90vh-120px)]">
          {loading ? (
            <p className="text-center text-muted-foreground py-8">
              Loading LLM logs...
            </p>
          ) : error ? (
            <p className="text-center text-muted-foreground py-8">{error}</p>
          ) : messages.length > 0 ? (
            <div className="space-y-4">
              {messages.map((msg, idx) => (
                <div
                  key={idx}
                  className={`rounded p-4 ${
                    msg.role === 'system'
                      ? 'bg-yellow-50 dark:bg-yellow-950 border-l-4 border-yellow-500'
                      : msg.role === 'user'
                        ? 'bg-blue-50 dark:bg-blue-950 border-l-4 border-blue-500'
                        : 'bg-green-50 dark:bg-green-950 border-l-4 border-green-500'
                  }`}
                >
                  <div className="text-xs font-semibold mb-2 text-muted-foreground uppercase flex items-center gap-2">
                    <Badge
                      variant="outline"
                      className={
                        msg.role === 'system'
                          ? 'border-yellow-500 text-yellow-700 dark:text-yellow-300'
                          : msg.role === 'user'
                            ? 'border-blue-500 text-blue-700 dark:text-blue-300'
                            : 'border-green-500 text-green-700 dark:text-green-300'
                      }
                    >
                      {msg.role}
                    </Badge>
                    <span className="text-muted-foreground">
                      Message {idx + 1}
                    </span>
                  </div>
                  <pre className="text-sm whitespace-pre-wrap break-words font-mono">
                    {getMessageContent(msg.content)}
                  </pre>
                </div>
              ))}
              {latestResponse && (
                <div className="rounded p-4 bg-green-50 dark:bg-green-950 border-l-4 border-green-500">
                  <div className="text-xs font-semibold mb-2 text-muted-foreground uppercase flex items-center gap-2">
                    <Badge
                      variant="outline"
                      className="border-green-500 text-green-700 dark:text-green-300"
                    >
                      assistant
                    </Badge>
                    <span className="text-muted-foreground">
                      Latest Response
                    </span>
                  </div>
                  <pre className="text-sm whitespace-pre-wrap break-words font-mono">
                    {latestResponse}
                  </pre>
                </div>
              )}
            </div>
          ) : logs && logs.logs.length === 0 ? (
            <p className="text-center text-muted-foreground py-8">
              No LLM requests logged
            </p>
          ) : (
            <p className="text-center text-muted-foreground py-8">
              No logs available
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// Execution Info Modal Component for Bug Finding
export function BugFindingExecutionInfoModal({
  open,
  onOpenChange,
  info,
  loading,
  error,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  info: ExecutionInfo | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[95vw] w-[1000px] max-h-[90vh]">
        <DialogHeader>
          <DialogTitle>Execution Information</DialogTitle>
          <DialogDescription>
            Trial execution details from execution.json
          </DialogDescription>
        </DialogHeader>
        <div className="overflow-auto max-h-[calc(90vh-120px)]">
          {loading ? (
            <p className="text-center text-muted-foreground py-8">
              Loading execution info...
            </p>
          ) : error ? (
            <p className="text-center text-muted-foreground py-8">{error}</p>
          ) : info ? (
            <div className="space-y-6">
              {/* Execution Status */}
              <div className="bg-muted rounded p-4">
                <h3 className="font-semibold mb-3">Execution Status</h3>
                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <span className="text-sm text-muted-foreground">Status</span>
                    <p className="font-medium">
                      <Badge
                        variant={info.execution.success ? 'default' : 'destructive'}
                      >
                        {info.execution.success ? 'Success' : 'Failed'}
                      </Badge>
                    </p>
                  </div>
                  <div>
                    <span className="text-sm text-muted-foreground">Duration</span>
                    <p className="font-medium">
                      {(info.execution.duration_seconds / 60).toFixed(2)} min
                    </p>
                  </div>
                  <div>
                    <span className="text-sm text-muted-foreground">
                      Return Code
                    </span>
                    <p className="font-medium">{info.execution.returncode}</p>
                  </div>
                </div>
              </div>

              {/* CRS Config */}
              <div className="bg-muted rounded p-4">
                <h3 className="font-semibold mb-3">CRS Configuration</h3>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div>
                    <span className="text-muted-foreground">Mode:</span>{' '}
                    <span className="font-medium">{info.crs_config.mode}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Build Timeout:</span>{' '}
                    <span className="font-medium">
                      {info.crs_config.build_timeout}s
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Run Timeout:</span>{' '}
                    <span className="font-medium">
                      {info.crs_config.run_timeout}s
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Image Prefix:</span>{' '}
                    <span className="font-medium">
                      {info.crs_config.project_image_prefix}
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Hints:</span>{' '}
                    <span className="font-medium">
                      {info.crs_config.hints_enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  </div>
                </div>
              </div>

              {/* Command */}
              {info.command && (
                <div className="bg-muted rounded p-4">
                  <h3 className="font-semibold mb-3">Command</h3>
                  <pre className="text-sm font-mono whitespace-pre-wrap break-all">
                    {Array.isArray(info.command)
                      ? info.command.join(' ')
                      : info.command}
                  </pre>
                </div>
              )}

              {/* Timestamp */}
              <div className="text-sm text-muted-foreground">
                Executed at: {new Date(info.timestamp).toLocaleString()}
              </div>
            </div>
          ) : (
            <p className="text-center text-muted-foreground py-8">
              No execution info available
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// Execution Info Modal Component for Bug Fixing
export function BugFixingExecutionInfoModal({
  open,
  onOpenChange,
  info,
  loading,
  error,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  info: ExecutionInfo | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[95vw] w-[1000px] max-h-[90vh]">
        <DialogHeader>
          <DialogTitle>Execution Information</DialogTitle>
          <DialogDescription>
            Trial execution details from execution.json
          </DialogDescription>
        </DialogHeader>
        <div className="overflow-auto max-h-[calc(90vh-120px)]">
          {loading ? (
            <p className="text-center text-muted-foreground py-8">
              Loading execution info...
            </p>
          ) : error ? (
            <p className="text-center text-muted-foreground py-8">{error}</p>
          ) : info ? (
            <div className="space-y-6">
              {/* Execution Status */}
              <div className="bg-muted rounded p-4">
                <h3 className="font-semibold mb-3">Execution Status</h3>
                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <span className="text-sm text-muted-foreground">Status</span>
                    <p className="font-medium">
                      <Badge
                        variant={info.execution.success ? 'default' : 'destructive'}
                      >
                        {info.execution.success ? 'Success' : 'Failed'}
                      </Badge>
                    </p>
                  </div>
                  <div>
                    <span className="text-sm text-muted-foreground">Duration</span>
                    <p className="font-medium">
                      {(info.execution.duration_seconds / 60).toFixed(2)} min
                    </p>
                  </div>
                  <div>
                    <span className="text-sm text-muted-foreground">
                      Return Code
                    </span>
                    <p className="font-medium">{info.execution.returncode}</p>
                  </div>
                </div>
              </div>

              {/* CRS Config */}
              <div className="bg-muted rounded p-4">
                <h3 className="font-semibold mb-3">CRS Configuration</h3>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div>
                    <span className="text-muted-foreground">Mode:</span>{' '}
                    <span className="font-medium">{info.crs_config.mode}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Build Timeout:</span>{' '}
                    <span className="font-medium">
                      {info.crs_config.build_timeout}s
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Run Timeout:</span>{' '}
                    <span className="font-medium">
                      {info.crs_config.run_timeout}s
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Image Prefix:</span>{' '}
                    <span className="font-medium">
                      {info.crs_config.project_image_prefix}
                    </span>
                  </div>
                  <div>
                    <span className="text-muted-foreground">Hints:</span>{' '}
                    <span className="font-medium">
                      {info.crs_config.hints_enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  </div>
                </div>
              </div>

              {/* POVs (only shown for bug fixing) */}
              {info.povs && (
                <div className="bg-muted rounded p-4">
                  <h3 className="font-semibold mb-3">Input POVs</h3>
                  <div className="grid grid-cols-2 gap-4 text-sm">
                    <div>
                      <span className="text-muted-foreground">Provided:</span>{' '}
                      <span className="font-medium">
                        {info.povs.provided ? 'Yes' : 'No'}
                      </span>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Count:</span>{' '}
                      <span className="font-medium">{info.povs.count}</span>
                    </div>
                  </div>
                </div>
              )}

              {/* Command */}
              {info.command && (
                <div className="bg-muted rounded p-4">
                  <h3 className="font-semibold mb-3">Command</h3>
                  <pre className="text-sm font-mono whitespace-pre-wrap break-all">
                    {Array.isArray(info.command)
                      ? info.command.join(' ')
                      : info.command}
                  </pre>
                </div>
              )}

              {/* Timestamp */}
              <div className="text-sm text-muted-foreground">
                Executed at: {new Date(info.timestamp).toLocaleString()}
              </div>
            </div>
          ) : (
            <p className="text-center text-muted-foreground py-8">
              No execution info available
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// Format time helper
export function formatTime(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}
