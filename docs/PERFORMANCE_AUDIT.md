# Export performance and query-plan audit

## Measured stages

The successful [2026-11-05 release run](https://github.com/Otzaria/SefariaExport/actions/runs/42053963528)
used this repository at `7ba4bb5983eb84011b7b6898405c81ba04463ed4` and
Sefaria-Project at `72be3076c9ea05917d8b1130c5827b7088b26c76`.
The following measurements come from that run's log, not a local benchmark.

| Stage | Observed duration |
| --- | ---: |
| Docker Compose pipeline | 32m25s |
| Mongo data restore | approximately 96s |
| Upstream index creation | 327s |
| Author export | approximately 0.025s between stage markers |
| Hebrew merged export | approximately 42s |
| Hebrew version export | approximately 11.2s including enumeration; progress timer reports 9s |
| Links export | 7m21s for 5,191,601 source links |
| Schemas | 54s |
| TOC | 2s |
| Archive compression | 662s |

The stage boundaries and rounded progress timers differ slightly, so these
figures should not be summed as an exact breakdown of the whole job. The job
also includes setup, download, verification, and other orchestration.

The existing compression worker count and job geometry already reflect earlier
benchmarks in `17_build_combined_archive.sh`. The links loop already streams a
Mongo cursor. Neither is rewritten by this audit. In particular, the small
version-enumeration and author stages do not justify building new indexes just
because a synthetic query plan contains `COLLSCAN`.

## Confirmed reliability gap

At the exact Sefaria-Project revision above,
[`ensure_indices()`](https://github.com/Sefaria/Sefaria-Project/blob/72be3076c9ea05917d8b1130c5827b7088b26c76/sefaria/system/database.py)
catches each `OperationFailure`, prints it, and continues. Its successful return
does not prove that the critical `links.refs.0` index was created. Previously,
`create_critical_indexes.py` nevertheless printed that all indexes were ensured.

A local reproduction on MongoDB **6.0.29**, with PyMongo **4.15.3** and 5,000
synthetic link documents, created an incompatible index named `refs.0_1` before
calling the actual upstream helper. The helper printed `IndexKeySpecsConflict`
and returned `None`. Explaining the exporter's unchanged
`find({}).sort({"refs.0": 1})` still produced `SORT -> COLLSCAN`.

After creating a usable index, the same query produced `FETCH -> IXSCAN`.
A descending index scanned backwards, and a compound index beginning with
`refs.0`, also produced plans without a blocking sort. This proves the failure
mode and the guard's acceptance criteria; it is not a production speedup claim.

## Guard after index creation

`create_critical_indexes.py` now requests `explain` with explicit
`verbosity: "queryPlanner"` and a 10-second server planning limit. It uses the
exporter's exact find/sort shape with no hint. It then checks the winning logical
plan, accepting classic and SBE wrappers and ignoring rejected alternatives.

A blocking `SORT`, an unrecognized plan, or an explain error stops the pipeline
before exports begin. A valid reverse or compound index is accepted. The log
reports index-creation elapsed time and the verified links plan's stages and index
names; it does not claim that every upstream index was validated.

`queryPlanner` does not execute the export query to completion, unlike
`executionStats` or the command's default `allPlansExecution` mode. This check
does not change the export cursor, add an index, use a hint, or write measurement
data into the deterministic release artifacts.

## Additional synthetic query measurements

On the same local Mongo version, with the canonical upstream index definitions,
3,000 synthetic text documents and 1,000 synthetic topic documents gave:

| Current query shape | Winning plan | Documents examined |
| --- | --- | ---: |
| Distinct text titles | `PROJECTION_COVERED -> DISTINCT_SCAN` on `title_1` | 0; 1,000 distinct keys |
| Hebrew title/license population | `PROJECTION_SIMPLE -> COLLSCAN` | 3,000 |
| Hebrew versions for 30 chosen titles | `FETCH -> IXSCAN` on `title_1` | 90; 72 returned |
| Author topic projection | `PROJECTION_SIMPLE -> COLLSCAN` | 1,000; 100 returned |

Only these local fixture probes used `executionStats`. Their cardinalities do
not represent the production dump, and they do not establish production latency
or a reason to add indexes. Live production `explain` output was not available
during this audit; subsequent normal runs will verify and log the actual links
plan after the index creation pass.

## Regression checks

```sh
python3 -m unittest -v test_critical_indexes.py
python3 -m unittest discover -v
```

The focused tests cover swallowed index creation errors, valid reverse and
compound indexes, classic/SBE/nested plans, rejected alternatives, empty `EOF`
plans, command errors, unknown explain output, and failure before a success log.
They are included in both the normal test workflow and the release preflight.
