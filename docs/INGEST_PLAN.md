# ManualMind `ingest/`: session-by-session build plan

## Context

The ingestion spike (`spike/`) proved that Docling plus Claude vision can extract figures, callouts and parts tables and link them (HP: 65/66 parts-table refs found; Canon: 73/76; Brother: no parts data). Its seven findings are in `docs/SPEC.md` under "Callout linking rules". The next step is the real ingestion pipeline (spec steps 1-9, plus report, search index and the upload API), built **one feature per session** so each session starts fresh from `CLAUDE.md` plus this file.

**How to use this file:** this is the working copy of the plan. Each session builds one item from the session map, ticks its box, and appends handoff notes in the "Handoff notes" section at the bottom. S0 is the first build session. Do not start S6 or later until S5's decision is recorded.

**Facts that shaped the plan** (checked, not assumed)
- The spike caches have page numbers but **no bounding boxes and no text blocks**, so S4 re-parses the three sample page ranges once to make new caches.
- Docling parse speed is **about 8-10 s per page** (HP 59 pages: 474 s). A 500-page manual takes about 67-83 min against a 10-min target. If parsing is to use about 60% of the budget (leaving the rest for vision, embeddings and the database), that needs about **0.7 s per page, a 12-14x improvement**. Nothing in this plan assumes that is achievable: S5 is a formal go/no-go checkpoint.
- Docling in the spike venv has `page_range`, `AcceleratorOptions`, `do_ocr`, `do_table_structure` and batch-size options. Their effect on speed and on accuracy is not measured.
- Local PostgreSQL 18 has no pgvector, so the database runs in Docker only. Docker Desktop is installed but **not running**: start it before S0's DB check.
- Spec order is vision (step 4) then tables (step 5). The spike showed tables must be classified **first**, so vision only runs on figures followed by a parts table (about 80% fewer calls).

## Decisions

Confirmed by you:
- **Code layout:** one Python package `manualmind/` at the repo root (`ingest/ schemas/ db/ llm/ api/ eval/ storage/`), tests in root `tests/`. `web/`, `docs/`, `data/`, `docker-compose.yml`, `.github/` stay at the root. The empty root `eval/` folder moves into the package.
- **Fixtures:** small JSON excerpts from the vendor manuals (headers, a few rows, recorded vision outputs) may be committed. Never the PDFs or page images. Check the GitHub repo's visibility.
- **Gate:** based on table coverage and unresolved text references, with the exact rules below.

### Quality gate (exact rules)

Two independent ratios. Each is always reported as **`unresolved / denominator`** (both numbers, never only a percentage).

| Ratio | What counts as one item (duplicates) | Unresolved when |
|---|---|---|
| Parts-table coverage | A unique `(merged table id, normalised ref)`. Blank refs, "Not shown" and section rows (`Fig.100`) are excluded. Several variant rows with the same ref in one table are **one** item. The same callout number appearing several times on a figure counts **once**. | No callout on any figure that the table follows names that ref. |
| Text references | A unique `(source block id, normalised target)` for "see Fig. X" and "see page N". The same target repeated in one block counts once; the same target in different blocks counts separately. | The figure caption or the page's section cannot be found. |

Failure rule, applied per ratio:
- **Denominator at least `GATE_MIN_SAMPLE` (default 20):** fail the job when `unresolved / denominator` is **above** `GATE_MAX_UNRESOLVED` (default 10%). Exactly 10% passes.
- **Denominator below 20 (small sample):** never fail on the percentage (1 of 5 is 20% but proves little). Mark the manual `degraded` and show the counts when 2 or more are unresolved.
- **Denominator 0:** that ratio is "n/a". No parts tables at all gives status `ready_no_parts_data` (not a failure); no text references means that ratio is n/a.
- Boundary cases that must be tests: 1/5, 2/19, 2/20 (pass), 3/20 (fail), 2/21 (pass), 3/21 (fail). Spike reality check: HP 1/66, Canon 3/76, both pass.
- Both settings live in `Settings`; failed parse pages are a separate `degraded` rule, not part of these ratios.

### Re-ingestion integrity (exact rules)

