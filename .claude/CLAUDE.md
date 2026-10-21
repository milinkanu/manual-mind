# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Pre-implementation. The repo currently contains only `ManualMind — Engineering Spec.md` (the full design doc) and `Service Manual.pdf` (a sample printer service manual for ingestion/testing). No code, `docker-compose.yml`, or package manifests exist yet. **Read the spec file in full before writing any code** — it is the source of truth for architecture, schemas, and targets; this CLAUDE.md only summarizes it.

When bootstrapping the repo, follow the layout and milestone order defined in the spec (see "Repo layout and build milestones" section) rather than inventing a different structure.

## What ManualMind is

ManualMind turns a product service manual (PDF) into grounded, step-by-step repair/diagnosis SOPs, where every step cites a manual page and every visual is cropped from the manual's own figures (or annotated on a user's product photo). Scope for v1: printer service manuals (HP, Brother, Epson, Canon), one mode, no personas, no user accounts/billing, no generated "realistic" internals imagery, no bundled manual library, no mobile app.

## Planned repo layout

```
manualmind/
  api/            FastAPI app, routes, SSE
  ingest/         parse, figures, tables, linking, chunking, worker
  retrieval/      query analysis, hybrid search, rerank, graph expansion
  agents/         router, diagnosis, planner, verifier, visual, graph.py
  render/         figure crop + annotation, photo localisation
  schemas/        Pydantic models (SOP, chunks, events)
  db/             migrations, repositories
  llm/            client wrapper, tracing, model tiers
  eval/           golden set, runners, metrics, ablation reports
  web/            minimal React demo
  docker-compose.yml
  .github/workflows/ci.yml
```

