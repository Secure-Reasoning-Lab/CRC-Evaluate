import { NextRequest, NextResponse } from 'next/server';
import { readFile, readdir, stat } from 'fs/promises';
import path from 'path';
import { getBaseDir, findExperimentName } from '@/lib/data/experiments';

export interface WorkerLogInfo {
  name: string;
  size: number;
  modifiedAt: string;
}

export interface WorkerLogsListResponse {
  logs: WorkerLogInfo[];
  workerLogsDir: string;
}

export interface WorkerLogContentResponse {
  name: string;
  content: string;
  path: string;
}

/**
 * Get worker-logs directory path for an experiment
 */
function getWorkerLogsDir(outerDir: string, experimentName: string): string {
  return path.join(getBaseDir(), outerDir, 'experiment-data', experimentName, 'worker-logs');
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  try {
    const { name: outerDir } = await params;
    const { searchParams } = new URL(request.url);
    const logName = searchParams.get('log');

    // Find experiment name
    const experimentName = await findExperimentName(outerDir);
    if (!experimentName) {
      return NextResponse.json({ error: 'Experiment not found' }, { status: 404 });
    }

    const workerLogsDir = getWorkerLogsDir(outerDir, experimentName);

    // Check if worker-logs directory exists
    try {
      await stat(workerLogsDir);
    } catch {
      return NextResponse.json({
        logs: [],
        workerLogsDir,
        error: 'No worker-logs directory found',
      });
    }

    // If logName is specified, return specific log content
    if (logName) {
      const logPath = path.join(workerLogsDir, logName);

      // Security check - prevent directory traversal
      if (!logPath.startsWith(workerLogsDir)) {
        return NextResponse.json({ error: 'Invalid log name' }, { status: 400 });
      }

      try {
        const content = await readFile(logPath, 'utf-8');
        const response: WorkerLogContentResponse = {
          name: logName,
          content,
          path: logPath,
        };
        return NextResponse.json(response);
      } catch {
        return NextResponse.json(
          { error: `Log file not found: ${logName}` },
          { status: 404 }
        );
      }
    }

    // List all log files
    const files = await readdir(workerLogsDir);
    const logs: WorkerLogInfo[] = [];

    for (const file of files) {
      if (file.endsWith('.log')) {
        const filePath = path.join(workerLogsDir, file);
        const fileStat = await stat(filePath);
        logs.push({
          name: file,
          size: fileStat.size,
          modifiedAt: fileStat.mtime.toISOString(),
        });
      }
    }

    // Sort by name
    logs.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));

    const response: WorkerLogsListResponse = {
      logs,
      workerLogsDir,
    };

    return NextResponse.json(response);
  } catch (error) {
    console.error('Error getting worker logs:', error);
    return NextResponse.json(
      { error: 'Failed to get worker logs' },
      { status: 500 }
    );
  }
}
