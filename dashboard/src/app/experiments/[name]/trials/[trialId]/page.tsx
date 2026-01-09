'use client';

import Link from 'next/link';
import { useEffect, useState, use } from 'react';
import type { TrialReport, SnapshotEntry } from '@/lib/data/trials';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
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

interface TrialPageProps {
  params: Promise<{ name: string; trialId: string }>;
}

// Adapter for SnapshotTimeline component
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

export default function TrialPage({ params }: TrialPageProps) {
  const { name, trialId } = use(params);
  const [report, setReport] = useState<TrialReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function loadData() {
      try {
        const res = await fetch(`/api/experiments/${name}/trials/${trialId}`);
        if (!res.ok) {
          throw new Error('Failed to load trial data');
        }
        const trialReport = await res.json();
        setReport(trialReport);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [name, trialId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">Loading trial data...</p>
      </div>
    );
  }

  if (error || !report) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-destructive">{error || 'Trial not found'}</p>
      </div>
    );
  }

  const { trial, summary, povs, patches, llm_usage, time_series, timeline } = report;

  const formatTime = (seconds: number): string => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (hours > 0) {
      return `${hours}h ${minutes}m`;
    }
    return `${minutes}m`;
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
          <Link href="/" className="hover:text-foreground">
            Experiments
          </Link>
          <span>/</span>
          <Link href={`/experiments/${name}`} className="hover:text-foreground">
            {name}
          </Link>
          <span>/</span>
          <span>Trial {trial.trial_num}</span>
        </div>
        <div className="flex items-center gap-4">
          <h1 className="text-3xl font-bold">
            {trial.crs} - {trial.benchmark}/{trial.harness}
          </h1>
          <Badge variant="outline">{trial.mode}</Badge>
        </div>
        {report.generated_at && (
          <p className="text-sm text-muted-foreground mt-1">
            Generated: {new Date(report.generated_at).toLocaleString()}
          </p>
        )}
      </div>

      {/* Summary Cards */}
      <div className="grid gap-4 md:grid-cols-5">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">POVs</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{summary.total_povs_discovered}</p>
            <p className="text-xs text-muted-foreground">
              {summary.unique_povs} unique
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Patches</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{summary.total_patches_generated}</p>
            <p className="text-xs text-muted-foreground">
              {summary.unique_patches} unique
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">
              Total Time
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{formatTime(summary.total_time)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">
              Time to First POV
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">
              {summary.time_to_first_pov
                ? formatTime(summary.time_to_first_pov)
                : '-'}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">
              LLM Cost
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">
              ${summary.total_llm_cost.toFixed(2)}
            </p>
            <p className="text-xs text-muted-foreground">
              {(summary.total_llm_tokens / 1000).toFixed(1)}K tokens
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Charts */}
      <Tabs defaultValue="timeline">
        <TabsList>
          <TabsTrigger value="timeline">Discovery Timeline</TabsTrigger>
          <TabsTrigger value="cost">Cost Over Time</TabsTrigger>
          <TabsTrigger value="breakdown">Cost Breakdown</TabsTrigger>
          <TabsTrigger value="snapshots">Snapshots</TabsTrigger>
        </TabsList>
        <TabsContent value="timeline" className="pt-4">
          <Card>
            <CardContent className="pt-6">
              {time_series && time_series.length > 0 ? (
                <DiscoveryTimeline data={time_series} />
              ) : (
                <p className="text-center text-muted-foreground py-8">
                  No timeline data available
                </p>
              )}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="cost" className="pt-4">
          <Card>
            <CardContent className="pt-6">
              {time_series && time_series.length > 0 ? (
                <CostOverTime data={time_series} />
              ) : (
                <p className="text-center text-muted-foreground py-8">
                  No cost data available
                </p>
              )}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="breakdown" className="pt-4">
          <Card>
            <CardContent className="pt-6">
              {llm_usage?.by_model && Object.keys(llm_usage.by_model).length > 0 ? (
                <CostBreakdown data={llm_usage.by_model} />
              ) : (
                <p className="text-center text-muted-foreground py-8">
                  No LLM usage data available
                </p>
              )}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="snapshots" className="pt-4">
          {timeline.snapshots.length > 0 ? (
            <div className="space-y-4">
              <Card>
                <CardContent className="pt-6">
                  <SnapshotTimeline data={snapshotsToChartData(timeline.snapshots)} />
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>Snapshot Details ({timeline.total_snapshots})</CardTitle>
                </CardHeader>
                <CardContent>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Cycle</TableHead>
                        <TableHead>Time</TableHead>
                        <TableHead className="text-right">POVs</TableHead>
                        <TableHead className="text-right">Cumulative</TableHead>
                        <TableHead className="text-right">Patches</TableHead>
                        <TableHead className="text-right">Cumulative</TableHead>
                        <TableHead className="text-right">LLM Cost</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {timeline.snapshots.map((snap, idx) => (
                        <TableRow key={idx}>
                          <TableCell>{snap.cycle}</TableCell>
                          <TableCell>
                            {(snap.elapsed_time / 3600).toFixed(2)}h
                          </TableCell>
                          <TableCell className="text-right">
                            {snap.povs_in_snapshot}
                          </TableCell>
                          <TableCell className="text-right">
                            {snap.cumulative_povs}
                          </TableCell>
                          <TableCell className="text-right">
                            {snap.patches_in_snapshot}
                          </TableCell>
                          <TableCell className="text-right">
                            {snap.cumulative_patches}
                          </TableCell>
                          <TableCell className="text-right">
                            ${snap.llm_cost.toFixed(2)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
          ) : (
            <Card>
              <CardContent className="py-8 text-center text-muted-foreground">
                No snapshots available
              </CardContent>
            </Card>
          )}
        </TabsContent>
      </Tabs>

      {/* POV and Patch Lists */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Discovered POVs ({povs.count})</CardTitle>
          </CardHeader>
          <CardContent>
            {povs.unique_names.length > 0 ? (
              <ul className="space-y-1 text-sm font-mono">
                {povs.unique_names.map((pov, idx) => (
                  <li key={idx} className="text-muted-foreground">
                    {pov}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground">No POVs discovered</p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Generated Patches ({patches.count})</CardTitle>
          </CardHeader>
          <CardContent>
            {patches.unique_names.length > 0 ? (
              <ul className="space-y-1 text-sm font-mono">
                {patches.unique_names.map((patch, idx) => (
                  <li key={idx} className="text-muted-foreground">
                    {patch}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground">No patches generated</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
