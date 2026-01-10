import { NextResponse } from 'next/server';
import { listExperiments } from '@/lib/data/experiments';

export async function GET() {
  try {
    const experiments = await listExperiments();
    return NextResponse.json(experiments);
  } catch (error) {
    console.error('Error listing experiments:', error);
    return NextResponse.json(
      { error: 'Failed to list experiments' },
      { status: 500 }
    );
  }
}
