"""Docling-based extraction of figures and tables from a page-limited slice of a PDF."""
import hashlib
from pathlib import Path

import fitz  # PyMuPDF

PAGE_DPI = 200


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_page_range(spec: str, total_pages: int) -> tuple[int, int]:
    """'75' -> (1, 75); '150-225' -> (150, 225). 1-indexed, inclusive, clamped to the doc."""
    if "-" in spec:
        start, end = (int(p) for p in spec.split("-", 1))
    else:
        start, end = 1, int(spec)
    start = max(1, start)
    end = min(total_pages, end)
    if start > end:
        raise ValueError(f"empty page range {spec!r} for a {total_pages}-page PDF")
    return start, end


def slice_pdf(src: Path, start: int, end: int, out_path: Path) -> None:
    with fitz.open(src) as s, fitz.open() as d:
        d.insert_pdf(s, from_page=start - 1, to_page=end - 1)
        d.save(out_path)


def convert_pdf(pdf_path: Path):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.images_scale = PAGE_DPI / 72
    opts.generate_picture_images = True
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return conv.convert(pdf_path).document


def extract_figures(doc, page_offset: int) -> list[dict]:
    """page_offset = start_page - 1, to map sliced-PDF page numbers back to the original."""
    out = []
    for i, pic in enumerate(doc.pictures):
        if not pic.prov:
            continue
        page = pic.prov[0].page_no + page_offset
        try:
            caption = pic.caption_text(doc) or None
        except Exception:
            caption = None
        out.append({
            "figure_id": f"fig_p{page}_{i}",
            "page": page,
            "image": pic.get_image(doc),  # PIL image or None
            "caption": caption,
        })
    return out


def extract_tables(doc, page_offset: int) -> list[dict]:
    out = []
    for i, tbl in enumerate(doc.tables):
        if not tbl.prov:
            continue
        page = tbl.prov[0].page_no + page_offset
        df = tbl.export_to_dataframe(doc)
        header = [str(c) for c in df.columns]
        df.columns = header
        rows = [{k: ("" if v is None else str(v)) for k, v in r.items()} for r in df.to_dict(orient="records")]
        out.append({"table_id": f"tbl_p{page}_{i}", "page": page, "header": header, "rows": rows})
    return out
