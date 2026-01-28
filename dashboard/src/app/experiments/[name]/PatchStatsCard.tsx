'use client';

import { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

interface ExperimentPatchStats {
  totalTrials: number;
  trialsWithPatches: number;
  trialsWithVerification: number;
  totalPatchesGenerated: number;
  validPatches: number;
  buildFailed: number;
  povStillTriggers: number;
  testFailed: number;
  errorPatches: number;
}

interface PatchStatsCardProps {
  experimentName: string;
}

export function PatchStatsCard({ experimentName }: PatchStatsCardProps) {
  const [stats, setStats] = useState<ExperimentPatchStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchStats() {
      try {
        const res = await fetch(`/api/experiments/${experimentName}/patch-stats`);
        if (!res.ok) {
          throw new Error('Failed to fetch patch stats');
        }
        const data = await res.json();
        setStats(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    }
    fetchStats();
  }, [experimentName]);

  if (loading) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm text-muted-foreground">Patch Stats</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">Loading...</p>
        </CardContent>
      </Card>
    );
  }

  if (error || !stats) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm text-muted-foreground">Patch Stats</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-destructive">{error || 'No data'}</p>
        </CardContent>
      </Card>
    );
  }

  const failedPatches = stats.buildFailed + stats.errorPatches;
  const totalVerified = stats.validPatches + failedPatches + stats.povStillTriggers + stats.testFailed;

  return (
    <Card className="col-span-full">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm text-muted-foreground">
          Patch Verification Results
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <div>
            <p className="text-2xl font-bold">{stats.totalPatchesGenerated}</p>
            <p className="text-xs text-muted-foreground">Generated</p>
          </div>
          <div>
            <p className="text-2xl font-bold" style={{ color: '#16a34a' }}>
              {stats.validPatches}
            </p>
            <p className="text-xs text-muted-foreground">Verified</p>
          </div>
          <div>
            <p className="text-2xl font-bold" style={{ color: '#dc2626' }}>
              {failedPatches}
            </p>
            <p className="text-xs text-muted-foreground">Build Failed</p>
          </div>
          <div>
            <p className="text-2xl font-bold" style={{ color: '#ea580c' }}>
              {stats.povStillTriggers}
            </p>
            <p className="text-xs text-muted-foreground">POV Still Triggers</p>
          </div>
          <div>
            <p className="text-2xl font-bold" style={{ color: '#ca8a04' }}>
              {stats.testFailed}
            </p>
            <p className="text-xs text-muted-foreground">Test Failed</p>
          </div>
        </div>
        {stats.trialsWithVerification < stats.totalTrials && (
          <p className="text-xs text-muted-foreground mt-2">
            * Verification results from {stats.trialsWithVerification} of {stats.totalTrials} trials
          </p>
        )}
      </CardContent>
    </Card>
  );
}