**Every ingestion replaces the whole manual snapshot. There is no incremental update.**
- Stages S3-S14 build an `IngestBundle` (blocks, figures, rows, edges, chunks with vectors, report), held in memory and optionally dumped to disk. **Nothing touches the live tables until the final `publish` step.**
- `publish(manual_id, bundle)` runs in **one transaction** under `pg_advisory_xact_lock(manual_id)`: delete all child rows of the manual (blocks, figures, table_rows, edges, chunks and their embeddings), insert the new snapshot, then set `manuals.status`, `ingest_report` and `active_run_id`. Any error rolls back and the previous snapshot is untouched. Readers see the old snapshot or the new one, never a mix.
- The gate runs **before** publish. A failed gate publishes nothing: the previous snapshot (if any) stays live, the failed run's report is stored in `ingest_runs`, and a debug copy of the bundle goes to the object store as JSON, never into search tables. For a manual never published, `manuals.status` is `failed`.
- Two concurrent ingests of one manual are serialised by the advisory lock and a unique active job per manual.
- Figure crops are written under `figures/{manual_id}/{run_id}/...` before publish. After a successful publish the old run's prefix is deleted; after a failed run its prefix is deleted.
- New table `ingest_runs(id, manual_id, status, report jsonb, bundle_uri, started_at, finished_at)`; `manuals.active_run_id`. `GET /manuals/{id}` returns the live snapshot's status plus the latest run's status.
- Tests (S1, S15, S16): re-ingest unchanged gives identical counts and ids; re-ingest after removing a figure, a row, an edge and a chunk leaves none of them; an error injected mid-publish leaves the old snapshot intact; a gate failure changes no rows; a concurrent ingest waits or is rejected.

### Other defaults (tell me in review if you want any changed)

| Topic | Default |
|---|---|
| Dependencies | `uv`, one root `pyproject.toml` + `uv.lock`, Python >= 3.12. Groups: core, `parse` (docling, pymupdf), `dev`. Docling is imported lazily so CI and unit tests install without it. |
| Database | psycopg 3 + `pgvector` package + numbered plain-SQL migrations (`db/migrations/0001_*.sql`) with a ~40-line runner. Alembic is a possible later swap. |
| Postgres | `pgvector/pgvector:pg16` in docker-compose on **host port 5433** (avoids your local PG 18 on 5432). |
| IDs | `manual_id = uuid5(NAMESPACE, sha256)` so dedupe is free; child ids deterministic (`uuid5(manual_id, "kind:page:ordinal")`), so an unchanged re-ingest yields identical ids. (Stale rows are handled by the snapshot replacement above, not by the ids.) |
| Config | `pydantic-settings` reading `.env`; committed `.env.example`. |
| Object storage | `ObjectStore` interface; local folder `data/objectstore/` first, MinIO in the API session. |
| LLM wrapper | `llm/client.py`: one `LLMClient.call(...)` for all Claude calls: retry 3x, tier fallback, one re-ask on bad structured output, a trace row per call (tokens, latency, cost). Per-model capability flags (the vision model rejects forced `tool_choice` and `temperature`). `FakeLLMClient` for tests. |
| Caching | Docling output cached per page chunk keyed by `(sha256, PARSER_VERSION, first_page, last_page)`, storing our own serialised blocks with bboxes. Vision cached by `(image_sha256, PROMPT_VERSION, model)`. Versions bumped by hand (the spike cache ignored code changes). |
| Status values | `queued, running, ready, ready_no_parts_data, degraded, failed`; details in `ingest_report`. `degraded` = partial parse or small-sample warning, still published. |
| Spec data-model additions (recorded in SPEC.md in S1) | `ingest_runs`, `manuals.active_run_id`, `edges.attrs jsonb`, `table_rows.table_id/row_idx`, `figures.bbox/description/image_w/image_h`, `blocks.reading_order`, `chunks.kind/section_path/page_start/page_end/meta`, `traces.manual_id`, and an `ingest_jobs` table. |
| Test markers | `unit` (default), `db` (needs Postgres), `llm` (needs a key, costs money), `pdf` (needs `data/manuals/`), `slow` (runs Docling), `e2e` (real PDF through to the search index). `--strict-markers`; `conftest.py` auto-skips a test whose prerequisite is missing. CI runs `-m "not db and not llm and not pdf and not slow and not e2e"` until S14 adds a Postgres service; `e2e` runs locally and in a manual or nightly CI job. |

