"""Print a hand-check sample: random figures with crop path, extracted callouts and matched parts.

    python spike/review_sample.py [--n-diagram 5] [--n-other 5] [--seed 7] [--diagram-from 1294]
"""
import argparse
import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-diagram", type=int, default=5)
    ap.add_argument("--n-other", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--diagram-from", type=int, default=1294, help="first page of the parts-diagram section")
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args()
    global OUT
    OUT = Path(__file__).resolve().parent / args.out_dir

    figs = [f for f in json.loads((OUT / "figures.json").read_text(encoding="utf-8")) if f["image_path"]]
    rows = json.loads((OUT / "parts_rows.json").read_text(encoding="utf-8"))
    by_table = {}
    for r in rows:
        by_table.setdefault(r["table_id"], []).append(r)

    rng = random.Random(args.seed)
    diag = [f for f in figs if f["page"] >= args.diagram_from and f["callouts"]]
    other = [f for f in figs if f not in diag]
    pick = rng.sample(diag, min(args.n_diagram, len(diag))) + rng.sample(other, min(args.n_other, len(other)))

    for f in pick:
        print(f"\n=== {f['figure_id']}  (PDF page {f['page']})  caption: {f['caption']}")
        print(f"crop: {(OUT / f['image_path']).resolve()}")
        if not f["callouts"]:
            print("  callouts: none")
        for c in f["callouts"]:
            if c["matched"]:
                ms = c["matched_parts"]
                print(f"  callout {c['number']:>3} @({c['x']},{c['y']}) -> {len(ms)} candidate row(s) [{c['match_table_id']}]")
                for m in ms[:4]:
                    print(f"        {m['part_number']}  {m['description'][:45]}  {m['remarks'][:40]}")
            else:
                print(f"  callout {c['number']:>3} @({c['x']},{c['y']}) -> NO MATCH")


if __name__ == "__main__":
    main()
