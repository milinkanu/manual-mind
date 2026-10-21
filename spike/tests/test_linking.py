"""Tests for the callout -> part linking rules found by the spike (see docs/SPEC.md, "Callout linking rules")."""
import linking

HP_HEADER = ["Ref", "Description", "Part number", "Qty"]
CANON_HEADER = ["FIGURE & KEY NO.", "PARTS NUMBER", "R A N K", "Q'TY", "DESCRIPTION", "SERIAL NUMBER/ REMARKS", "S V C"]


def table(table_id, page, rows, header=HP_HEADER):
    ref, pn = linking.find_columns(header)
    return {"table_id": table_id, "page": page, "header": header, "rows": rows,
            "ref_column": ref, "part_number_column": pn}


def hp_row(ref, pn, desc="Part"):
    return {"Ref": ref, "Description": desc, "Part number": pn, "Qty": "1"}


def figure(page, *numbers):
    return {"figure_id": f"fig_p{page}", "page": page,
            "callouts": [{"number": n, "x": 0, "y": 0} for n in numbers]}


# --- table classification and column detection ---

def test_hp_and_canon_headers_are_parts_lists():
    assert linking.classify_table(HP_HEADER) == "parts_list"
    assert linking.classify_table(CANON_HEADER) == "parts_list"


def test_columns_found_for_both_layouts():
    assert linking.find_columns(HP_HEADER) == ("Ref", "Part number")
    # Canon says "PARTS NUMBER" (plural); the spike first missed this and returned no part numbers
    assert linking.find_columns(CANON_HEADER) == ("FIGURE & KEY NO.", "PARTS NUMBER")


def test_part_number_column_is_never_used_as_the_ref_column():
    ref, pn = linking.find_columns(["Part no.", "Description"])
    assert ref is None and pn == "Part no."


def test_supplies_table_with_product_names_is_not_a_parts_list():
    rows = [{"Item": "HP 26A Black Original LaserJet Toner Cartridge"}, {"Item": "Supplies"}]
    assert not linking.ref_column_is_numeric(rows, "Item")


def test_not_shown_placeholders_do_not_count_against_the_ref_column():
    refs = ["1", "1", "2", "3", "Not shown", "4", "5", "6"] + ["Not shown"] * 8
    assert linking.ref_column_is_numeric([{"Ref": r} for r in refs], "Ref")


# --- matching ---

def test_refs_match_numbers_but_not_letter_labels():
    assert linking.refs_match("14", "14")
    assert linking.refs_match("03", "3")
    assert not linking.refs_match("A01", "1")
    assert not linking.refs_match("", "")


# --- linking rules ---

def test_figure_links_to_the_table_after_it_not_before():
    # Real bug: the figure on p.1300 sits between the Covers table (p.1299) and its own table (p.1301).
    # Nearest-page-either-side picked p.1299 and gave callout 5 a cover instead of a cable.
    covers = table("t1299", 1299, [hp_row("5", "COVER-LEFT")])
    cable = table("t1301", 1301, [hp_row("5", "CABLE-NFC")])
    fig = figure(1300, "5")
    linking.link_callouts([fig], [covers, cable])
    assert fig["callouts"][0]["match_table_id"] == "t1301"
    assert fig["callouts"][0]["matched_part_number"] == "CABLE-NFC"


def test_table_on_the_previous_page_alone_never_matches():
    fig = figure(1306, "1")
    linking.link_callouts([fig], [table("t1305", 1305, [hp_row("1", "WRONG-PART")])])
    assert fig["callouts"][0]["matched"] is False


def test_every_model_variant_row_is_kept():
    # Canon key 2 has one row per model; taking the first row silently picks the wrong variant.
    rows = [{"FIGURE & KEY NO.": "2", "PARTS NUMBER": f"FC0-{n}", "DESCRIPTION": "COVER, RIGHT",
             "SERIAL NUMBER/ REMARKS": model}
            for n, model in (("4236", "LBP6600"), ("4282", "LBP6680x"), ("4283", "LBP6670dn"))]
    fig = figure(17, "2")
    linking.link_callouts([fig], [table("t18", 18, rows, CANON_HEADER)])
    c = fig["callouts"][0]
    assert c["matched"] and len(c["matched_parts"]) == 3
    assert c["matched_part_number"] is None  # ambiguous: the caller must choose by model
    assert [m["remarks"] for m in c["matched_parts"]] == ["LBP6600", "LBP6680x", "LBP6670dn"]


def test_single_row_gives_a_unique_part_number():
    fig = figure(1296, "3")
    linking.link_callouts([fig], [table("t1297", 1297, [hp_row("3", "RC4-2956")])])
    assert fig["callouts"][0]["matched_part_number"] == "RC4-2956"


def test_labels_without_a_table_row_stay_unmatched():
    # HP "A01" labels are non-orderable hardware: no row, and that is normal, not an error.
    fig = figure(1300, "A01", "7")
    linking.link_callouts([fig], [table("t1301", 1301, [hp_row("7", "SPRING")])])
    a01, seven = fig["callouts"]
    assert a01["matched"] is False and a01["matched_parts"] == []
    assert seven["matched"] is True