## Session map (tick in `docs/INGEST_PLAN.md`)

**S5 is a hard checkpoint: S6 and everything after it do not start until S5's decision is recorded and the targets in SPEC.md are updated.**

| | Session | Spec step | Order |
|---|---|---|---|
| [ ] S0 | Skeleton and tooling | - | first |
| [ ] S1 | Schemas, database, snapshot `publish` | data model | after S0 |
| [ ] S2 | LLM wrapper and tracing | client wrapper | after S1, before S8 |
| [ ] S3 | Intake (hash, dedupe, store) and pipeline skeleton | 1 | after S1 |
| [ ] S4 | Docling parse into blocks (bbox, reading order), cached | 2 | after S3 |
| [ ] **S5** | **Parse-speed go/no-go checkpoint** | perf target | **right after S4, blocks S6+** |
| [ ] S6 | Section tree | 3 | after S5; S6 and S7 either order |
| [ ] S7 | Table normalisation and classification, multi-page tables | 5 | after S5 |
| [ ] S8 | Figure crops, parts-table association, vision pass | 4 | after S2, S7 |
| [ ] S9 | Callout -> part linking | 6 | after S8 |
| [ ] S10 | Text-reference links (see Fig., see page, warnings) | 6 | after S6, S8 |
| [ ] S11 | Error code -> troubleshooting links | 6 | after S6, S7 |
| [ ] S12 | Model-variant tagging | 7 | after S6, S9 |
| [ ] S13 | Semantic chunking | 8 | after S6, S9, S10, S12 |
| [ ] S14 | Index: embeddings (benchmarked), tsvector, lookup indexes | 9 | after S13 |
| [ ] S15 | Ingest report, quality gate, atomic publish, full CLI pipeline | report | after S3-S14 |
| [ ] S16 | **End-to-end acceptance test on one real manual** | - | after S15, before the API |
| [ ] S17 | Worker (arq), upload API with input hardening, MinIO | API | after S16 |
| [ ] S18 | Hardening A: failure paths, other manuals (Epson file), delete `spike/` | - | after S16 |
| [ ] S19 | Hardening B: full-manual run, speed and cost proof, CI | perf | after S5, S17 |

Pipeline stage order in the final CLI: intake, parse, sections, tables, figures + vision, linking (callouts, text, errors), variants, chunk, embed, **gate**, **publish**.

## Session details

Each entry: goal, files, tests, **done when**, risk. "Reuse" means port from the spike (`spike/docling_extract.py`, `vlm_callouts.py`, `linking.py`, `tests/test_linking.py`).

**S0 Skeleton and tooling.** Goal: a runnable empty project with tests, DB container and CI. Files: `pyproject.toml`, `uv.lock`, `manualmind/{ingest,schemas,db,llm,api,eval,storage}/__init__.py`, `manualmind/config.py`, `tests/conftest.py` (marker auto-skip), `tests/test_smoke.py`, `docker-compose.yml` (db only), `.env.example`, `.github/workflows/ci.yml` (uv sync without `parse`, unit tests), CLAUDE.md commands. Done when: `uv sync` and `uv run pytest` green; after Docker Desktop is started, `docker compose up -d db` and `psql -p 5433 -c "create extension vector"` work; CI YAML lints. Risk: Docker off, so the DB check is marked pending you.

**S1 Schemas, database, snapshot publish.** Goal: the data model exists as Pydantic types and Postgres tables, with an atomic snapshot-replace operation. Files: `schemas/ingest.py` (`BBox, BlockType, Block, Figure, Callout, TableKind, TableRow, Chunk, EdgeKind, Edge, ManualStatus, IngestReport, IngestBundle`), `db/migrations/0001_init.sql` (tables, `vector` extension, HNSW and GIN indexes, `ingest_runs`), `db/migrate.py`, `db/pool.py`, `db/repos/*` (`get/list_by_manual`), **`db/publish.py` (`publish_snapshot(manual_id, bundle)`: advisory lock, delete children, insert, update manual, all in one transaction)**, SPEC.md data-model update. Tests: unit (validation, id function); `db` (migrate twice is a no-op; publish round trip; unchanged re-publish gives identical rows; re-publish with fewer figures/rows/edges/chunks leaves no stale rows; an error injected mid-publish rolls back and the old snapshot is intact; cascade delete of a manual; second concurrent publish waits). Done when: `uv run pytest -m "unit or db"` green. Risk: over-designing the schema; add only the columns listed above.

