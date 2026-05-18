# CRSBench Dashboard (Experimental)

> **Status: Experimental.** This Next.js dashboard is an experimental, read-only
> viewer for CRSBench experiment results. Routes, server-side APIs, JSON
> contracts, and CLI flags may change without notice. It has not been hardened
> for multi-user or remote deployment and is intended to run on `localhost`
> only.

## What it does

Visualizes already-completed CRSBench experiments by reading the `report-data/`
and `experiment-data/` directories produced by `crsbench report`. Features:

- Experiment list with mode, CRS, and benchmark summaries
- Per-experiment CRS comparison and benchmark analysis charts
- Per-trial drill-down: time series, snapshots, CRS logs, LLM conversation viewer
- Patch verification stats and worker/error log viewers

It does not start, stop, or otherwise control experiments.

## Usage

Preferred entry point (uses the CRSBench CLI):

```bash
uv run crsbench dashboard --base-dir ./experiments
```

Run `crsbench report` first to generate the JSON reports the dashboard reads.

For local Next.js development inside this directory:

```bash
npm install
BASE_DIR=/absolute/path/to/experiments npm run dev
```

Open <http://localhost:3000>.

## Directory layout expected

```
<base-dir>/
  <experiment-name>/
    experiment-data/   # Trial execution data
    report-data/       # Generated reports (experiment-*.json, trial-reports/)
```

## Caveats

- Bound to `localhost`; not safe to expose publicly.
- No authentication, rate-limiting, or input validation beyond what Next.js
  provides by default.
- File I/O is performed on every request; no caching layer.
- Some endpoints (e.g. patch stats) shell out synchronously; performance on
  experiments with hundreds of trials is not yet validated.
