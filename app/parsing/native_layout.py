"""Conservative line ordering and heading hints for the local PDF fallback."""

from __future__ import annotations

from collections import Counter, defaultdict
import math
import re
import statistics
import unicodedata

from app.parsing.mupdf import invoke
from app.parsing.pdf import PageText, normalize_page_text


def _characters(text):
    return Counter(re.sub(r"\s+", "", text))


def _furniture(documents):
    groups = defaultdict(list)
    fonts = Counter()
    for content in documents:
        for line in content["lines"]:
            if content["height"] * .15 < line["bbox"][1] < content["height"] * .85 and len(line["text"]) > 25:
                fonts[round(line["size"], 1)] += len(line["text"])
    body = fonts.most_common(1)[0][0] if fonts else 10
    if len(documents) < 3:
        return {}, body
    for index, content in enumerate(documents):
        height = content["height"]
        for line in content["lines"]:
            if abs(line["dir"][1]) > .2:
                continue
            box = line["bbox"]
            side = "top" if box[3] <= height * .085 else "bottom" if box[1] >= height * .915 else None
            if not side:
                continue
            text = unicodedata.normalize("NFKC", line["text"]).strip()
            number = int(text) if re.fullmatch(r"\d{1,5}", text) else None
            publisher = re.search(r"journal|transactions|conference|published|\bet al\.|licensed|copyright|restrictions apply|\u00a9", text, re.I)
            if number is None and not (line["size"] <= body * .92 or (side == "top" and box[3] <= 45) or publisher):
                continue
            signature = "#page-number" if number is not None else re.sub(r"\s+", " ", text).casefold()
            position = round((box[1] if side == "top" else height - box[3]) / 8)
            groups[(side, position, round(line["size"]), signature)].append((index, line, number))
    selected = defaultdict(list)
    threshold = max(2, math.ceil(len(documents) * .25))
    for group in groups.values():
        if group[0][2] is not None:
            offsets = Counter(number - index for index, _, number in group)
            offset, count = offsets.most_common(1)[0]
            if count < threshold:
                continue
            group = [entry for entry in group if entry[2] - entry[0] == offset]
        if len({entry[0] for entry in group}) >= threshold:
            for index, line, number in group:
                selected[index].append((line, number))
    return selected, body


def _heading(line, body, first_page):
    text = normalize_page_text(line["text"])
    if len(text) > 180 or len(text.split()) > 18 or text.endswith((".", ",", ";")):
        return None
    if first_page and line["bbox"][1] < 220 and line["size"] >= body * 1.45 and len(text) > 12:
        return 1
    numbered = re.match(r"^(\d+(?:\.\d+)*|[IVX]+|[A-Z])[.)]?\s+([A-Za-z].*)$", text)
    if numbered and line["size"] >= body * .9:
        number = numbered[1]
        if number.isdigit() or "." in number:
            return min(6, 2 + number.count("."))
        return 2 if re.fullmatch(r"[IVX]+", number) else 3
    from app.parsing.structure import KNOWN_HEADINGS

    if text.rstrip(":").casefold() in KNOWN_HEADINGS:
        return 2
    if line["bold"] and line["size"] > body * 1.12 and len(text.split()) <= 12:
        return 2
    return None


def _order(lines, width, body):
    """Split only when long body lines establish two actual column populations."""
    center = width / 2
    eligible = [line for line in lines if len(line["text"]) >= 25 and line["size"] >= body * .85
                and abs(line["dir"][1]) < .2]
    left = [line for line in eligible if line["bbox"][2] < center + 8]
    right = [line for line in eligible if line["bbox"][0] > center - 8]
    two_columns = len(left) >= 3 and len(right) >= 3
    if not two_columns:
        return sorted(lines, key=lambda line: (round(line["bbox"][1], 1), line["bbox"][0])), 1
    gutter = (statistics.median(line["bbox"][2] for line in left) + min(line["bbox"][0] for line in right)) / 2
    full = [line for line in lines if line["bbox"][0] < gutter - 12 and line["bbox"][2] > gutter + 12]
    ordered = []
    remaining = list(lines)
    for barrier in sorted(full, key=lambda line: line["bbox"][1]):
        top = [line for line in remaining if line is not barrier and line["bbox"][3] <= barrier["bbox"][1] + 1]
        for side in (True, False):
            ordered.extend(sorted((line for line in top if (line["bbox"][0] < gutter) == side), key=lambda line: line["bbox"][1]))
        ordered.append(barrier)
        used = {id(line) for line in top + [barrier]}
        remaining = [line for line in remaining if id(line) not in used]
    for side in (True, False):
        ordered.extend(sorted((line for line in remaining if (line["bbox"][0] < gutter) == side), key=lambda line: line["bbox"][1]))
    return ordered, 2


def prepare_pages(data, pages):
    from app.parsing.layout import LayoutReport, RunningLinesResult

    report = LayoutReport(pages=list(pages))
    furniture = RunningLinesResult(pages=list(pages))
    try:
        documents = invoke("layout", data)
        if len(documents) != len(pages):
            raise ValueError("PDF coordinates do not cover every source page")
    except Exception as exc:
        report.failed = True
        report.unverified_pages = [page.page for page in pages]
        report.warnings.append(str(exc))
        return report.pages, report, furniture
    excluded, body = _furniture(documents)
    for index, (content, original) in enumerate(zip(documents, pages, strict=True)):
        removed = excluded.get(index, [])
        keys = {tuple(line["key"]) for line, _ in removed}
        lines = [line for line in content["lines"] if tuple(line["key"]) not in keys]
        joined = "\n".join(normalize_page_text(line["text"]) for line in lines)
        deleted = "\n".join(normalize_page_text(line["text"]) for line, _ in removed)
        if _characters(original.text) != _characters(joined) + _characters(deleted):
            report.unverified_pages.append(original.page)
            report.warnings.append(f"page {original.page}: text could not be put back in order; original kept")
            report.columns.append(1)
            continue
        furniture.dropped.extend(line["text"] for line, number in removed if number is None)
        furniture.page_numbers += sum(number is not None for _, number in removed)
        ordered, columns = _order(lines, content["width"], body)
        report.columns.append(columns)
        report.reordered_pages += columns == 2
        headings = {}
        output = []
        previous = None
        for line in ordered:
            text = normalize_page_text(line["text"])
            if not text:
                continue
            level = _heading(line, body, index == 0)
            if level:
                headings[text] = level
            if output and (level or previous is None or line["key"][0] != previous["key"][0]
                           or line["bbox"][1] < previous["bbox"][1] - body
                           or line["bbox"][1] - previous["bbox"][3] > body * .7):
                output.append("")
            output.append(text)
            if level:
                output.append("")
            previous = line
        report.pages[index] = PageText(original.page, normalize_page_text("\n".join(output)), headings)
        if any(abs(line["dir"][1]) > .2 for line in lines):
            report.unverified_pages.append(original.page)
    return report.pages, report, furniture
