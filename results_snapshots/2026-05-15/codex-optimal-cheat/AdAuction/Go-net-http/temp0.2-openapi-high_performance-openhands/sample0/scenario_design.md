# AdAuction Scenario Design

## Goal

AdAuction is intended to test whether models can build a CPU-heavy web service instead of only optimizing around database access. The hot endpoint, `POST /auction`, ranks eligible ad campaigns by targeting and vector similarity. A naive implementation will scan every campaign, compute a dot product for each one, sort all candidates, and often hold a broad lock while doing it.

## Performance Mistakes This Should Expose

- Full-table or full-slice scans for every auction even when country/interest targeting can prefilter candidates.
- Sorting every eligible campaign instead of using bounded top-k selection.
- Normalizing campaign vectors on every request instead of once at ingestion.
- Re-parsing or re-allocating large per-campaign structures inside the hot path.
- Holding one global mutex while doing all scoring work.
- Using database/disk I/O for read-mostly ranking state when the scenario does not require persistence.
- Updating impression/click counters under broad locks instead of using cheap counters or batched updates.

## Optimization Room

Good solutions can improve throughput by:

- normalizing campaign embeddings once on `POST /campaigns`;
- indexing campaigns by country and/or interest;
- copying small candidate slices under a read lock, then scoring outside the lock;
- using top-k selection for `slots` rather than full sorting;
- caching common segment/query shapes if semantics are preserved;
- storing campaign fields in compact, immutable structures and using atomic counters for stats.

## Reference Implementation

The Go reference implementation uses normalized vectors, a country index, top-k replacement, and atomic counters. It intentionally keeps the implementation simple enough to be readable while still demonstrating the main intended optimization path.

Functional status: `2/2` tests passed.
