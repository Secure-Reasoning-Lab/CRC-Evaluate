import { NextRequest, NextResponse } from 'next/server';
import { loadTrialReport } from '@/lib/data/trials';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string; trialId: string }> }
) {
  try {
    const { name, trialId } = await params;

    // trialId is the trial number
    const trialNum = parseInt(trialId, 10);
    if (isNaN(trialNum)) {
      return NextResponse.json(
        { error: 'Invalid trial ID' },
        { status: 400 }
      );
    }

    const report = await loadTrialReport(name, trialNum);
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
