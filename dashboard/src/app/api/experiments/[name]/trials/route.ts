import { NextRequest, NextResponse } from 'next/server';
import { getExperimentReport } from '@/lib/data/experiments';
import { listTrialReports } from '@/lib/data/trials';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  try {
    const { name } = await params;

    // Check experiment exists
    const report = await getExperimentReport(name);
    if (!report) {
      return NextResponse.json(
        { error: 'Experiment not found' },
        { status: 404 }
      );
    }

    // List trial reports
    const trials = await listTrialReports(name);

    // Return trial summaries from experiment report
    return NextResponse.json({
      trials: report.trial_summaries,
      trial_files: trials,
    });
  } catch (error) {
    console.error('Error listing trials:', error);
    return NextResponse.json(
      { error: 'Failed to list trials' },
      { status: 500 }
    );
  }
}