**S2 LLM wrapper and tracing.** Goal: every Claude call goes through one wrapper that writes a trace row. Files: `llm/client.py`, `llm/pricing.py` (hand-edited prices), `llm/tracing.py`, `llm/fake.py`. Reuse `prepare_image` (1568 px cap) and the `USAGE` token idea, now per call. Tests: unit with a fake SDK (retry, fallback, re-ask once); `db` (trace row written); one `llm` smoke call. Done when: unit tests green and the smoke call records tokens and cost. Risk: model quirks scattered as `if`s, so use capability flags.

**S3 Intake and pipeline skeleton.** Goal: hash, dedupe and store a PDF with a runnable CLI that later sessions extend. Files: `ingest/hashing.py` (reuse `sha256_file`), `storage/base.py` + `storage/local.py`, `ingest/intake.py` (`intake(path, store, repo) -> IntakeResult(manual_id, is_duplicate, page_count)`; creates the `manuals` and `ingest_runs` rows only), `ingest/pipeline.py` (tiny `Stage` protocol that reads and extends the `IngestBundle`, `PipelineContext`, runner with per-stage timings, `--stages`, `--pages`; reuse `parse_page_range`), `ingest/__main__.py`. Tests: unit (hash, page range, synthetic PDF built with PyMuPDF, corrupt PDF gives a clear error); `db` (same file twice gives one `manual_id` and one `manuals` row). Done when: running the CLI twice on a sample prints `duplicate` the second time. Risk: the stage interface is hard to change later, so keep it tiny.

**S4 Docling parse into blocks.** Goal: typed blocks with page, bbox and reading order, plus raw figure images and tables, cached per page chunk. Files: `ingest/parse/base.py` (`PdfParser` protocol, `ParsedChunk`), `docling_parser.py` (reuse `convert_pdf` and extraction; add bbox in top-left points, label mapping to `BlockType`, reading order), `cache.py`, `ingest/stages/parse.py`, `scripts/make_fixtures.py`. Default: slice the PDF to the chunk with PyMuPDF (known to work); try `page_range=` as an experiment and verify whether `prov.page_no` is absolute. Tests: unit (label mapping and bbox on a recorded Docling dict; a cache hit never imports Docling); `slow` (synthetic 2-page PDF); `pdf` (re-parse HP 1270-1328, Canon 1-46, Brother 118-178; `make_fixtures.py` writes small committed excerpts). Done when: the three ranges parse; figure/table counts match the spike (HP 52/29, Canon 32/40, Brother 71/5, within 2); blocks have bboxes; a rerun from cache takes under 5 s. **The default-settings output is saved as the accuracy baseline that S5 compares against.** Risk: Docling labels/bboxes differ from assumptions; about 24 min of compute, once.

