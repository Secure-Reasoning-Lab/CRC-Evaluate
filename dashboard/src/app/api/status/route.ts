import { NextResponse } from 'next/server';
import { listExperiments } from '@/lib/data/experiments';

export async function GET() {
  try {
    // Since we're reading from static JSON reports, no "active" experiments
    const experiments = await listExperiments();

    return NextResponse.json({
      experiment_count: experiments.length,
      timestamp: new Date().toISOString(),
    });
  } catch (error) {
    console.error('Error getting status:', error);
    return NextResponse.json(
      { error: 'Failed to get status' },
      { status: 500 }
    );
  }
}
