import { NextRequest, NextResponse } from 'next/server';
import { readFile, readdir, stat } from 'fs/promises';
import path from 'path';
import { execSync } from 'child_process';
import { getExperimentReport } from '@/lib/data/experiments';

/**
 * Count non-empty patches from the latest snapshot in a trial directory
 */
async function countNonEmptyPatches(trialDir: string): Promise<number> {
  try {
    const files = await readdir(trialDir);
    const snapshotFiles: { path: string; cycle: number }[] = [];

    for (const file of files) {
      const match = file.match(/^snapshot-(\d+)\.tar\.gz$/);
      if (match) {
        const cycle = parseInt(match[1], 10);
        const completePath = path.join(trialDir, `snapshot-${match[1]}.complete`);
        try {
          await stat(completePath);
          snapshotFiles.push({ path: path.join(trialDir, file), cycle });
        } catch {
          // No .complete marker, skip
        }
      }
    }

    if (snapshotFiles.length === 0) {
      return 0;
    }

    // Get latest snapshot
    snapshotFiles.sort((a, b) => b.cycle - a.cycle);
    const latestSnapshot = snapshotFiles[0].path;

    // List contents with sizes
    const listOutput = execSync(`tar -tvzf "${latestSnapshot}" 2>/dev/null`, { encoding: 'utf-8' });
    const lines = listOutput.split('\n').filter(f => f.trim());

    const nonEmptyPatches = new Set<string>();

    for (const line of lines) {
      const parts = line.split(/\s+/);
      if (parts.length < 6) continue;

      const size = parseInt(parts[2], 10);
      const filePath = parts.slice(5).join(' ');

      // Check for patch.diff files with non-zero size
      if (filePath.startsWith('patches/') && filePath.endsWith('/patch.diff')) {
        if (size > 0) {
          const patchName = filePath.split('/')[1];
          if (patchName && !patchName.startsWith('.')) {
            nonEmptyPatches.add(patchName);
          }
        }
      }
    }

    return nonEmptyPatches.size;
  } catch {
    return 0;
  }
}

interface PatchVerificationSummary {
  total_input_povs: number;
  patches_generated: number;
  patch_ids: string[];
  valid: number;
  build_failed: number;
  pov_still_triggers: number;
  test_failed: number;
  error: number;
}

interface PatchVerificationResults {
  summary: PatchVerificationSummary;
  results: unknown[];
}

export interface ExperimentPatchStats {
  // Aggregated across all trials
  totalTrials: number;
  trialsWithPatches: number;
  trialsWithVerification: number;

  // Patch generation stats
  totalPatchesGenerated: number;

  // Verification stats
  validPatches: number;
  buildFailed: number;
  povStillTriggers: number;
  testFailed: number;
  errorPatches: number;

  // Per-trial breakdown
  trialStats: TrialPatchStat[];
}

interface TrialPatchStat {
  trialIndex: number;
  benchmark: string;
  harness: string;
  patchesGenerated: number;
  valid: number;
  buildFailed: number;
  povStillTriggers: number;
  testFailed: number;
  error: number;
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  try {
    const { name } = await params;

    // Load experiment report to get trial info
    const report = await getExperimentReport(name);
    if (!report) {
      return NextResponse.json({ error: 'Experiment not found' }, { status: 404 });
    }

    const stats: ExperimentPatchStats = {
      totalTrials: report.trial_summaries.length,
      trialsWithPatches: 0,
      trialsWithVerification: 0,
      totalPatchesGenerated: 0,
      validPatches: 0,
      buildFailed: 0,
      povStillTriggers: 0,
      testFailed: 0,
      errorPatches: 0,
      trialStats: [],
    };

    // Process each trial
    for (let i = 0; i < report.trial_summaries.length; i++) {
      const trial = report.trial_summaries[i];
      const verificationPath = path.join(
        trial.trial_dir,
        'patch_verification_results.json'
      );

      const trialStat: TrialPatchStat = {
        trialIndex: i,
        benchmark: trial.benchmark,
        harness: trial.harness,
        patchesGenerated: 0,
        valid: 0,
        buildFailed: 0,
        povStillTriggers: 0,
        testFailed: 0,
        error: 0,
      };

      // Count non-empty patches from snapshot (more accurate than verification file)
      const actualPatchCount = await countNonEmptyPatches(trial.trial_dir);

      if (actualPatchCount > 0) {
        stats.trialsWithPatches++;
      }

      try {
        const content = await readFile(verificationPath, 'utf-8');
        const verification: PatchVerificationResults = JSON.parse(content);
        const summary = verification.summary;

        stats.trialsWithVerification++;
        // Use actual non-empty patch count instead of verification file's patches_generated
        stats.totalPatchesGenerated += actualPatchCount;
        stats.validPatches += summary.valid;
        stats.buildFailed += summary.build_failed;
        stats.povStillTriggers += summary.pov_still_triggers;
        stats.testFailed += summary.test_failed;
        stats.errorPatches += summary.error;

        trialStat.patchesGenerated = actualPatchCount;
        trialStat.valid = summary.valid;
        trialStat.buildFailed = summary.build_failed;
        trialStat.povStillTriggers = summary.pov_still_triggers;
        trialStat.testFailed = summary.test_failed;
        trialStat.error = summary.error;
      } catch {
        // No verification results for this trial - use actual patch count
        trialStat.patchesGenerated = actualPatchCount;
        stats.totalPatchesGenerated += actualPatchCount;
      }

      stats.trialStats.push(trialStat);
    }

    return NextResponse.json(stats);
  } catch (error) {
    console.error('Error getting patch stats:', error);
    return NextResponse.json(
      { error: 'Failed to get patch stats' },
      { status: 500 }
    );
  }
}