Build order (each milestone ends with something runnable — don't skip ahead):
1. **Ingestion** — parse one printer manual end to end; ingest report shows figures/tables/links.
2. **Retrieval** — hybrid search + rerank + graph expansion; recall@8 on 50 labelled questions.
3. **Planner + Verifier** — structured SOP with citations; faithfulness measured; ablation with/without verifier.
4. **Diagnosis loop** — multi-turn troubleshooting with measurements; session checkpointing.
5. **Visuals** — figure crops with callout annotations; photo localisation for the part.
6. **Hardening** — streaming, caching, latency/cost targets, full 300-question eval in CI, README with ablation table + demo video.

Appliances (washing machines, refrigerators) are a post-milestone-6 generalisation test, not part of v1 scope.

## Architecture (two paths, one Postgres)

- **Ingest path** (async, once per manual): PDF → layout parse → section tree → figure extraction → table normalisation → linking (edges) → variant tagging → semantic chunking → indexing (dense + BM25). Runs on a worker queue (Redis + arq), never touched by the query path.
- **Query path** (per request): five LangGraph agents read the graph/index and stream only *verified* steps back over SSE. The raw PDF is never touched at query time — everything comes from the document graph built at ingest.

### Agent layer (LangGraph state machine, 5 nodes)

| Agent | Job |
| --- | --- |
| Router | Picks intent: lookup / diagnose / procedure. Known error code with one fix skips Diagnosis and goes straight to Planner. |
| Diagnosis | Walks the manual's troubleshooting data one question/measurement at a time; stops at confidence ≥ 0.8 or 6 turns. |
| Planner | Produces the SOP JSON — steps, tools, part numbers, specs, warnings, figure ids. Every step must carry citations. |
| Verifier | **The gate.** Checks each step is entailed by its cited chunk and numbers match exactly. Failed steps retry (max 2) then are dropped into `unverified_steps`. No step reaches the client unverified. |
| Visual | Crops the cited figure to the callout region and annotates it; if a user photo exists, a VLM localises/boxes the part. No figure and no photo → text-only step. |

No agent talks to the user except through the orchestrator. Session state (`manual_id`, model, observations, diagnosis history, current SOP) is checkpointed to Postgres so a session survives a restart.

**Model tiers.** One strong model for Diagnosis/Planner/Verifier; a smaller/faster model for Router and query analysis. All LLM calls go through a single client wrapper (`llm/`) that logs prompt, tokens, latency, and cost per node — do not call providers directly from agent code.

**Review gate (`VISUALS_MODE=gated|auto`).** During development the default is `gated`: Planner/Verifier run normally, figures are crop-and-callout-annotated with Pillow only (no image-model/VLM calls), and the session pauses in `awaiting_review` until a human approves/edits/rejects each step via the review endpoints. Only approved steps go to the Visual agent for image-model work. Rejections/edits become labelled examples for the eval golden set. `auto` mode skips the gate. A per-session `MAX_IMAGE_COST_USD` cap hard-stops image calls in both modes.

## Ingestion pipeline (detail)

1. Hash (SHA-256) + dedupe by `manual_id`.
2. Layout parse via Docling (fallback: PyMuPDF + Unstructured) → typed blocks (heading, paragraph, list, table, figure, caption) with bbox, page, reading order.
3. Section tree: heading hierarchy → `section_path` (e.g. `6 Disassembly > 6.3 Fuser unit > Removal`) on every block.
4. Figure extraction: crop at 200 DPI to object storage; VLM pass extracts callout numbers + pixel coords + a one-line description for embedding.
5. Table normalisation: rows become typed objects; tables classified as `parts_list`, `error_codes`, `troubleshooting`, `specs`, `other`.
6. Linking (edges): procedure→figure, figure callout→parts-list row, error code→troubleshooting procedure, cross-reference→target section, warning/caution→procedure it precedes.
7. Variant tagging: model applicability (e.g. "Models A/B only") via regex + LLM fallback → `models[]` attribute on blocks.
8. Chunking: by semantic unit (one procedure, one table row group, one figure+caption) — **not** by token count. Each chunk carries `section_path`, page, `models[]`, linked ids.
9. Indexing: dense embeddings + BM25 into search index; nodes/edges into graph tables.

Ingestion always emits a report (page count, figures found, callouts linked, unlinked references, parse confidence). **Unlinked references above 10% fail the job loudly** rather than indexing silently — preserve this behavior; don't swallow ingestion errors into a "degraded but silent" state.

## Retrieval pipeline (detail)

1. Query analysis extracts error codes, part names, model number, symptom phrases. Exact error codes/part numbers route to a **direct lookup**, bypassing vector search entirely.
2. Model filter: if model is known, exclude chunks whose `models[]` doesn't match.
3. Hybrid search: BM25 top 30 + dense top 30, fused with reciprocal rank fusion.
4. Rerank: cross-encoder (bge-reranker-v2) keeps top 8.
5. Graph expansion: one hop from each kept chunk (linked figures, callout parts, warnings, cross-referenced sections), capped at a token budget; warnings are always included regardless of budget.
6. Context pack: ordered, deduplicated, stable `chunk_id`s for downstream citation.

If the top reranked score is below threshold, return `insufficient_evidence` — **never let an agent improvise past this gate.**

## Data model

Single Postgres 16 + pgvector + tsvector — no separate vector DB or graph DB. Key tables: `manuals`, `blocks`, `figures`, `table_rows`, `chunks` (with `embedding vector(1024)`), `edges` (kind: `refers_to`, `callout_part`, `warns`, `fixes`, `see_also`), `sessions`, `traces`. Full schema and the SOP JSON contract (the shared contract between Planner, Verifier, Visual, and the API client) are in the spec's "Data models and SOP schema" section — the SOP schema should live in exactly one Pydantic module in `schemas/`, reused for LLM structured output, API responses, and eval fixtures.

## API surface

6 core endpoints, SSE-streamed query results (`question`, `step`, `warning`, `visual`, `insufficient_evidence`, `done` events) so steps appear as they pass verification. Two additional endpoints (`GET/POST /sessions/{id}/review`) exist only in `gated` mode. Full endpoint table is in the spec.

## Tech stack

Python end to end. FastAPI+SSE API, LangGraph orchestration, Claude (strong+fast tiers) via one client wrapper, Docling/PyMuPDF for PDF parsing, bge-m3 embeddings, bge-reranker-v2, Postgres 16+pgvector for everything (graph+vectors+full-text), Redis+arq for async ingest jobs, MinIO/S3-compatible object storage for figure crops, Pillow+OpenCV for annotation, Langfuse (self-hosted) for tracing, Docker Compose + GitHub Actions CI, minimal React demo client. Every choice is meant to be swappable behind an interface — don't hard-code a provider/library assumption into agent or pipeline logic where the spec implies swappability.

## Evaluation harness

A golden set (5 printer manuals × 60 questions = 300, mix of error-code lookup / procedure / multi-turn diagnosis / out-of-scope) runs in CI; a regression on any hard metric fails the build. Hard targets include part number/spec exact match = 1.00 (hard fail below), retrieval recall@8 ≥ 0.90, step faithfulness ≥ 0.95, citation accuracy ≥ 0.95 — see the spec's metrics table for the full list. Ablations to maintain and publish in the README: naive vs semantic chunking, no graph expansion vs one hop, no verifier vs verifier.

## Performance targets

p95 < 2s for error-code lookup end to end, p95 < 5s for first streamed step, p95 < 20s for a full 8-step verified SOP with visuals, < 10 min to ingest a 500-page manual, < $0.05 cost per query. Design choices that support this (keep these properties when implementing): steps stream as each passes the Verifier; figure crops are precomputed at ingest, not at query time; repeat queries hit a cache keyed on `(manual, model, normalised_query)`; visual rendering runs in parallel with verification of the next step.

## Failure handling conventions

- LLM call fails/times out → retry w/ backoff (3 tries), then fall back to the other model tier.
- Structured output invalid → re-ask once with the validation error, then fail the node (don't loop indefinitely).
- Verifier rejects a step twice → drop it into `unverified_steps`, surface to the user (don't silently omit).
- Retrieval below threshold → `insufficient_evidence`, never improvise an answer.
- Partial ingestion → index what parsed, mark manual `degraded`, surface the report (don't fail the whole ingest for a partial parse, but never hide the degradation).