**S5 Parse-speed go/no-go checkpoint.** Goal: decide, with measurements, whether and how the 10-minute / 500-page target can be met, **before anything else is built around it**. No assumption is made that multiprocessing, turning OCR off, or a text-only fast path will work.
- *Representative sample:* about 30 pages stratified by type from all three manuals (HP, Canon, Brother): text-only pages, table-heavy pages, figure-heavy pages, parts-chapter pages; include any page with scanned or image-only content if one exists.
- *Configurations measured, one change at a time against the S4 default:* OCR off, fast table mode, batch sizes, thread count, 1/2/4 worker processes, GPU if available, and a PyMuPDF text-only fast path for pages with no images or tables.
- *Measure speed AND accuracy for each:* seconds per page by page type, peak memory, and against the S4 baseline: figure count, table count, **parts-table row recall and cell-level equality on the parts tables**, block count and text similarity. A configuration is rejected if it loses any parts-table row or any figure on the sample; other losses are recorded and shown to you.
- *Budget check:* estimate the full 500-page time from the measured per-page numbers plus the vision and embedding estimates (about 2,200 input tokens per vision call, S14 embedding latency once known).
- *If no safe configuration reaches the target,* compare, with numbers, at least these alternatives: (a) **parse only the relevant chapters** (a cheap PyMuPDF text scan picks parts, error/troubleshooting and procedure chapters; measure how many relevant pages it misses); (b) **asynchronous ingestion** (publish the most useful chapters first, finish the rest in the background; the target becomes "time to first searchable" plus an agreed total); (c) **hardware acceleration** (GPU or a hosted parse service, with the cost and deployment consequences). Present the comparison table and recommendation.
- *Deliverables:* `ingest/parse/parallel.py` and `ParserConfig` only for the chosen option; `scripts/bench_parse.py`; a results table in the handoff notes; **your recorded decision**; and the updated Performance targets in `docs/SPEC.md`.
- Tests: unit (chunk planning, in-order merge, config validation); `slow` benchmark writing a JSON results file.
- Done when: results table and your decision are recorded, SPEC.md targets match the decision, and the chosen configuration is the parser default with its accuracy impact stated. **Stop and wait for your go/no-go before S6.** Risk: high.

**S6 Section tree.** Goal: every block gets a `section_path`; procedure nodes identified. Files: `ingest/sections.py` (`build_section_tree`, `assign_section_paths`): use the PDF outline (`get_toc()`) when present (check HP and Brother first), else numbering depth (`6`, `6.3`, `6.3.1`), else heading order; mark leaf sections with list/step blocks `is_procedure`; strip repeated page headers/footers. Tests: unit on recorded HP/Canon/Brother blocks plus synthetic missing-level cases; a handful of known headings. Done when: at least 95% of non-header fixture blocks have a path and 5 hand-checked paths match. Risk: Docling headings may lack levels.

**S7 Table normalisation and classification.** Goal: typed row objects, correctly classified, multi-page tables merged. Files: `ingest/tables.py` (`normalise_table`, `classify`, `merge_continuations`: next page, same column count, repeated or missing header, inherits kind and header; the merged table gets one `table_group_id` used by the gate). Port `classify_table`, `find_columns`, `ref_column_is_numeric`, `NOT_SHOWN`, `_remarks`. Flag `Fig.100`-style rows `is_section_row`. Row `data` carries `ref, part_number, description, remarks`. Tests: port the 11 spike tests; unit on header variants, "Not shown", `Fig.100`, supplies table rejected; Canon pp.18-19 becomes one table. Done when: parts-table counts match the spike on fixtures; Brother has 0 parts tables. Risk: continuation pages without a header (the Canon fixture is the test).

**S8 Figures: crops, parts-table association, vision pass.** Goal: crop every figure; vision only on figures followed by a parts table. Files: `ingest/figures.py` (`save_crops` 200 DPI via `ObjectStore` under the run-scoped prefix, record width/height; `assign_parts_tables`: walk blocks in `(page, reading_order)` order and give each figure every parts table up to the next figure; skip crops under 100 px), `ingest/vision.py` (port `CALLOUT_TOOL`/`CALLOUT_PROMPT`, style-neutral, add a `description` field; `extract_callouts(img, llm)`; prompt version constant). Tests: unit (association on recorded orders: Canon p17 -> tables pp18-19; HP figures; `FakeLLMClient`; cache hit); `pdf`+`llm` (vision on 3 HP figures). Done when: vision call count is HP 9 +/-1, Canon 7 +/-1, Brother 0; a rerun makes 0 API calls. Risk: ordering on pages with several figures and tables; prompt drift (record outputs).

