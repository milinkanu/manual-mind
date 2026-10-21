"""Table classification, ref/part column detection, and callout -> part matching."""
import re

REF_KW = ["ref", "item", "key", "callout", "no."]
PN_KW = ["part no", "part number", "parts no", "parts number", "part #", "p/n", "catalog"]
DESC_KW = ["description", "qty", "quantity"]

PAGE_WINDOW = 1  # the manual puts a figure's parts table on the same or the NEXT page, never before it


def _has(norm: list[str], kws: list[str]) -> bool:
    return any(k in h for h in norm for k in kws)


def classify_table(header: list[str]) -> str:
    norm = [h.strip().lower() for h in header]
    if _has(norm, REF_KW) and (_has(norm, PN_KW) or _has(norm, DESC_KW)):
        return "parts_list"
    if _has(norm, ["error", "fault"]) and _has(norm, ["cause", "solution", "remedy"]):
        return "error_codes"
    if _has(norm, ["symptom"]):
        return "troubleshooting"
    if _has(norm, ["spec"]):
        return "specs"
    return "other"


def find_columns(header: list[str]) -> tuple[str | None, str | None]:
    """(ref_col, part_number_col) among the header strings."""
    norm = {h: h.strip().lower() for h in header}
    ref = next((h for h, n in norm.items()
                if any(k in n for k in REF_KW) and not any(k in n for k in PN_KW)), None)
    pn = next((h for h, n in norm.items() if h != ref and any(k in n for k in PN_KW)), None)
    return ref, pn


NOT_SHOWN = {"not shown", "n/a", "-", "—"}  # placeholder values: part has no callout
_REF_VALUE = re.compile(r"^\d{1,3}[a-z]?$", re.I)


def ref_column_is_numeric(rows: list[dict], col: str | None) -> bool:
    """True if most non-empty cells in col look like callout numbers (e.g. '14', '3a').
    Rejects tables whose 'Item' column holds product names (e.g. the supplies table)."""
    if col is None:
        return False
    vals = [v for r in rows if (v := r.get(col, "").strip()) and v.lower() not in NOT_SHOWN]
    return bool(vals) and sum(bool(_REF_VALUE.match(v)) for v in vals) / len(vals) >= 0.5


def refs_match(a, b) -> bool:
    na, nb = str(a).strip().lower(), str(b).strip().lower()
    return bool(na) and (na == nb or (na.isdigit() and nb.isdigit() and int(na) == int(nb)))


def _remarks(row: dict) -> str:
    """Variant/model info for a row: any 'remark'/'serial' column (Canon), else description."""
    for k, v in row.items():
        if any(w in k.lower() for w in ("remark", "serial")) and v.strip():
            return v.strip()
    return ""


def link_callouts(figures: list[dict], parts_tables: list[dict]) -> None:
    """Annotate each callout in-place. A key number can have several rows (one per model variant),
    so ALL rows of the first table with a hit are kept in matched_parts; matched_part_number is set
    only when the match is unambiguous."""
    for fig in figures:
        cands = sorted((t for t in parts_tables if 0 <= t["page"] - fig["page"] <= PAGE_WINDOW),
                       key=lambda t: t["page"] - fig["page"])
        for c in fig["callouts"]:
            c.update(matched=False, matched_parts=[], matched_part_number=None, match_table_id=None)
            for t in cands:
                if t["ref_column"] is None:
                    continue
                hits = [r for r in t["rows"] if refs_match(c["number"], r.get(t["ref_column"], ""))]
                if hits:
                    pn = t["part_number_column"]
                    c["matched"] = True
                    c["match_table_id"] = t["table_id"]
                    c["matched_parts"] = [{"part_number": r.get(pn) if pn else None,
                                           "description": next((v for k, v in r.items() if "desc" in k.lower()), ""),
                                           "remarks": _remarks(r)} for r in hits]
                    if len(hits) == 1:
                        c["matched_part_number"] = c["matched_parts"][0]["part_number"]
                    break
