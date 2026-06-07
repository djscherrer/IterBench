# MicroBlog semantic-preserving optimization summary

## Scope

This pass created semantic-preserving optimized variants for:

- `Go-net/http`
- `Python-Flask`
- `Rust-Actix`

All three variants live under:

`results_snapshots/2026-05-16/manual-semantic/MicroBlog/`

The main constraint was to avoid the earlier high-throughput strategy of replacing the DB-backed hot path with in-memory authoritative state. These variants keep PostgreSQL as the synchronous source of truth and only change query shape, indexes, pool sizing, and small allocation details.

## Shared optimization

The main shared bottleneck in the base samples was the feed query:

```sql
WHERE p.username = $1
   OR p.username IN (...)
ORDER BY p.created_at DESC
LIMIT ...
```

Each semantic variant rewrites this into:

- A `visible_users` CTE containing the requester and followed users.
- A `JOIN LATERAL` per visible user.
- A bounded per-user newest-post scan.
- A final global `ORDER BY created_at DESC LIMIT ...`.

Each variant also replaces broad single-column post indexes with covering indexes shaped for feed and trending reads. The follows primary key already supports `follower_username` lookups, so redundant follows indexes were removed.

## Validation

All three variants passed the MicroBlog functional test:

| env | functional test | image id |
|---|---:|---|
| Go-net/http | 1/1 | `sha256:56c756cba667cae496da6e8d34fca4e2df511893d491c5d2a268fb7bbb233754` |
| Python-Flask | 1/1 | `sha256:9073f3cb1c9f051bd1b7f62c3a9daed0d7f90aba2f1f9020db4f84946fcc6464` |
| Rust-Actix | 1/1 | `sha256:316e4227f5b7e59bb3e0239c284eed4e212224a85e90ff235743417b0977de9f` |

## Original-topology benchmarks

All runs used:

- Topology: `2C-1B-1DB-8W`
- Load profile: `stairs-massive-microblog`
- Backend: `r630-05`
- Load: `r630-06`, `r630-07`, `r630-08`
- Database: `r630-09`

| env | run | final aggregate | best useful step |
|---|---|---:|---:|
| Go-net/http | `perf-2C-1B-1DB-8W-stairs-massive-microblog-20260516-162006` | 2,770.1 RPS, 5.21% failures | 4,437.8 RPS at 4,500 users with p99 at 300 ms, but not failure-free; 6,993.6 RPS pre-collapse with p99 p90 750 ms and failures |
| Python-Flask | `perf-2C-1B-1DB-8W-stairs-massive-microblog-20260516-163200` | 2,925.3 RPS, 1.96% failures | 4,395.0 RPS at 4,500 users under 1 s p99 with no failures in that step |
| Rust-Actix | `perf-2C-1B-1DB-8W-stairs-massive-microblog-20260516-164359` | 2,635.4 RPS, 0.23% failures | 5,885.0 RPS under 300 ms p99; 5,933.5 RPS under 1 s p99 |

## Per-environment details

- Go report: `Go-net-http/temp0.2-openapi-high_performance-openhands/sample0/semantic_optimization_report.md`
- Python report: `Python-Flask/temp0.2-openapi-high_performance-openhands/sample0/semantic_optimization_report.md`
- Rust report: `Rust-Actix/temp0.2-openapi-high_performance-openhands/sample0/semantic_optimization_report.md`

## Interpretation

The Rust semantic variant is the cleanest win: it materially improves the base Rust clean-step RPS while keeping persistence semantics intact.

The Python semantic variant also improves the low-latency, zero-failure region versus the base Python run.

The Go semantic variant improves pre-collapse throughput, but the base Go semantics already include transactional duplicate-like failures under this workload, and failures appear early in the staircase. Its result should be interpreted as higher throughput before collapse, not as a clean-step improvement.