**S9 Callout -> part linking.** Goal: `callout_part` edges keeping every variant row, plus the coverage numbers the gate uses. Files: `ingest/linking/callouts.py` (`link_callouts(figures, tables, assoc)` creating `Edge(kind=callout_part, attrs={callout, x, y, remarks})`; port `refs_match` and keep-all-hits; drop the one-page window), `ingest/linking/metrics.py` (`parts_table_coverage` returning `unresolved` and `denominator` by the exact counting rules above, plus `numeric_match`). Unmatched labels like `A##` stay on the figure with no edge. Tests: ported linking tests; **replay** test from committed recorded vision outputs (HP, Canon); duplicate-counting tests (variant rows, repeated callouts); Brother gives no edges and coverage n/a; `llm` live test with a looser threshold. Done when: replay gives HP coverage >= 65/66 and numeric >= 68/87, Canon >= 73/76 and >= 120/123, Canon callout 9 has 5 edges; live run >= 95% coverage on both. Risk: numbers shift when the association rule differs from the spike's one-page window; compare and document.

**S10 Text-reference links.** Goal: procedure -> figure, cross-reference, warning -> procedure edges, with the text-reference counts the gate uses. Files: `ingest/linking/textrefs.py` (`find_figure_refs` for "see Fig. 6-12"/"Figure 1-2239", `resolve_figure` against captions, `find_page_refs`/`resolve_page` to a target section, `link_warnings`; counting by the exact rules above). Edge kinds `refers_to`, `see_also`, `warns`. Name-labelled figures (Brother) link to the enclosing or naming procedure. Tests: unit on regex variants, duplicate counting and synthetic blocks; HP excerpt with real captions. Done when: a hand-listed set of 10 refs resolves, and every captioned Brother fixture figure gets a procedure link. Risk: many HP figures have no caption; count unresolved refs honestly.

**S11 Error code -> troubleshooting links.** Goal: `fixes` edges and a direct-lookup structure. Files: `ingest/linking/errors.py` (`extract_error_codes`: patterns like `50.2`, `E4`, `13.20.01` built per manual from `error_codes` tables; `link_error_codes`). Tests: unit on formats and false positives (section numbers like `6.3`). The spike ranges are parts chapters, so **first find an HP/Canon page range with an error-code table** and fixture it. Done when: on one real range at least 80% of table codes link to a procedure and the false-positive list is empty on fixtures. Risk: formats vary by vendor; first place depending on an unseen page range.

**S12 Model-variant tagging.** Goal: `models[]` on blocks and rows, plus manual title/brand/models. Files: `ingest/variants.py` (`extract_manual_meta` from the first pages; `tag_models`: regex for "Models A/B only", "except", "M426/M427 only" in headings applying to the whole section, plus the Canon remarks column; LLM fallback via S2 only on trigger-word blocks the regex missed, cached). Tests: table-driven 20 phrases; fake-LLM test that the fallback fires only for ambiguous blocks; `llm` test with 3 cases. Done when: 20 phrases pass at 100% and under 5% of HP fixture blocks reach the LLM. Risk: over-tagging hides content; prefer empty `models[]` (meaning all models) when unsure.

**S13 Semantic chunking.** Goal: chunks by semantic unit with provenance. Files: `ingest/chunking.py` (`chunk_manual`: one procedure = one chunk, split on step boundaries only above a token limit with the heading repeated; one chunk per parts-row group per figure; one figure + caption + description = one chunk; each carries `section_path`, pages, `models[]`, `block_ids`, linked ids), plus `chunk_naive` (fixed windows) for the ablation baseline. Tests: unit on synthetic trees (no block lost or duplicated, size limit held, a warning stays with its procedure or is linked). Done when: on HP and Canon fixtures 100% of blocks are covered by exactly one non-figure chunk and every parts row is in a row-group chunk. Risk: very long procedures, page-header noise.

