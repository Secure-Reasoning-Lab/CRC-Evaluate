import { NextRequest, NextResponse } from 'next/server';
import { getExperimentReport } from '@/lib/data/experiments';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  try {
    const { name } = await params;
    const report = await getExperimentReport(name);

    if (!report) {
      return NextResponse.json(
        { error: 'Experiment not found' },
        { status: 404 }
      );
    }

    return NextResponse.json(report);
  } catch (error) {
    console.error('Error getting experiment:', error);
    return NextResponse.json(
      { error: 'Failed to get experiment' },
      { status: 500 }
    );
  }
}
