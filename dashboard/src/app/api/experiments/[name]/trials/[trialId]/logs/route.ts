import { NextRequest, NextResponse } from 'next/server';
import { readFile, readdir } from 'fs/promises';
import path from 'path';
import { loadTrialReportByIndex } from '@/lib/data/trials';
import type { LLMLogsFile } from '@/lib/types';

export interface ExecutionInfo {
  timestamp: string;
  command: string[];
  crs_config: {
    build_timeout: number;
    run_timeout: number;
    hints_enabled: boolean;
    hint_sarif_level: string | null;
    hint_corpus_level: string | null;
    project_image_prefix: string;
    mode: string;
    allocated_cpus: number | null;
  };
  hints: {
    enabled: boolean;
    path: string | null;
    corpus_level: string | null;
    sarif_count: number;
    corpus_count: number;
  };
  povs: {
    provided: boolean;
    path: string;
    count: number;
  };
  execution: {
    duration_seconds: number;
    returncode: number;
    success: boolean;
  };
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string; trialId: string }> }
) {
  try {
    const { name, trialId } = await params;
    const { searchParams } = new URL(request.url);
    const logType = searchParams.get('type') || 'crs';

    const trialIndex = parseInt(trialId, 10);
    if (isNaN(trialIndex) || trialIndex < 0) {
      return NextResponse.json({ error: 'Invalid trial ID' }, { status: 400 });
    }

    // Load trial report to get trial directory
    const report = await loadTrialReportByIndex(name, trialIndex);
    if (!report) {
      return NextResponse.json({ error: 'Trial not found' }, { status: 404 });
    }

    const trialDir = report.trial.trial_dir;

    if (logType === 'crs') {
      // Read CRS output log from crs-logs/ directory
      const crsLogsDir = path.join(trialDir, 'crs-logs');
      try {
        const files = await readdir(crsLogsDir);
        // Find log file (pattern: crs_run_*.log or any .log file)
        const logFile = files.find((f) => f.endsWith('.log'));
        if (!logFile) {
          return NextResponse.json({
            type: 'crs',
            content: null,
            error: 'No CRS log file found in crs-logs directory',
          });
        }
        const logPath = path.join(crsLogsDir, logFile);
        const content = await readFile(logPath, 'utf-8');
        return NextResponse.json({
          type: 'crs',
          content,
          filename: logFile,
        });
      } catch {
        // Fallback: try reading crs-output.log directly from trial dir
        try {
          const fallbackPath = path.join(trialDir, 'crs-output.log');
          const content = await readFile(fallbackPath, 'utf-8');
          return NextResponse.json({
            type: 'crs',
            content,
            filename: 'crs-output.log',
          });
        } catch {
          return NextResponse.json({
            type: 'crs',
            content: null,
            error: 'CRS log not found',
          });
        }
      }
    } else if (logType === 'llm') {
      // Read LLM logs
      const logPath = path.join(trialDir, 'llm-logs.json');
      try {
        const content = await readFile(logPath, 'utf-8');
        const llmLogs: LLMLogsFile = JSON.parse(content);
        return NextResponse.json({
          type: 'llm',
          content: llmLogs,
        });
      } catch {
        return NextResponse.json({
          type: 'llm',
          content: null,
          error: 'LLM logs not found',
        });
      }
    } else if (logType === 'execution') {
      // Read execution.json
      const execPath = path.join(trialDir, 'execution.json');
      try {
        const content = await readFile(execPath, 'utf-8');
        const execution: ExecutionInfo = JSON.parse(content);
        return NextResponse.json({
          type: 'execution',
          content: execution,
        });
      } catch {
        return NextResponse.json({
          type: 'execution',
          content: null,
          error: 'Execution info not found',
        });
      }
    } else {
      return NextResponse.json(
        { error: 'Invalid log type. Use "crs", "llm", or "execution"' },
        { status: 400 }
      );
    }
  } catch (error) {
    console.error('Error getting logs:', error);
    return NextResponse.json({ error: 'Failed to get logs' }, { status: 500 });
  }
}
