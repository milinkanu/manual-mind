"""Throwaway spike: can Docling + Claude vision extract figures/callouts and parts-list rows,
and can we link callouts to part numbers?  See docs/SPEC.md "Ingestion pipeline" steps 2, 4, 5, 6.

Usage (from repo root, venv active):
    python spike/run_ingest_spike.py --pages 150-225 [--skip-vlm] [--max-figures N] [--pdf PATH]
"""
import argparse
import json
import re
import time
from pathlib import Path

import fitz
from dotenv import load_dotenv
from PIL import Image

import docling_extract as dx
import linking

ROOT = Path(__file__).resolve().parent
DEFAULT_PDF = ROOT.parent / "data" / "manuals" / "hp-lj-pro-m402-m403-m426-m427-repair-manual.pdf"
OUT = ROOT / "output"
CACHE = OUT / "docling_cache.json"

load_dotenv(ROOT / ".env")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    ap.add_argument("--pages", default="1-75", help="'75' or '150-225' (1-indexed, inclusive)")
    ap.add_argument("--out-dir", default="output", help="subfolder of spike/ for this manual's results")
    ap.add_argument("--skip-vlm", action="store_true", help="no Claude calls; Docling half only")
    ap.add_argument("--reuse-cache", action="store_true", help="skip Docling; reuse output/docling_cache.json")
    ap.add_argument("--max-figures", type=int, default=None, help="cap vision calls")
    args = ap.parse_args()
    global OUT, CACHE
    OUT = ROOT / args.out_dir
    CACHE = OUT / "docling_cache.json"

    OUT.mkdir(exist_ok=True)
    (OUT / "crops").mkdir(exist_ok=True)

    with fitz.open(args.pdf) as d:
        total_pages = d.page_count
    start, end = dx.parse_page_range(args.pages, total_pages)
    manual_id = dx.sha256_file(args.pdf)

    cached = json.loads(CACHE.read_text(encoding="utf-8")) if args.reuse_cache and CACHE.exists() else None
    if cached and cached["manual_id"] == manual_id and cached["pages"] == [start, end]:
        print(f"Reusing Docling cache ({CACHE.name})")
        figures, tables, convert_s = cached["figures"], cached["tables"], cached["convert_s"]
    else:
        sliced = OUT / "_sliced_pages.pdf"
        dx.slice_pdf(args.pdf, start, end, sliced)
        print(f"Converting pages {start}-{end} with Docling (first run downloads models)...")
        t0 = time.time()
        doc = dx.convert_pdf(sliced)
        convert_s = time.time() - t0
        figures = dx.extract_figures(doc, start - 1)
        tables = dx.extract_tables(doc, start - 1)
        for fig in figures:
            img = fig.pop("image")
            fig["image_path"] = None
            if img is not None:
                fig["image_path"] = f"crops/{fig['figure_id']}.png"
                img.save(OUT / fig["image_path"])
        CACHE.write_text(json.dumps({"manual_id": manual_id, "pages": [start, end], "convert_s": convert_s,
                                     "figures": figures, "tables": tables}, indent=2), encoding="utf-8")

    # --- tables: classify, keep parts lists ---
    kind_counts: dict[str, int] = {}
    parts_tables, parts_rows = [], []
    for t in tables:
        kind = linking.classify_table(t["header"])
        ref_col, pn_col = linking.find_columns(t["header"])
        if kind == "parts_list" and not linking.ref_column_is_numeric(t["rows"], ref_col):
            kind = "other (parts-like, ref col not numeric)"
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if kind != "parts_list":
            continue
        t.update(table_kind=kind, ref_column=ref_col, part_number_column=pn_col)
        parts_tables.append(t)
        parts_rows += [{"table_id": t["table_id"], "manual_id": manual_id, "page": t["page"],
                        "table_kind": kind, "ref_column": ref_col, "part_number_column": pn_col, "data": r}
                       for r in t["rows"]]

    # --- figures: crop + callouts ---
    client = None
    if not args.skip_vlm:
        import anthropic
        client = anthropic.Anthropic()  # needs ANTHROPIC_API_KEY
        import vlm_callouts as vlm

    vision_calls = vision_errors = 0
    for fig in figures:
        fig["manual_id"] = manual_id
        fig["callouts"] = []
        if not fig["image_path"] or client is None:
            continue
        if args.max_figures is not None and vision_calls >= args.max_figures:
            continue
        img = Image.open(OUT / fig["image_path"])
        if min(img.size) < 100:  # icons / bullets, not diagrams
            continue
        vision_calls += 1
        res = vlm.extract_callouts(img, client)
        if res is None:
            vision_errors += 1
        else:
            fig["callouts"] = res

    linking.link_callouts(figures, parts_tables)

    (OUT / "figures.json").write_text(json.dumps(figures, indent=2), encoding="utf-8")
    (OUT / "parts_rows.json").write_text(json.dumps(parts_rows, indent=2), encoding="utf-8")

    # --- report ---
    callouts = [c for f in figures for c in f["callouts"]]
    matched = sum(c["matched"] for c in callouts)
    ambiguous = sum(1 for c in callouts if len(c["matched_parts"]) > 1)
    no_pn = sum(1 for c in callouts if c["matched"] and not any(m["part_number"] for m in c["matched_parts"]))
    print("\n=== ManualMind Ingestion Spike Report ===")
    print(f"PDF: {args.pdf.name}\nmanual_id (sha256): {manual_id}")
    print(f"Pages: {total_pages} total, processed {start}-{end}   Docling time: {convert_s:.1f}s")
    print(f"Figures found: {len(figures)}   (with crop image: {sum(1 for f in figures if f['image_path'])})")
    print(f"Tables found: {len(tables)}   by kind: {kind_counts}")
    print(f"Parts-list rows: {len(parts_rows)}")
    if client is None:
        print("Vision: skipped (--skip-vlm)")
    else:
        print(f"Vision calls: {vision_calls} ({vision_errors} errors, model={vlm.VISION_MODEL})")
        u = vlm.USAGE
        if u["calls"]:
            print(f"Vision tokens: {u['input_tokens']:,} in / {u['output_tokens']:,} out "
                  f"({u['input_tokens'] // u['calls']:,} in + {u['output_tokens'] // u['calls']:,} out per call; "
                  f"x100 figures = {100 * u['input_tokens'] // u['calls']:,} in / {100 * u['output_tokens'] // u['calls']:,} out)")
        print(f"Figures with zero callouts: {sum(1 for f in figures if f['image_path'] and not f['callouts'])}")
        print(f"Callouts extracted: {len(callouts)}")
        print(f"Callouts matched to a part: {matched}  (unique part: {matched - ambiguous - no_pn}, "
              f"ambiguous/several variant rows: {ambiguous}, no part number found: {no_pn})")
        print(f"Callouts unmatched: {len(callouts) - matched}")
        numeric = [c for c in callouts if re.fullmatch(r"\d{1,3}", c["number"])]
        print(f"Numeric-label callouts matched: {sum(c['matched'] for c in numeric)}/{len(numeric)}")
        found = {(c["match_table_id"], c["number"].lower()) for c in callouts if c["matched"]}
        covered = total = 0
        for pt in parts_tables:
            refs = {r.get(pt["ref_column"], "").strip().lower() for r in pt["rows"]} - {""} - linking.NOT_SHOWN
            total += len(refs)
            covered += sum((pt["table_id"], x) in found for x in refs)
        print(f"Parts-table refs found in a figure (table coverage): {covered}/{total}")
        print(f"Match rate: {100 * matched / len(callouts):.1f}%" if callouts else "Match rate: n/a")
    print(f"Output: {OUT / 'figures.json'}\n        {OUT / 'parts_rows.json'}\n        {OUT / 'crops'}")


if __name__ == "__main__":
    main()
