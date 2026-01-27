import { NextRequest, NextResponse } from 'next/server';
import { readdir, stat } from 'fs/promises';
import path from 'path';
import { execSync } from 'child_process';
import { loadTrialReportByIndex } from '@/lib/data/trials';

interface SnapshotMetadata {
  cycle: number;
  timestamp: number;
  elapsed_time: number;
  snapshot_period?: number;
  running_elapsed_time?: number;
}

interface TimelineEntry {
  cycle: number;
  timestamp: number;
  elapsed_time: number;
  running_elapsed_time?: number;
  povs_in_snapshot: number;
  cumulative_povs: number;
  patches_in_snapshot: number;
  cumulative_patches: number;
  llm_cost: number;
  llm_tokens: number;
}

interface TimelineResponse {
  total_snapshots: number;
  snapshots: TimelineEntry[];
}

/**
 * Extract metadata and counts from a snapshot archive
 */
function extractSnapshotInfo(snapshotPath: string): { metadata: SnapshotMetadata; patchCount: number; povCount: number } | null {
  try {
    // List contents with sizes to count patches and povs (excluding empty patches)
    const listOutput = execSync(`tar -tvzf "${snapshotPath}" 2>/dev/null`, { encoding: 'utf-8' });
    const lines = listOutput.split('\n').filter(f => f.trim());

    // Track non-empty patches (patch.diff with size > 0)
    const nonEmptyPatches = new Set<string>();
    const povDirs = new Set<string>();

    for (const line of lines) {
      // tar -tvzf output format: -rw-r--r-- user/group   size date time path
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

      // Count POVs (any entry under povs/)
      if (filePath.startsWith('povs/') && filePath.includes('/')) {
        const povName = filePath.split('/')[1];
        if (povName && !povName.startsWith('.')) {
          povDirs.add(povName);
        }
      }
    }

    // Extract metadata.json
    try {
      const metadataJson = execSync(`tar -xzf "${snapshotPath}" -O metadata.json 2>/dev/null`, { encoding: 'utf-8' });
      const metadata: SnapshotMetadata = JSON.parse(metadataJson);

      return {
        metadata,
        patchCount: nonEmptyPatches.size,
        povCount: povDirs.size,
      };
    } catch {
      return null;
    }
  } catch {
    return null;
  }
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string; trialId: string }> }
) {
  try {
    const { name, trialId } = await params;

    const trialIndex = parseInt(trialId, 10);
    if (isNaN(trialIndex) || trialIndex < 0) {
      return NextResponse.json({ error: 'Invalid trial ID' }, { status: 400 });
    }

    const report = await loadTrialReportByIndex(name, trialIndex);
    if (!report) {
      return NextResponse.json({ error: 'Trial not found' }, { status: 404 });
    }

    const trialDir = report.trial.trial_dir;

    // Find all complete snapshots
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

    // Sort by cycle
    snapshotFiles.sort((a, b) => a.cycle - b.cycle);

    // Build timeline entries
    const snapshots: TimelineEntry[] = [];
    let cumulativePatches = 0;
    let cumulativePovs = 0;

    for (const snapshot of snapshotFiles) {
      const info = extractSnapshotInfo(snapshot.path);
      if (info) {
        cumulativePatches = info.patchCount; // In bug-fixing mode, this is the total count
        cumulativePovs = info.povCount;

        snapshots.push({
          cycle: info.metadata.cycle,
          timestamp: info.metadata.timestamp,
          elapsed_time: info.metadata.elapsed_time,
          running_elapsed_time: info.metadata.running_elapsed_time,
          povs_in_snapshot: info.povCount,
          cumulative_povs: cumulativePovs,
          patches_in_snapshot: info.patchCount,
          cumulative_patches: cumulativePatches,
          llm_cost: 0, // Would need to parse llm-usage.json for this
          llm_tokens: 0,
        });
      }
    }

    const response: TimelineResponse = {
      total_snapshots: snapshots.length,
      snapshots,
    };

    return NextResponse.json(response);
  } catch (error) {
    console.error('Error getting timeline:', error);
    return NextResponse.json(
      { error: 'Failed to get timeline' },
      { status: 500 }
    );
  }
}
