'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';

interface TrialError {
  trialIndex: number;
  crs: string;
  benchmark: string;
  harness: string;
  logSource: string;
  errors: string[];
}

interface ExperimentErrorsResponse {
  errors: TrialError[];
  totalErrorCount: number;
  trialsWithErrors: number;
  totalTrials: number;
}

interface ErrorsViewerProps {
  experimentName: string;
}

function TrialErrorCard({
  trial,
  experimentName,
}: {
  trial: TrialError;
  experimentName: string;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="border rounded-lg overflow-hidden">
      {/* Trial Header - clickable to expand */}
      <div
        className="bg-muted px-4 py-2 cursor-pointer hover:bg-muted/80"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <span style={{ width: '20px', textAlign: 'center' }}>
              {expanded ? '▼' : '▶'}
            </span>
            <Link
              href={`/experiments/${experimentName}/trials/${trial.trialIndex}`}
              className="font-semibold text-primary hover:underline"
              onClick={(e) => e.stopPropagation()}
            >
              Trial #{trial.trialIndex}
            </Link>
            <span className="text-sm text-muted-foreground">
              {trial.crs} / {trial.benchmark} / {trial.harness}
            </span>
          </div>
          <span className="text-sm font-medium" style={{ color: '#991b1b' }}>
            {trial.errors.length} error{trial.errors.length !== 1 ? 's' : ''}
          </span>
        </div>
        <div
          className="mt-1 text-xs text-muted-foreground font-mono truncate"
          style={{ marginLeft: '36px' }}
          title={trial.logSource}
        >
          Source: {trial.logSource}
        </div>
      </div>

      {/* Error Lines - shown when expanded */}
      {expanded && (
        <div
          className="p-2 bg-background"
          style={{ maxHeight: '400px', overflowY: 'auto' }}
        >
          <pre className="text-xs font-mono whitespace-pre-wrap break-words">
            {trial.errors.map((errorLine, idx) => (
              <div
                key={idx}
                style={{
                  backgroundColor: '#fecaca',
                  color: '#991b1b',
                  padding: '2px 4px',
                  marginBottom: '2px',
                  borderRadius: '2px',
                }}
              >
                {errorLine}
              </div>
            ))}
          </pre>
        </div>
      )}
    </div>
  );
}

export function ErrorsViewer({ experimentName }: ErrorsViewerProps) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<ExperimentErrorsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchErrors = async () => {
    if (data) return; // Already loaded

    setLoading(true);
    setError(null);

    try {
      const response = await fetch(`/api/experiments/${experimentName}/errors`);
      if (!response.ok) {
        throw new Error('Failed to fetch errors');
      }
      const result: ExperimentErrorsResponse = await response.json();
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch errors');
    } finally {
      setLoading(false);
    }
  };

  const handleOpenChange = (isOpen: boolean) => {
    setOpen(isOpen);
    if (isOpen) {
      fetchErrors();
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          View All Errors
        </Button>
      </DialogTrigger>
      <DialogContent
        className="max-w-5xl"
        style={{
          display: 'flex',
          flexDirection: 'column',
          height: '85vh',
          overflow: 'hidden',
        }}
      >
        <DialogHeader style={{ flexShrink: 0 }}>
          <DialogTitle>CRS Errors - {experimentName}</DialogTitle>
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

          {error && (
            <div className="text-red-500 py-4">{error}</div>
          )}

          {data && (
            <div className="space-y-4 pr-2">
              {/* Summary */}
              <div className="bg-muted rounded p-3 text-sm sticky top-0 z-10">
                <span className="font-semibold">{data.totalErrorCount}</span> errors found in{' '}
                <span className="font-semibold">{data.trialsWithErrors}</span> of{' '}
                <span className="font-semibold">{data.totalTrials}</span> trials
              </div>

              {data.errors.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground">
                  No errors found in any trial logs.
                </div>
              ) : (
                <div className="space-y-2">
                  {data.errors.map((trial) => (
                    <TrialErrorCard
                      key={trial.trialIndex}
                      trial={trial}
                      experimentName={experimentName}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
