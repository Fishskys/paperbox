"""PDFFigures2 locator and native MuPDF export, executed only in a child process."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import uuid


def _canonical_source(source, output):
    import pymupdf
    from app.parsing.mupdf import _open

    with _open(source.read_bytes()) as document:
        geometry = [
            {
                "rotation": page.rotation,
                "cropbox": list(page.cropbox),
                "mediabox": list(page.mediabox),
            }
            for page in document
        ]
        if all(
            not item["rotation"] and item["cropbox"] == item["mediabox"]
            for item in geometry
        ):
            return source, geometry
        target = output / "canonical.pdf"
        with pymupdf.open() as canonical:
            for index, page in enumerate(document):
                visible, rotation = page.rect, page.rotation
                page.set_rotation(0)
                copied = canonical.new_page(width=visible.width, height=visible.height)
                if page.get_contents():
                    copied.show_pdf_page(copied.rect, document, index, rotate=-rotation)
            canonical.save(target, deflate=True)
        return target, geometry


def _region(value, page):
    import pymupdf

    box = pymupdf.Rect(*(float(value[key]) for key in ("x1", "y1", "x2", "y2")))
    if (
        not all(math.isfinite(number) for number in box)
        or box.is_empty
        or box.is_infinite
    ):
        raise ValueError("invalid figure coordinates")
    if (box & page.rect).get_area() < box.get_area() * 0.99:
        raise ValueError("figure coordinates are outside the page")
    return box


def extract(source: Path, output: Path, options: dict) -> dict:
    import pymupdf

    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    canonical, geometry = _canonical_source(source, output)
    subprocess.run(
        [
            options["java"],
            "-XX:ActiveProcessorCount=2",
            "-Xmx1536m",
            "-jar",
            options["jar"],
            str(canonical),
            "-t",
            "1",
            "-d",
            str(output) + "/",
            "-c",
            "-q",
        ],
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
        timeout=options["timeout"],
    )
    located = json.loads((output / (canonical.stem + ".json")).read_text())
    warnings = []
    for item in located.get("regionless-captions", []):
        if item.get("figType") == "Figure":
            warnings.append(
                {
                    "code": "FIGURE_REGION_MISSING",
                    "figure_number": str(item.get("name", "")),
                }
            )
    candidates = [
        row for row in located.get("figures", []) if row.get("figType") == "Figure"
    ]
    candidates.sort(
        key=lambda row: (
            row["page"],
            [
                (0, int(p)) if p.isdigit() else (1, p.casefold())
                for p in re.findall(r"\d+|\D+", str(row.get("name", "")))
            ],
        )
    )
    if len(candidates) > options["max_figures"]:
        warnings.append({"code": "FIGURE_LIMIT_REACHED", "detected": len(candidates)})
        candidates = candidates[: options["max_figures"]]
    groups = []
    for row in candidates:
        caption = str(row.get("caption") or "")
        number = str(row["name"]) if row.get("name") is not None else None
        continued = re.search(r"\((?:continued|cont\.)\)", caption[:100], re.I)
        prior = next(
            (
                group
                for group in reversed(groups)
                if group["figure_number"] == number
                and group["rows"][-1]["page"] + 1 == row["page"]
            ),
            None,
        )
        if continued and prior is not None:
            prior["rows"].append(row)
            continue
        groups.append({"figure_number": number, "caption_text": caption, "rows": [row]})
    figures = []
    total_bytes = source.stat().st_size + sum(
        path.stat().st_size for path in output.iterdir() if path.is_file()
    )
    if total_bytes > options["max_temp_bytes"]:
        raise ValueError("source and locator output exceed temporary byte budget")
    with pymupdf.open(canonical) as document:
        for group in groups:
            identifier = str(uuid.uuid4())
            directory = output / identifier
            directory.mkdir()
            assets, parts = [], []
            pdf = pymupdf.open()
            try:
                for row in group["rows"]:
                    try:
                        page_number = int(row["page"])
                        if not 0 <= page_number < len(document):
                            raise ValueError("invalid page")
                        page = document[page_number]
                        box = _region(row["regionBoundary"], page)
                    except (KeyError, ValueError, IndexError):
                        warnings.append(
                            {
                                "code": "FIGURE_INVALID_REGION",
                                "figure_number": group["figure_number"],
                            }
                        )
                        continue
                    padded = (
                        pymupdf.Rect(box.x0 - 2, box.y0 - 2, box.x1 + 2, box.y1 + 2)
                        & page.rect
                    )
                    index = len(parts) + 1
                    pdf.insert_pdf(document, from_page=page_number, to_page=page_number)
                    exported = pdf[-1]
                    exported.set_cropbox(padded)
                    dpi = options["dpi"]
                    for image in page.get_image_info():
                        image_box = pymupdf.Rect(image["bbox"])
                        if (
                            image_box.get_area()
                            and (image_box & box).get_area()
                            > image_box.get_area() * 0.9
                        ):
                            dpi = max(
                                dpi,
                                math.ceil(
                                    max(
                                        image["width"] / max(image_box.width, 1),
                                        image["height"] / max(image_box.height, 1),
                                    )
                                    * 72
                                ),
                            )
                    capped = min(dpi, options["max_dpi"])
                    part = {
                        "part": index,
                        "page": page_number + 1,
                        "bbox": list(box),
                        "export_bbox": list(padded),
                        "source_geometry": geometry[page_number],
                        "coordinate_space": "visible-page-points",
                        "dpi": capped,
                        "width": None,
                        "height": None,
                        "preview_downsampled": capped < dpi,
                    }
                    if (
                        math.ceil(padded.width * capped / 72)
                        * math.ceil(padded.height * capped / 72)
                        <= options["max_pixels"]
                    ):
                        pixmap = exported.get_pixmap(dpi=capped, alpha=False)
                        target = directory / f"part-{index:03}-preview.png"
                        pixmap.save(target)
                        assets.append(
                            {
                                "path": str(target.relative_to(output)),
                                "role": "preview",
                                "part": index,
                                "mime_type": "image/png",
                                "width": pixmap.width,
                                "height": pixmap.height,
                                "dpi": capped,
                            }
                        )
                        part.update(width=pixmap.width, height=pixmap.height)
                    else:
                        warnings.append(
                            {
                                "code": "FIGURE_PREVIEW_PIXEL_LIMIT",
                                "figure_id": identifier,
                                "part": index,
                            }
                        )
                    scale = min(
                        options["thumbnail_edge"] / max(padded.width, padded.height),
                        capped / 72,
                    )
                    thumbnail = exported.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale), alpha=False
                    )
                    target = directory / f"part-{index:03}-thumbnail.png"
                    thumbnail.save(target)
                    assets.append(
                        {
                            "path": str(target.relative_to(output)),
                            "role": "thumbnail",
                            "part": index,
                            "mime_type": "image/png",
                            "width": thumbnail.width,
                            "height": thumbnail.height,
                            "dpi": scale * 72,
                        }
                    )
                    parts.append(part)
                if not parts:
                    continue
                target = directory / "figure.pdf"
                pdf.save(target, deflate=True)
                assets.append(
                    {
                        "path": str(target.relative_to(output)),
                        "role": "native",
                        "part": 0,
                        "mime_type": "application/pdf",
                        "width": None,
                        "height": None,
                        "dpi": None,
                    }
                )
            finally:
                pdf.close()
            for asset in assets:
                file = output / asset["path"]
                asset.update(
                    size_bytes=file.stat().st_size,
                    sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
                )
                total_bytes += asset["size_bytes"]
            if total_bytes > options["max_temp_bytes"]:
                raise ValueError("figure temporary output exceeded its byte budget")
            label = re.match(
                r"\s*(?:Fig(?:ure)?\.?|\u56fe)\s*[A-Za-z0-9.()-]+",
                group["caption_text"],
                re.I,
            )
            figures.append(
                {
                    "id": identifier,
                    "figure_number": group["figure_number"],
                    "label_raw": label[0].strip() if label else None,
                    "caption_text": group["caption_text"],
                    "parts": parts,
                    "assets": assets,
                    "match_status": "needs_review",
                }
            )
    if figures:
        warnings.append(
            {
                "code": "FIGURE_REVIEW_REQUIRED",
                "detail": "Automatic regions and captions require review; native PDF is a view crop, not redaction.",
            }
        )
    return {
        "schema_version": 1,
        "source_sha256": original_hash,
        "backend": "pdffigures2",
        "backend_version": options["backend_version"],
        "exporter_version": f"pymupdf {pymupdf.__version__}",
        "figures": figures,
        "warnings": warnings,
        "status": "partial" if warnings else "completed",
    }


def main():
    options = json.loads(Path(sys.argv[3]).read_text())
    output = Path(sys.argv[2])
    result = extract(Path(sys.argv[1]), output, options)
    (output / "manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
