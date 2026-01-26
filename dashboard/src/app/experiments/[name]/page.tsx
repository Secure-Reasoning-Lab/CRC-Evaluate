import Link from 'next/link';
import { notFound } from 'next/navigation';
import { getExperimentReport } from '@/lib/data/experiments';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { CRSComparison } from '@/components/charts/CRSComparison';
import { BenchmarkAnalysis } from '@/components/charts/BenchmarkAnalysis';

export const dynamic = 'force-dynamic';

interface ExperimentPageProps {
  params: Promise<{ name: string }>;
}

export default async function ExperimentPage({ params }: ExperimentPageProps) {
  const { name } = await params;
  const report = await getExperimentReport(name);

  if (!report) {
    notFound();
  }

  const summary = report.summary;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
            <Link href="/" className="hover:text-foreground">
              Experiments
            </Link>
            <span>/</span>
            <span>{name}</span>
          </div>
          <h1 className="text-3xl font-bold">{name}</h1>
          {report.generated_at && (
            <p className="text-sm text-muted-foreground">
              Generated: {new Date(report.generated_at).toLocaleString()}
            </p>
          )}
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid gap-4 md:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">
              Total Trials
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{summary.total_trials}</p>
            <p className="text-xs text-muted-foreground">
              {summary.valid_trials} valid
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">
              Avg POVs/Trial
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">
              {summary.avg_povs_per_trial.toFixed(2)}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">
              Avg Patches/Trial
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">
              {summary.avg_patches_per_trial.toFixed(2)}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">
              Avg Cost/Trial
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">
              ${summary.avg_cost_per_trial.toFixed(2)}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Charts */}
      <Tabs defaultValue="crs">
        <TabsList>
          <TabsTrigger value="crs">CRS Comparison</TabsTrigger>
          <TabsTrigger value="benchmark">Benchmark Analysis</TabsTrigger>
        </TabsList>
        <TabsContent value="crs" className="pt-4">
          <Card>
            <CardContent className="pt-6">
              {Object.keys(report.by_crs).length > 0 ? (
                <CRSComparison data={report.by_crs} />
              ) : (
                <p className="text-center text-muted-foreground py-8">
                  No CRS data available
                </p>
              )}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="benchmark" className="pt-4">
          <Card>
            <CardContent className="pt-6">
              {Object.keys(report.by_benchmark).length > 0 ? (
                <BenchmarkAnalysis data={report.by_benchmark} />
              ) : (
                <p className="text-center text-muted-foreground py-8">
                  No benchmark data available
                </p>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Trial Table */}
      <Card>
        <CardHeader>
          <CardTitle>Trials ({report.trial_summaries.length})</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Trial</TableHead>
                <TableHead>CRS</TableHead>
                <TableHead>Benchmark</TableHead>
                <TableHead>Harness</TableHead>
                <TableHead className="text-right">POVs</TableHead>
                <TableHead className="text-right">Patches</TableHead>
                <TableHead className="text-right">Cost</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {report.trial_summaries.map((trial, idx) => (
                <TableRow key={idx} className="hover:bg-muted/50">
                  <TableCell className="font-medium">
                    <Link
                      href={`/experiments/${name}/trials/${idx}`}
                      className="hover:underline text-primary"
                    >
                      #{idx}
                    </Link>
                  </TableCell>
                  <TableCell>{trial.crs}</TableCell>
                  <TableCell>{trial.benchmark}</TableCell>
                  <TableCell>{trial.harness}</TableCell>
                  <TableCell className="text-right">
                    {trial.total_povs}
                  </TableCell>
                  <TableCell className="text-right">
                    {trial.total_patches}
                  </TableCell>
                  <TableCell className="text-right">
                    ${trial.total_cost.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button variant="outline" size="sm" asChild>
                      <Link href={`/experiments/${name}/trials/${idx}`}>
                        View
                      </Link>
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
