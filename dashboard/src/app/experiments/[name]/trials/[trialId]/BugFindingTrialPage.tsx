'use client';

import Link from 'next/link';
import { useState, useCallback } from 'react';
import type {
  TrialReport,
  SnapshotEntry,
  LLMLogsFile,
  ExecutionInfo,
} from '@/lib/data/trials';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { DiscoveryTimeline } from '@/components/charts/DiscoveryTimeline';
import { CostOverTime } from '@/components/charts/CostOverTime';
import { CostBreakdown } from '@/components/charts/CostBreakdown';
import { SnapshotTimeline } from '@/components/charts/SnapshotTimeline';
import {
  CRSLogsModal,
  LLMLogsModal,
  BugFindingExecutionInfoModal,
  formatTime,
} from './components';

interface BugFindingTrialPageProps {
  experimentName: string;
  report: TrialReport;
}

interface SnapshotData {
  cycle: number;
  elapsed_time: number;
  pov_count: number;
  patch_count: number;
}

function snapshotsToChartData(snapshots: SnapshotEntry[]): SnapshotData[] {
  return snapshots.map((s) => ({
    cycle: s.cycle,
    elapsed_time: s.elapsed_time,
    pov_count: s.povs_in_snapshot,
    patch_count: s.patches_in_snapshot,
  }));
}