**S14 Index and embedding benchmark.** Goal: embeddings plus a text index plus exact-lookup indexes, with the local-versus-hosted embedding choice made on measurements. Files: `ingest/embed.py` (`Embedder` protocol, `BgeM3Embedder` kept behind it, `FakeEmbedder`), `ingest/stages/index.py` (computes vectors into the bundle; they reach the database only through `publish`), migration `0002_search.sql` (tsvector with the `simple` config so part numbers like `RM1-4554` aren't stemmed, GIN, HNSW cosine, indexes on part number and error code), a Postgres service added to CI with `db` tests on, `scripts/bench_embed.py`. **Benchmark before choosing:** for local bge-m3 measure latency per chunk and per batch, peak memory and total time for the HP + Canon fixture chunks; measure **retrieval quality** on about 20 hand-written queries (part numbers, error codes, phrases, symptom wording) as hit@5 against known chunks; run the same queries through a hosted embedding API if you provide a key, and compare cost. Record the decision. Tests: unit (batching/normalisation); `db` (part-number lookup returns its row; tsvector finds a known phrase); `slow` (real bge-m3). Done when: the benchmark table and decision are recorded, chunk count equals vector count, and a known HP part-number lookup returns in under 50 ms. Risk: CPU embedding time may push the full-ingest budget; feed this back into S5's budget.

**S15 Report, quality gate, atomic publish.** Goal: the full pipeline runs end to end from the CLI and ends with an honest report and status, publishing atomically. Files: `ingest/report.py` (`build_report`: pages, figures, vision calls/tokens/cost, the two gate ratios as `unresolved/denominator`, parse confidence, stage timings), `ingest/gate.py` (`evaluate_gate` implementing the exact rules above), final stage order ending `... embed, gate, publish`. Partial parse = `degraded`; zero parts tables = `ready_no_parts_data`; small-sample warnings = `degraded` with counts. A failed gate stores the report in `ingest_runs` and a debug bundle in the object store and publishes nothing. Tests: unit on made-up reports (all boundary cases listed above; Brother-like -> `ready_no_parts_data`; one failed chunk -> `degraded`); `db` (failed gate changes no rows; re-ingest replaces the snapshot, stale rows gone; old run's crops removed); `pdf` end-to-end on HP 1270-1328 with `FakeLLMClient`. Done when: the CLI prints a readable report for HP and Canon (`ready`), Brother (`ready_no_parts_data`), and a deliberately broken input (`failed`, reason shown). Risk: gate definition drift; the table above is the contract.

**S16 End-to-end acceptance test (before any upload API).** Goal: prove that one real manual goes from the original PDF to a searchable index reliably. Default manual: the Canon parts catalog (46 pages, smallest with parts data), real Docling, real vision (a recorded-vision mode exists for CI). Marker `e2e`. It verifies:
- **Provenance:** every chunk's `block_ids` exist and carry page numbers; every edge endpoint exists; every figure `image_uri` resolves to a stored crop; a chunk's cited page really contains its text.
- **Part-number lookup:** a hand-verified Canon part number returns the right row, with its model remarks, within the latency target.
- **Embeddings:** one vector per chunk, correct dimension, none null, and a semantic query returns a known chunk.
- **Gate results:** the report's `unresolved/denominator` values match the expected numbers within a stated tolerance.
- **Re-ingestion:** a second run is a no-op with identical ids; a run on a modified bundle (a figure, a row, an edge and a chunk removed) leaves none of them behind.
- **Failure safety:** forcing a gate failure, and separately injecting an error at publish, never leaves partially published search results; lookups and searches return the previous snapshot or nothing.
Done when: the test passes locally from a clean database, its run time and cost are recorded, and it is documented how to run it (including a CI job that is manual or nightly). No API work starts before this is green. Risk: real-manual flakiness (vision variance); keep the recorded-vision mode as the deterministic variant.

**S17 Worker, upload API, MinIO.** Goal: upload over HTTP and poll status, treating every upload as untrusted input. Files: `api/app.py`, `api/routes/manuals.py` (`POST /manuals`, `GET /manuals/{id}`), `ingest/worker.py` (arq job calling the same pipeline), `storage/s3.py`, Redis and MinIO in `docker-compose.yml`, `ingest_jobs` for job state. Hardening, all configurable in `Settings` and all tested:
- Streamed upload to a temp file with a **size limit** (default 500 MB, large enough for the 465 MB sample; configurable) and an **upload timeout**; never read the whole file into memory.
- **PDF validation:** `%PDF-` magic bytes, opens with PyMuPDF in a **subprocess with a timeout**, not encrypted, page count within `MAX_PAGES`; reject otherwise with a clear message.
- **No client-supplied paths:** stored under a generated name from the SHA-256; the filename is only metadata.
- **Resource limits:** per-job wall-clock timeout, worker concurrency limit (default 1-2), container memory limits in compose, parse runs in a separate process.
- **Retry and idempotency:** arq `max_tries` (default 2) with backoff; the job id is derived from `manual_id` plus the pipeline version, so retries and duplicate uploads do not start a second ingest; one active ingest per manual (advisory lock); a retried job resumes from caches and publishes atomically.
Tests: FastAPI `TestClient` with queue/DB mocked (oversize, not-a-PDF, encrypted, timeout, traversal filename, duplicate upload returns the existing `manual_id`); `db` dedupe and one-active-job; `slow` integration with a tiny synthetic PDF. Done when: `curl -F file=@x.pdf /manuals` then `GET /manuals/{id}` shows queued -> running -> ready with the report, and each hardening test passes. Risk: Windows + Redis + arq; run the worker in compose, not natively.

**S18 Hardening A.** Goal: survive manuals and failures we haven't seen. Files: PyMuPDF `PdfParser` fallback used per chunk when Docling raises; tests for scanned pages, an encrypted PDF, an empty page range, a vision outage (no callouts for that figure, status `degraded`, no crash). Run the Epson file (actually an Artisan 800 manual) as a generalisation test and write down what broke. Delete `spike/`; update CLAUDE.md and SPEC.md so the callout rules point at `ingest/`. Done when: the four failure tests pass, Epson reaches an honest status, `spike/` is gone and the suite is green. Risk: unknown manual shape; time-box and log follow-ups.

**S19 Hardening B.** Goal: prove performance and cost against the targets agreed in S5. Files: `scripts/full_run.py`; run the full 1,328-page HP manual plus full Canon and Brother; record total time, time per stage, vision calls and cost, chunk counts; tune worker counts; add a CI job for replay/golden-fixture tests. Done when: a 500-page-equivalent run meets the S5-agreed target and cost per manual is reported. Risk: the speed target.

## Per-session routine

Start: read `.claude/CLAUDE.md` and `docs/INGEST_PLAN.md` including the handoff notes; read the SPEC sections for the session's steps; `git status` and run the unit tests for a clean base; build **only** this session's feature.
End: run `uv run pytest -m "not db and not llm and not pdf and not slow and not e2e"` plus any marked tests the session needs; one commit with a clear message (push only when asked); tick the box; append 3-5 lines of handoff notes (what exists, commands, gotchas, measured numbers); update CLAUDE.md if commands/paths changed; report in plain language: what happened, what it means, what's next.

## Out of scope for ingest

Retrieval (hybrid search, rerank, graph expansion, query-time model filtering); agents, LangGraph, sessions, SOP and SSE schemas; rendering/annotation and photo localisation (`render/`, `/figures/{id}/render`); every API endpoint except `POST /manuals` and `GET /manuals/{id}`; Langfuse, the eval harness and golden set, the web demo, the review gate; generated images, accounts, billing, appliance manuals.

## Decisions deferred to the session that needs them

- **S5 (hard gate):** which speed option, and whether the 10-minute target changes; your go/no-go.
- **S14:** local bge-m3 versus a hosted embedding API, on measured latency, memory and retrieval quality.
- **Anytime:** psycopg + SQL migrations (default) versus SQLAlchemy + Alembic.

## Verification of this plan

- After approval: `docs/INGEST_PLAN.md` exists with the same content and unticked boxes; `.claude/CLAUDE.md` points to it.
- Every session has a measurable "done when" tied to spike numbers or a CLI behaviour; sessions are small enough for one sitting (S4 and S5 are the heaviest because of Docling run time).
- The four acceptance points you required map to: S5 (go/no-go with accuracy), the Quality gate table and S15 (exact denominators), the Re-ingestion integrity rules with S1/S15 tests, and S16 (end-to-end before the API); S14 and S17 carry the two smaller recommendations.
- First real check of the plan itself: S0 and S1 should finish without changing the session map; if S4 shows Docling bboxes or labels differ from assumptions, update S6-S8 in the plan before continuing.

## Handoff notes

Append 3-5 lines per finished session: what exists, commands, gotchas, measured numbers.

(none yet)
