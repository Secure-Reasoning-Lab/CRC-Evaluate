import { NextRequest, NextResponse } from 'next/server';
import { loadTrialReportByIndex } from '@/lib/data/trials';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string; trialId: string }> }
) {
  try {
    const { name, trialId } = await params;

    // trialId is the index in the trial_summaries array
    const trialIndex = parseInt(trialId, 10);
    if (isNaN(trialIndex) || trialIndex < 0) {
      return NextResponse.json(
        { error: 'Invalid trial ID' },
        { status: 400 }
      );
    }

    const report = await loadTrialReportByIndex(name, trialIndex);
    if (!report) {
      return NextResponse.json(
        { error: 'Trial not found' },
        { status: 404 }
      );
    }

    return NextResponse.json(report);
  } catch (error) {
    console.error('Error getting trial:', error);
    return NextResponse.json(
      { error: 'Failed to get trial' },
      { status: 500 }
    );
  }
}
