'use client';

import Link from 'next/link';
import { useState, useCallback, useEffect } from 'react';
import type {
  TrialReport,
  SnapshotEntry,
  LLMLogsFile,
  ExecutionInfo,
  ArtifactListResponse,
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
import { CostOverTime } from '@/components/charts/CostOverTime';
import { CostBreakdown } from '@/components/charts/CostBreakdown';
import { SnapshotTimeline } from '@/components/charts/SnapshotTimeline';
import {
  CRSLogsModal,
  LLMLogsModal,
  BugFixingExecutionInfoModal,
  ArtifactViewerModal,
  formatTime,
} from './components';

interface BugFixingTrialPageProps {
  experimentName: string;
  trialId: string;
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

export default function BugFixingTrialPage({
  experimentName,
  trialId,
  report,
}: BugFixingTrialPageProps) {
  const { trial, summary, patches, povs, llm_usage, time_series, timeline } = report;

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

  // Artifact viewer state
  const [artifactModalOpen, setArtifactModalOpen] = useState(false);
  const [selectedArtifactType, setSelectedArtifactType] = useState<'patch' | 'pov' | null>(null);
  const [selectedArtifactName, setSelectedArtifactName] = useState<string | null>(null);
  const [artifactContent, setArtifactContent] = useState<string | null>(null);
  const [artifactLoading, setArtifactLoading] = useState(false);
  const [artifactError, setArtifactError] = useState<string | null>(null);

  // Artifact list from snapshot (for actual content viewing)
  const [artifactList, setArtifactList] = useState<ArtifactListResponse | null>(null);
  const [artifactListLoaded, setArtifactListLoaded] = useState(false);

  // Timeline data (fallback from API when report timeline is empty)
  const [timelineData, setTimelineData] = useState(timeline);
  const [timelineLoaded, setTimelineLoaded] = useState(timeline.snapshots.length > 0);

  const loadCrsLogs = useCallback(async () => {
    if (crsLogs !== null) return;
    setCrsLogsLoading(true);
    try {
      const res = await fetch(
        `/api/experiments/${experimentName}/trials/${trialId}/logs?type=crs`
      );
      const data = await res.json();
      if (data.error) setCrsLogsError(data.error);
      else setCrsLogs(data.content);
    } catch {
      setCrsLogsError('Failed to load CRS logs');
    } finally {
      setCrsLogsLoading(false);
    }
  }, [experimentName, trialId, crsLogs]);

  const loadLlmLogs = useCallback(async () => {
    if (llmLogs !== null) return;
    setLlmLogsLoading(true);
    try {
      const res = await fetch(
        `/api/experiments/${experimentName}/trials/${trialId}/logs?type=llm`
      );
      const data = await res.json();
      if (data.error) setLlmLogsError(data.error);
      else setLlmLogs(data.content);
    } catch {
      setLlmLogsError('Failed to load LLM logs');
    } finally {
      setLlmLogsLoading(false);
    }
  }, [experimentName, trialId, llmLogs]);

  const loadExecutionInfo = useCallback(async () => {
    if (executionInfo !== null) return;
    setExecutionLoading(true);
    try {
      const res = await fetch(
        `/api/experiments/${experimentName}/trials/${trialId}/logs?type=execution`
      );
      const data = await res.json();
      if (data.error) setExecutionError(data.error);
      else setExecutionInfo(data.content);
    } catch {
      setExecutionError('Failed to load execution info');
    } finally {
      setExecutionLoading(false);
    }
  }, [experimentName, trialId, executionInfo]);

  // Load artifact list from snapshot
  const loadArtifactList = useCallback(async () => {
    if (artifactListLoaded) return;
    try {
      const res = await fetch(
        `/api/experiments/${experimentName}/trials/${trialId}/artifacts?type=list`
      );
      const data = await res.json();
      setArtifactList(data);
      setArtifactListLoaded(true);
    } catch {
      // Silently fail - artifact list is optional
      setArtifactListLoaded(true);
    }
  }, [experimentName, trialId, artifactListLoaded]);

  // Load artifact content
  const loadArtifactContent = useCallback(async (type: 'patch' | 'pov', name: string) => {
    setSelectedArtifactType(type);
    setSelectedArtifactName(name);
    setArtifactContent(null);
    setArtifactError(null);
    setArtifactLoading(true);
    setArtifactModalOpen(true);

    try {
      const res = await fetch(
        `/api/experiments/${experimentName}/trials/${trialId}/artifacts?type=${type}&name=${encodeURIComponent(name)}`
      );
      const data = await res.json();
      if (data.error) {
        setArtifactError(data.error);
      } else {
        setArtifactContent(data.content);
      }
    } catch {
      setArtifactError(`Failed to load ${type}`);
    } finally {
      setArtifactLoading(false);
    }
  }, [experimentName, trialId]);

  // Load artifact list on mount
  useEffect(() => {
    loadArtifactList();
  }, [loadArtifactList]);

  // Load timeline data from API if report timeline is empty
  useEffect(() => {
    if (timelineLoaded) return;

    async function loadTimeline() {
      try {
        const res = await fetch(`/api/experiments/${experimentName}/trials/${trialId}/timeline`);
        const data = await res.json();
        if (data.snapshots && data.snapshots.length > 0) {
          setTimelineData(data);
        }
      } catch {
        // Silently fail - timeline is optional
      } finally {
        setTimelineLoaded(true);
      }
    }
    loadTimeline();
  }, [experimentName, trialId, timelineLoaded]);

  // Load execution info on mount to show running time
  useEffect(() => {
    loadExecutionInfo();
  }, [loadExecutionInfo]);

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
          <Link href="/" className="hover:text-foreground">Experiments</Link>
          <span>/</span>
          <Link href={`/experiments/${experimentName}`} className="hover:text-foreground">{experimentName}</Link>
          <span>/</span>
          <span>Trial {trialId}</span>
        </div>
        <div className="flex items-center gap-4">
          <h1 className="text-3xl font-bold">{trial.crs} - {trial.benchmark}/{trial.harness}</h1>
          <Badge variant="outline">Bug Fixing</Badge>
        </div>
        {report.generated_at && (
          <p className="text-sm text-muted-foreground mt-1">Generated: {new Date(report.generated_at).toLocaleString()}</p>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-4">
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm text-muted-foreground">Generated Patches</CardTitle></CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{summary.total_patches_generated}</p>
            <p className="text-xs text-muted-foreground">{summary.unique_patches} unique</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm text-muted-foreground">Total Time</CardTitle></CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{formatTime(summary.total_time)}</p>
            {executionInfo?.execution?.duration_seconds && (
              <p className="text-xs text-muted-foreground">Running: {formatTime(executionInfo.execution.duration_seconds)}</p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-sm text-muted-foreground">Snapshots</CardTitle></CardHeader>
          <CardContent><p className="text-2xl font-bold">{summary.snapshot_count}</p></CardContent>
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

      <Tabs defaultValue="patches">
        <TabsList>
          <TabsTrigger value="patches">Patch Generation Timeline</TabsTrigger>
          <TabsTrigger value="cost">Cost Over Time</TabsTrigger>
          <TabsTrigger value="breakdown">Cost Breakdown</TabsTrigger>
          <TabsTrigger value="snapshots">Snapshots</TabsTrigger>
        </TabsList>
        <TabsContent value="patches" className="pt-4">
          <Card><CardContent className="pt-6">
            {timelineData.snapshots.length > 0 ? (
              <SnapshotTimeline data={snapshotsToChartData(timelineData.snapshots)} />
            ) : (timelineLoaded ? <p className="text-center text-muted-foreground py-8">No timeline data available</p> : <p className="text-center text-muted-foreground py-8">Loading timeline...</p>)}
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
          {timelineData.snapshots.length > 0 ? (
            <div className="space-y-4">
              <Card>
                <CardHeader><CardTitle>Snapshot Details ({timelineData.total_snapshots})</CardTitle></CardHeader>
                <CardContent>
                  <Table>
                    <TableHeader><TableRow>
                      <TableHead>Cycle</TableHead><TableHead>Elapsed</TableHead><TableHead>Running</TableHead><TableHead className="text-right">Patches</TableHead><TableHead className="text-right">Cumulative</TableHead><TableHead className="text-right">LLM Cost</TableHead>
                    </TableRow></TableHeader>
                    <TableBody>
                      {timelineData.snapshots.map((snap, idx) => (
                        <TableRow key={idx}>
                          <TableCell>{snap.cycle}</TableCell>
                          <TableCell>{(snap.elapsed_time / 3600).toFixed(2)}h</TableCell>
                          <TableCell>{snap.running_elapsed_time != null ? `${(snap.running_elapsed_time / 3600).toFixed(2)}h` : '-'}</TableCell>
                          <TableCell className="text-right">{snap.patches_in_snapshot}</TableCell>
                          <TableCell className="text-right">{snap.cumulative_patches}</TableCell>
                          <TableCell className="text-right">${snap.llm_cost.toFixed(2)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
          ) : (timelineLoaded ? <Card><CardContent className="py-8 text-center text-muted-foreground">No snapshots available</CardContent></Card> : <Card><CardContent className="py-8 text-center text-muted-foreground">Loading snapshots...</CardContent></Card>)}
        </TabsContent>
      </Tabs>

      {/* Generated Patches Card */}
      <Card>
        <CardHeader><CardTitle>Generated Patches ({patches.count || artifactList?.patches?.length || 0})</CardTitle></CardHeader>
        <CardContent>
          {/* Use artifact list if patches.unique_names is empty but artifacts exist */}
          {(patches.unique_names.length > 0 || (artifactList?.patches && artifactList.patches.length > 0)) ? (
            <div className="space-y-1">
              <p className="text-xs text-muted-foreground mb-3">Click on a patch name to view its content</p>
              <ul className="space-y-1 text-sm font-mono">
                {(patches.unique_names.length > 0 ? patches.unique_names : artifactList?.patches?.map(p => p.name) || []).map((patch, idx) => {
                  const patchInSnapshot = artifactList?.patches?.some(p => p.name === patch);
                  return (
                    <li key={idx}>
                      {patchInSnapshot || !artifactListLoaded ? (
                        <button
                          onClick={() => loadArtifactContent('patch', patch)}
                          className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-200 hover:underline text-left"
                        >
                          {patch}
                        </button>
                      ) : (
                        <span className="text-muted-foreground">{patch}</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : (artifactListLoaded ? <p className="text-muted-foreground">No patches generated</p> : <p className="text-muted-foreground">Loading patches...</p>)}
        </CardContent>
      </Card>

      {/* POVs Card (if available) */}
      {(povs?.unique_names?.length > 0 || (artifactList?.povs && artifactList.povs.length > 0)) && (
        <Card>
          <CardHeader><CardTitle>POVs ({povs?.count || artifactList?.povs?.length || 0})</CardTitle></CardHeader>
          <CardContent>
            <div className="space-y-1">
              <p className="text-xs text-muted-foreground mb-3">Click on a POV name to view its content</p>
              <ul className="space-y-1 text-sm font-mono">
                {(povs?.unique_names?.length > 0 ? povs.unique_names : artifactList?.povs?.map(p => p.name) || []).map((pov, idx) => {
                  const povInSnapshot = artifactList?.povs?.some(p => p.name === pov);
                  return (
                    <li key={idx}>
                      {povInSnapshot || !artifactListLoaded ? (
                        <button
                          onClick={() => loadArtifactContent('pov', pov)}
                          className="text-green-600 hover:text-green-800 dark:text-green-400 dark:hover:text-green-200 hover:underline text-left"
                        >
                          {pov}
                        </button>
                      ) : (
                        <span className="text-muted-foreground">{pov}</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          </CardContent>
        </Card>
      )}

      <CRSLogsModal open={crsLogsModalOpen} onOpenChange={setCrsLogsModalOpen} logs={crsLogs} loading={crsLogsLoading} error={crsLogsError} />
      <LLMLogsModal open={llmLogsModalOpen} onOpenChange={setLlmLogsModalOpen} logs={llmLogs} loading={llmLogsLoading} error={llmLogsError} />
      <BugFixingExecutionInfoModal open={executionModalOpen} onOpenChange={setExecutionModalOpen} info={executionInfo} loading={executionLoading} error={executionError} />
      <ArtifactViewerModal
        open={artifactModalOpen}
        onOpenChange={setArtifactModalOpen}
        artifactType={selectedArtifactType}
        artifactName={selectedArtifactName}
        content={artifactContent}
        loading={artifactLoading}
        error={artifactError}
      />
    </div>
  );
}
