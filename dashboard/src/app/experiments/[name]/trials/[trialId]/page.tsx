'use client';

import { useEffect, useState, use } from 'react';
import type { TrialReport } from '@/lib/data/trials';
import BugFindingTrialPage from './BugFindingTrialPage';
import BugFixingTrialPage from './BugFixingTrialPage';

interface TrialPageProps {
  params: Promise<{ name: string; trialId: string }>;
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

  // Route to appropriate page based on mode
  const mode = report.trial.mode;

  if (mode === 'bug_finding') {
    return <BugFindingTrialPage experimentName={name} trialId={trialId} report={report} />;
  }

  if (mode === 'patch_generation') {
    return <BugFixingTrialPage experimentName={name} trialId={trialId} report={report} />;
  }

  // Fallback: show bug finding page for unknown modes
  return <BugFindingTrialPage experimentName={name} trialId={trialId} report={report} />;
}
