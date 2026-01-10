import Link from 'next/link';
import { listExperiments } from '@/lib/data/experiments';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';

export const dynamic = 'force-dynamic';

export default async function HomePage() {
  const experiments = await listExperiments();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">Experiments</h1>
          <p className="text-muted-foreground">
            View and analyze CRS experiment results
          </p>
        </div>
        <Badge variant="outline" className="text-sm">
          {experiments.length} experiments
        </Badge>
      </div>

      {experiments.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center">
            <p className="text-muted-foreground">
              No experiments found. Make sure REPORT_DIR is set correctly.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {experiments.map((exp) => (
            <Link key={exp.name} href={`/experiments/${exp.name}`}>
              <Card className="hover:border-primary transition-colors cursor-pointer h-full">
                <CardHeader className="pb-2">
                  <CardTitle className="text-lg truncate">{exp.name}</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2 text-sm">
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Trials:</span>
                      <span className="font-medium">{exp.summary?.total_trials || 0}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">CRSes:</span>
                      <span className="font-medium">{exp.crs_list.length}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Benchmarks:</span>
                      <span className="font-medium">{exp.benchmark_list.length}</span>
                    </div>
                    {exp.summary && (
                      <div className="flex justify-between">
                        <span className="text-muted-foreground">Avg POVs:</span>
                        <span className="font-medium">
                          {exp.summary.avg_povs_per_trial.toFixed(2)}
                        </span>
                      </div>
                    )}
                    {exp.crs_list.length > 0 && (
                      <div className="pt-2 flex flex-wrap gap-1">
                        {exp.crs_list.slice(0, 3).map((crs) => (
                          <Badge key={crs} variant="secondary" className="text-xs">
                            {crs}
                          </Badge>
                        ))}
                        {exp.crs_list.length > 3 && (
                          <Badge variant="secondary" className="text-xs">
                            +{exp.crs_list.length - 3} more
                          </Badge>
                        )}
                      </div>
                    )}
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
