"""Real native PDF exports with independent, controlled figure coordinates."""

import json

import pymupdf
import pytest

from app.parsing.figures import extract


@pytest.fixture
def native(tmp_path, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "output"
    output.mkdir()
    with pymupdf.open() as pdf:
        for index in range(2):
            page = pdf.new_page(width=400, height=600)
            page.draw_rect((50, 80, 250, 200), color=(1, 0, 0), fill=(0, 1, 0))
            page.insert_text((60, 120), "Circuit node A", fontsize=12)
        pdf.save(source)
    rows = [
        {
            "figType": "Figure",
            "name": "7",
            "caption": "Fig. 7. Circuit node A.",
            "page": 0,
            "regionBoundary": {"x1": 45, "y1": 75, "x2": 255, "y2": 205},
        }
    ]

    def locate(*args, **kwargs):
        (output / "source.json").write_text(json.dumps({"figures": rows}))

    monkeypatch.setattr("app.parsing.figures.subprocess.run", locate)
    options = {
        "java": "fake",
        "jar": "fake",
        "timeout": 3,
        "max_figures": 20,
        "dpi": 300,
        "max_dpi": 600,
        "max_pixels": 40000000,
        "thumbnail_edge": 480,
        "max_temp_bytes": 10000000,
        "backend_version": "fixture",
    }
    return source, output, options, rows


def test_export_keeps_vectors_caption_and_nonblank_preview(native):
    source, output, options, rows = native
    before = source.read_bytes()
    exported = extract(source, output, options)
    figure = exported["figures"][0]
    assert figure["caption_text"] == "Fig. 7. Circuit node A."
    assert figure["figure_number"] == "7" and figure["parts"][0]["page"] == 1
    native_asset = next(
        asset for asset in figure["assets"] if asset["role"] == "native"
    )
    with pymupdf.open(output / native_asset["path"]) as pdf:
        assert pdf[0].get_drawings()
        assert "Circuit node A" in pdf[0].get_text()
    preview = next(asset for asset in figure["assets"] if asset["role"] == "preview")
    pixmap = pymupdf.Pixmap(str(output / preview["path"]))
    assert pixmap.width >= 850 and pixmap.height >= 550
    assert pixmap.color_count() > 2
    assert source.read_bytes() == before


def test_pixel_budget_retains_native_and_thumbnail(native):
    source, output, options, rows = native
    options["max_pixels"] = 10
    exported = extract(source, output, options)
    assert {asset["role"] for asset in exported["figures"][0]["assets"]} == {
        "native",
        "thumbnail",
    }
    assert any(
        warning["code"] == "FIGURE_PREVIEW_PIXEL_LIMIT"
        for warning in exported["warnings"]
    )


def test_continued_figures_are_multipage_and_invalid_pages_rejected(native):
    source, output, options, rows = native
    rows.extend(
        [
            {**rows[0], "page": 1, "caption": "Fig. 7. (Continued) Circuit."},
            {**rows[0], "page": -1},
        ]
    )
    exported = extract(source, output, options)
    assert len(exported["figures"]) == 1
    assert [part["page"] for part in exported["figures"][0]["parts"]] == [1, 2]
    native_asset = next(
        asset for asset in exported["figures"][0]["assets"] if asset["role"] == "native"
    )
    with pymupdf.open(output / native_asset["path"]) as pdf:
        assert len(pdf) == 2


def test_rotated_crop_is_canonicalized_without_blank_page_failure(native, monkeypatch):
    source, output, options, rows = native
    with pymupdf.open(source) as pdf:
        pdf[0].set_rotation(90)
        pdf[1].set_cropbox(pymupdf.Rect(20, 20, 380, 580))
        pdf.new_page()
        rotated = source.with_name("rotated.pdf")
        pdf.save(rotated)

    def locate(*args, **kwargs):
        (output / "canonical.json").write_text(json.dumps({"figures": []}))

    monkeypatch.setattr("app.parsing.figures.subprocess.run", locate)
    assert extract(rotated, output, options)["figures"] == []
    with pymupdf.open(output / "canonical.pdf") as pdf:
        assert pdf[0].rotation == 0
        assert pdf[0].rect.width == 600
        assert "Circuit node A" in pdf[0].get_text()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_canonical_source_preserves_visible_page_pixels(native, rotation):
    from app.parsing.figures import _canonical_source

    source, output, options, rows = native
    transformed = source.with_name("transformed.pdf")
    with pymupdf.open(source) as pdf:
        pdf[0].set_cropbox(pymupdf.Rect(20, 30, 380, 550))
        pdf[0].set_rotation(rotation)
        pdf.save(transformed)
    canonical, _ = _canonical_source(transformed, output)
    with pymupdf.open(transformed) as original, pymupdf.open(canonical) as exported:
        expected, actual = original[0].get_pixmap(), exported[0].get_pixmap()
        assert (actual.width, actual.height) == (expected.width, expected.height)
        assert (
            sum(a != b for a, b in zip(actual.samples, expected.samples))
            / len(expected.samples)
            < 0.001
        )
