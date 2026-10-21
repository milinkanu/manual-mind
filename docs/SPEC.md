# ManualMind — Engineering Spec

Oct 19, 2026 · @milin

## Overview

ManualMind turns a product manual (owner's or service) into grounded, step-by-step repair and diagnosis guides, where every step cites a manual page and every visual comes from the manual's own figures or the user's product photo.

**Problem.** Manuals run 100–800 pages, split procedures across sections, and reference diagrams by callout number. Finding the right procedure, the right figure and the right part number takes a technician 10–30 minutes per job.

**What it does.**

1. Ingests a PDF manual into a document graph: text, tables, figures, callouts, parts lists, cross-references, model variants.
2. Takes a query ("E4 error, drum not spinning") plus an optional product photo.
3. Runs a diagnosis loop when the cause is unclear, asking for observations or measurements and branching on the manual's troubleshooting data.
4. Produces a structured SOP: ordered steps, tools, part numbers, specs, warnings, each step tied to a page and a figure.
5. Verifies every step against the source before returning it.
6. Renders visuals by cropping and annotating manual figures, or by marking the part on the user's photo.

**Scope.** One mode, no personas. Printers first (HP, Brother, Epson, Canon service manuals), then appliances.

**Non-goals.**

- No user accounts, billing, or multi-tenant admin.
- No generative "realistic" images of internals. Generated illustrations are out of v1.
- No bundled manual library. Users upload their own PDFs.
- No mobile app. A minimal web client exists only to demo the API.

## Architecture

Two paths share one Postgres: an async ingest path that builds the document graph once per manual, and a query path where five agents read that graph and stream only verified steps.

&#91;embedded content: system architecture · ingest path and query path\]

The Verifier is the gate: a step reaches the client only after its claims and numbers match the cited manual text. A direct error-code lookup skips Diagnosis and goes straight to the Planner.

## Ingestion pipeline

Ingestion converts a PDF into a typed document graph once, asynchronously, so queries never touch the raw PDF.

1. **Hash and dedupe.** SHA-256 of the file. Same hash returns the existing `manual_id`.
2. **Layout parse.** Docling (fallback: PyMuPDF + Unstructured) yields blocks with type, bbox, page and reading order: heading, paragraph, list, table, figure, caption.
3. **Section tree.** Build heading hierarchy (chapter → section → procedure). Each block gets a `section_path` like `6 Disassembly > 6.3 Fuser unit > Removal`.
4. **Figure extraction.** Crop each figure at 200 DPI to object storage. A VLM pass extracts callout numbers and their pixel coordinates, plus a one-line description for embedding.
5. **Table normalisation.** Tables become row objects with typed columns. Classify each table: `parts_list`, `error_codes`, `troubleshooting`, `specs`, `other`.
6. **Linking.** Create edges:
   - procedure → figure ("see Fig. 6-12")
   - figure callout → parts-list row (callout 14 → part RM1-4554)
   - error code → troubleshooting procedure
   - cross-reference ("see page 47") → target section
   - warning/caution block → the procedure it precedes
7. **Variant tagging.** Detect model applicability ("Models A/B only") with regex plus an LLM fallback, stored as a `models[]` attribute on blocks.
8. **Chunking.** Chunk by semantic unit, not token count: one procedure, one table row group, one figure plus caption. Each chunk carries `section_path`, page, models and linked ids.
9. **Index.** Dense embeddings and BM25 into the search index; nodes and edges into the graph tables.

Ingestion emits a report: page count, figures found, callouts linked, unlinked references, parse confidence. Unlinked references above 10% fail the job loudly instead of indexing silently.

## Retrieval

Retrieval is hybrid search plus graph expansion, so a hit on one chunk pulls in the figure, parts rows and warnings it depends on.

1. **Query analysis.** Extract structured signals: error codes, part names, model number, symptom phrases. Exact error codes and part numbers route to a direct lookup before any vector search.
2. **Model filter.** If the model is known (from the query, the photo or session state), filter out chunks whose `models[]` excludes it.
3. **Hybrid search.** BM25 top 30 and dense top 30, fused with reciprocal rank fusion.
4. **Rerank.** Cross-encoder reranker keeps the top 8.
5. **Graph expansion.** For each kept chunk, follow edges one hop: linked figures, callout parts, warnings, cross-referenced sections. Cap the context at a token budget, warnings always included.
6. **Context pack.** Return an ordered, deduplicated bundle with stable ids so every downstream claim can cite `chunk_id` and page.

If the top reranked score falls below a threshold, retrieval returns `insufficient_evidence` and the system says the manual doesn't cover it rather than guessing.

## Agent layer

Five agents run as nodes in one LangGraph state machine; each has a typed input, a typed output and a single job. No agent talks to the user except through the orchestrator.

| Agent | Input | Output | Job |
| --- | --- | --- | --- |
| Router | query, session state | `intent`: lookup / diagnose / procedure | Picks the path. Error code with one known fix skips diagnosis. |
| Diagnosis | symptoms, context pack, answers so far | next question, or `cause` with confidence | Walks the manual's troubleshooting data. Asks for one observation or measurement at a time, compares readings to spec, stops at confidence ≥ 0.8 or after 6 turns. |
| Planner | cause or task, context pack | `SOP` JSON | Orders steps, attaches tools, part numbers, specs, warnings and figure ids. Every step must carry citations. |
| Verifier | SOP, context pack | pass, or failures per step | Checks each step is entailed by its cited chunk, numbers match the source exactly, warnings are present, and no step cites nothing. Failed steps go back to the Planner, max 2 retries, then the step is dropped and flagged. |
| Visual | SOP, figures, user photo | annotated image per step | Picks the cited figure, crops to the callout region, draws arrows and step numbers. If a photo exists, a VLM localises the part and boxes it. No figure and no photo means a text-only step. |

**State.** Session state holds `manual_id`, model, collected observations, diagnosis history and the current SOP. It is checkpointed to Postgres so a session survives a restart and can resume mid-diagnosis.

**Model choice.** One strong model for Diagnosis, Planner and Verifier; a smaller, faster model for Router and query analysis. All calls go through one client wrapper that logs prompt, tokens, latency and cost per node.

**Review gate (development).** A config switch, `VISUALS_MODE=gated|auto`, holds back every image-model call until a human approves the SOP. In `gated` mode (the default during development):

1. Planner and Verifier run as normal.
2. The cheap, deterministic part of the Visual stage runs: crop the cited figures and draw callout numbers with Pillow. No image-model or VLM calls.
3. The session pauses in state `awaiting_review` and emits a review package: the SOP plus the cropped figure for each step.
4. You approve, edit, or reject per step. Only approved steps go to the Visual agent for image-model work (photo localisation, rich annotation, any generated illustration).
5. Rejections and edits are saved as labelled examples and feed the eval golden set.

In `auto` mode the gate is skipped and the pipeline streams end to end. A per-session cost cap (`MAX_IMAGE_COST_USD`) applies in both modes and hard-stops image calls when reached.

## Data models and SOP schema

Postgres holds everything: relational tables for the graph, pgvector for embeddings, a tsvector column for BM25-style search. One database keeps the demo deployable on a single machine.

| Table | Key columns |
| --- | --- |
| `manuals` | id, sha256, title, brand, models\[\], pages, status, ingest\_report jsonb |
| `blocks` | id, manual\_id, page, bbox, type, section\_path, text, models\[\] |
| `figures` | id, manual\_id, page, image\_uri, caption, callouts jsonb (number, x, y) |
| `table_rows` | id, manual\_id, table\_kind, data jsonb, page |
| `chunks` | id, manual\_id, block\_ids\[\], text, embedding vector(1024), tsv, models\[\] |
| `edges` | src\_id, dst\_id, kind (refers\_to, callout\_part, warns, fixes, see\_also) |
| `sessions` | id, manual\_id, model, state jsonb, created\_at |
| `traces` | id, session\_id, node, input, output, tokens, latency\_ms, cost |

The SOP is the contract between Planner, Verifier, Visual and the client:

```json
{
  "sop_id": "uuid",
  "manual_id": "uuid",
  "model": "HP LaserJet M404",
  "title": "Replace the fuser unit",
  "cause": "Fuser thermistor open circuit (error 50.2)",
  "tools": ["Phillips #2", "Multimeter"],
  "parts": [{"name": "Fuser assembly", "number": "RM2-5399", "citation": "c_812"}],
  "warnings": [{"text": "Fuser is hot. Wait 30 min after power-off.", "citation": "c_801"}],
  "steps": [
    {
      "n": 1,
      "action": "Remove the rear cover (2 screws).",
      "spec": null,
      "citations": [{"chunk_id": "c_804", "page": 212}],
      "figure": {"figure_id": "f_77", "callouts": [3, 4]},
      "verified": true
    }
  ],
  "unverified_steps": []
}
```

The schema lives in one Pydantic module and is used for LLM structured output, API responses and eval fixtures.

## API surface

Six endpoints cover the whole product; query results stream over Server-Sent Events so steps appear as they pass verification.

| Method | Path | Does |
| --- | --- | --- |
| POST | `/manuals` | Upload PDF; returns `manual_id` and an ingest job id |
| GET | `/manuals/{id}` | Status and ingest report |
| POST | `/sessions` | Start a session on a manual; optional product photo |
| POST | `/sessions/{id}/messages` | Send a query or an answer to a diagnosis question; SSE stream of events |
| GET | `/sessions/{id}/sop` | Latest verified SOP JSON |
| GET | `/figures/{id}/render?callouts=3,4&step=1` | Annotated figure PNG |

SSE event types: `question` (diagnosis asks something), `step` (one verified step), `warning`, `visual` (image URL for a step), `insufficient_evidence`, `done`.

In `gated` mode, two more endpoints drive the review gate. `GET /sessions/{id}/review` returns the SOP with cropped figures. `POST /sessions/{id}/review` takes per-step decisions (`approve`, `edit` with new text, `reject`) and releases approved steps to the Visual agent. The stream emits an `awaiting_review` event when the session pauses.

## Tech stack

Python end to end, one Postgres, one worker queue; every choice below is swappable behind an interface.

| Layer | Choice | Why |
| --- | --- | --- |
| API | FastAPI + SSE | Async, typed, streams easily |
| Orchestration | LangGraph | Explicit state machine, checkpointing, retries per node |
| LLM | Claude (strong + fast tiers) via one client wrapper | Structured output, vision for figures and photos |
| PDF parsing | Docling, PyMuPDF fallback | Layout, tables, figure bboxes |
| Embeddings | bge-m3 or a hosted embedding API | Strong on technical text; local option keeps cost at zero |
| Reranker | bge-reranker-v2 | Cheap, large precision gain |
| Store | Postgres 16 + pgvector | Graph, vectors and full-text in one place |
| Queue | Redis + arq | Async ingestion jobs |
| Object storage | MinIO (local), S3-compatible | Figure crops and renders |
| Image annotation | Pillow + OpenCV | Crops, arrows, boxes, step badges |
| Tracing | Langfuse (self-hosted) | Per-node traces, cost, latency |
| Infra | Docker Compose; GitHub Actions CI | `docker compose up` runs everything |
| Demo client | Minimal React page | Upload, chat, step cards |

## Evaluation harness and metrics

Every change runs against a golden set in CI, and a regression on any hard metric fails the build.

**Golden set.** 5 printer service manuals, 60 questions each (300 total), hand-labelled with: expected chunk ids, expected figure, expected part numbers and specs, expected step order. Mix: 40% error-code lookups, 30% procedures, 20% diagnosis (multi-turn, scripted answers), 10% out-of-scope questions the manual cannot answer.

| Metric | How measured | Target |
| --- | --- | --- |
| Retrieval recall@8 | Expected chunks present in context pack | ≥ 0.90 |
| Step faithfulness | LLM judge + exact-match on numbers, per step | ≥ 0.95 |
| Citation accuracy | Cited page contains the claim | ≥ 0.95 |
| Part number / spec exact match | String match against labels | 1.00 (hard fail below) |
| Step order correctness | Kendall tau vs labelled order | ≥ 0.90 |
| Figure selection accuracy | Chosen figure = labelled figure | ≥ 0.85 |
| Diagnosis accuracy | Correct cause within 6 turns | ≥ 0.80 |
| Out-of-scope refusal | Returns `insufficient_evidence` | ≥ 0.90 |

**Ablations to publish in the README.** Naive chunking vs semantic chunking; no graph expansion vs one hop; no verifier vs verifier. Each row shows the metric delta, which is the resume headline.

## Performance targets and failure handling

The first verified step must reach the client in under 5 seconds; a full SOP in under 20.

| Target | Value |
| --- | --- |
| Error-code lookup, end to end | p95 < 2 s |
| First streamed step | p95 < 5 s |
| Full SOP (8 steps, verified, with visuals) | p95 < 20 s |
| Ingestion, 500-page manual | < 10 min |
| Cost per query | < $0.05 |

**How we hit them.** Steps stream as each passes the Verifier. Figure crops are precomputed at ingest. Repeat queries hit a cache keyed on (manual, model, normalised query). Visual rendering runs in parallel with verification of the next step.

**Failure handling.**

- LLM call fails or times out: retry with backoff (3 tries), then fall back to the other model tier.
- Structured output invalid: re-ask with the validation error, once, then fail the node.
- Verifier rejects a step twice: drop it, list it in `unverified_steps`, tell the user.
- Retrieval below threshold: return `insufficient_evidence`, never improvise.
- Ingestion partial: index what parsed, mark the manual `degraded`, surface the report.

## Repo layout and build milestones

One repo, one `docker compose up`, built in six milestones of roughly a week each, each ending with something that runs.

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

1. **Ingestion.** Parse one printer service manual end to end; ingest report shows figures, tables and links.
2. **Retrieval.** Hybrid search, rerank, graph expansion; recall@8 measured on 50 labelled questions.
3. **Planner and Verifier.** Structured SOP with citations; faithfulness measured; ablation with and without verifier.
4. **Diagnosis loop.** Multi-turn troubleshooting with measurements; session checkpointing.
5. **Visuals.** Figure crops with callout annotations; photo localisation for the part.
6. **Hardening.** Streaming, caching, latency and cost targets, full 300-question eval in CI, README with ablation table and a 2-minute demo video.

Appliances (washing machines, refrigerators) come after milestone 6 as a generalisation test: ingest, run the eval, report what broke.