export default function BugFindingTrialPage({
  experimentName,
  report,
}: BugFindingTrialPageProps) {
  const { trial, summary, povs, llm_usage, time_series, timeline } = report;

  const [crsLogs, setCrsLogs] = useState<string | null>(null);
  const [crsLogsLoading, setCrsLogsLoading] = useState(false);
  const [crsLogsError, setCrsLogsError] = useState<string | null>(null);
  const [crsLogsModalOpen, setCrsLogsModalOpen] = useState(false);

  const [llmLogs, setLlmLogs] = useState<LLMLogsFile | null>(null);
  const [llmLogsLoading, setLlmLogsLoading] = useState(false);
  const [llmLogsError, setLlmLogsError] = useState<string | null>(null);
  const [llmLogsModalOpen, setLlmLogsModalOpen] = useState(false);

  const [executionInfo, setExecutionInfo] = useState<ExecutionInfo | null>(null);
  const [executionLoading, setExecutionLoading] = useState(false);
  const [executionError, setExecutionError] = useState<string | null>(null);
  const [executionModalOpen, setExecutionModalOpen] = useState(false);

  const loadCrsLogs = useCallback(async () => {
    if (crsLogs !== null) return;
    setCrsLogsLoading(true);
    try {
      const res = await fetch(
        `/api/experiments/${experimentName}/trials/${trial.trial_num}/logs?type=crs`
      );
      const data = await res.json();
      if (data.error) setCrsLogsError(data.error);
      else setCrsLogs(data.content);
    } catch {
      setCrsLogsError('Failed to load CRS logs');
    } finally {
      setCrsLogsLoading(false);
    }
  }, [experimentName, trial.trial_num, crsLogs]);

  const loadLlmLogs = useCallback(async () => {
    if (llmLogs !== null) return;
    setLlmLogsLoading(true);
    try {
      const res = await fetch(
        `/api/experiments/${experimentName}/trials/${trial.trial_num}/logs?type=llm`
      );
      const data = await res.json();
      if (data.error) setLlmLogsError(data.error);
      else setLlmLogs(data.content);
    } catch {
      setLlmLogsError('Failed to load LLM logs');
    } finally {
      setLlmLogsLoading(false);
    }
  }, [experimentName, trial.trial_num, llmLogs]);

  const loadExecutionInfo = useCallback(async () => {
    if (executionInfo !== null) return;
    setExecutionLoading(true);
    try {
      const res = await fetch(
        `/api/experiments/${experimentName}/trials/${trial.trial_num}/logs?type=execution`
      );
      const data = await res.json();
      if (data.error) setExecutionError(data.error);
      else setExecutionInfo(data.content);
    } catch {
      setExecutionError('Failed to load execution info');
    } finally {
      setExecutionLoading(false);
    }
  }, [experimentName, trial.trial_num, executionInfo]);

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
          <Link href="/" className="hover:text-foreground">Experiments</Link>
          <span>/</span>
          <Link href={`/experiments/${experimentName}`} className="hover:text-foreground">{experimentName}</Link>
          <span>/</span>
          <span>Trial {trial.trial_num}</span>
        </div>
        <div className="flex items-center gap-4">
          <h1 className="text-3xl font-bold">{trial.crs} - {trial.benchmark}/{trial.harness}</h1>
          <Badge variant="outline">Bug Finding</Badge>
        </div>
        {report.generated_at && (
          <p className="text-sm text-muted-foreground mt-1">Generated: {new Date(report.generated_at).toLocaleString()}</p>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-4">
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm text-muted-foreground">Discovered POVs</CardTitle></CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{summary.total_povs_discovered}</p>
            <p className="text-xs text-muted-foreground">{summary.unique_povs} unique</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm text-muted-foreground">Total Time</CardTitle></CardHeader>
          <CardContent><p className="text-2xl font-bold">{formatTime(summary.total_time)}</p></CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm text-muted-foreground">Time to First POV</CardTitle></CardHeader>
          <CardContent><p className="text-2xl font-bold">{summary.time_to_first_pov ? formatTime(summary.time_to_first_pov) : '-'}</p></CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm text-muted-foreground">LLM Cost</CardTitle></CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">${summary.total_llm_cost.toFixed(2)}</p>
            <p className="text-xs text-muted-foreground">{(summary.total_llm_tokens / 1000).toFixed(1)}K tokens</p>
          </CardContent>
        </Card>
      </div>

      <div className="flex gap-4">
        <Button variant="outline" onClick={() => { setCrsLogsModalOpen(true); loadCrsLogs(); }}>View CRS Logs</Button>
        <Button variant="outline" onClick={() => { setLlmLogsModalOpen(true); loadLlmLogs(); }}>View LLM Logs</Button>
        <Button variant="outline" onClick={() => { setExecutionModalOpen(true); loadExecutionInfo(); }}>View Execution Info</Button>
      </div>

      <Tabs defaultValue="timeline">
        <TabsList>
          <TabsTrigger value="timeline">POV Discovery Timeline</TabsTrigger>
          <TabsTrigger value="cost">Cost Over Time</TabsTrigger>
          <TabsTrigger value="breakdown">Cost Breakdown</TabsTrigger>
          <TabsTrigger value="snapshots">Snapshots</TabsTrigger>
        </TabsList>
        <TabsContent value="timeline" className="pt-4">
          <Card><CardContent className="pt-6">
            {time_series && time_series.length > 0 ? <DiscoveryTimeline data={time_series} /> : <p className="text-center text-muted-foreground py-8">No timeline data available</p>}
          </CardContent></Card>
        </TabsContent>
        <TabsContent value="cost" className="pt-4">
          <Card><CardContent className="pt-6">
            {time_series && time_series.length > 0 ? <CostOverTime data={time_series} /> : <p className="text-center text-muted-foreground py-8">No cost data available</p>}
          </CardContent></Card>
        </TabsContent>
        <TabsContent value="breakdown" className="pt-4">
          <Card><CardContent className="pt-6">
            {llm_usage?.by_model && Object.keys(llm_usage.by_model).length > 0 ? <CostBreakdown data={llm_usage.by_model} /> : <p className="text-center text-muted-foreground py-8">No LLM usage data available</p>}
          </CardContent></Card>
        </TabsContent>
        <TabsContent value="snapshots" className="pt-4">
          {timeline.snapshots.length > 0 ? (
            <div className="space-y-4">
              <Card><CardContent className="pt-6"><SnapshotTimeline data={snapshotsToChartData(timeline.snapshots)} /></CardContent></Card>
              <Card>
                <CardHeader><CardTitle>Snapshot Details ({timeline.total_snapshots})</CardTitle></CardHeader>
                <CardContent>
                  <Table>
                    <TableHeader><TableRow>
                      <TableHead>Cycle</TableHead><TableHead>Time</TableHead><TableHead className="text-right">POVs</TableHead><TableHead className="text-right">Cumulative</TableHead><TableHead className="text-right">LLM Cost</TableHead>
                    </TableRow></TableHeader>
                    <TableBody>
                      {timeline.snapshots.map((snap, idx) => (
                        <TableRow key={idx}>
                          <TableCell>{snap.cycle}</TableCell>
                          <TableCell>{(snap.elapsed_time / 3600).toFixed(2)}h</TableCell>
                          <TableCell className="text-right">{snap.povs_in_snapshot}</TableCell>
                          <TableCell className="text-right">{snap.cumulative_povs}</TableCell>
                          <TableCell className="text-right">${snap.llm_cost.toFixed(2)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
          ) : <Card><CardContent className="py-8 text-center text-muted-foreground">No snapshots available</CardContent></Card>}
        </TabsContent>
      </Tabs>

      <Card>
        <CardHeader><CardTitle>Discovered POVs ({povs.count})</CardTitle></CardHeader>
        <CardContent>
          {povs.unique_names.length > 0 ? (
            <ul className="space-y-1 text-sm font-mono">{povs.unique_names.map((pov, idx) => <li key={idx} className="text-muted-foreground">{pov}</li>)}</ul>
          ) : <p className="text-muted-foreground">No POVs discovered</p>}
        </CardContent>
      </Card>

      <CRSLogsModal open={crsLogsModalOpen} onOpenChange={setCrsLogsModalOpen} logs={crsLogs} loading={crsLogsLoading} error={crsLogsError} />
      <LLMLogsModal open={llmLogsModalOpen} onOpenChange={setLlmLogsModalOpen} logs={llmLogs} loading={llmLogsLoading} error={llmLogsError} />
      <BugFindingExecutionInfoModal open={executionModalOpen} onOpenChange={setExecutionModalOpen} info={executionInfo} loading={executionLoading} error={executionError} />
    </div>
  );
}
