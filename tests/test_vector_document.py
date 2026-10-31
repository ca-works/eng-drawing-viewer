"""Geometry-based vector tests using synthetic, redistributable artwork only."""
import json
import math
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPainterPath, QTransform

from eng_viewer.core.vector_document import (
    VectorDocument, VectorObject, commands_from_path, import_dxf, import_svg, parse_svg_path,
)
from eng_viewer.core.vector_trace import simplify_object, trace_image


def rectangle(x=10, y=20, width=30, height=40, **kwargs):
    path = QPainterPath(); path.addRect(x, y, width, height)
    return VectorObject("Rectangle", commands_from_path(path), **kwargs)


def test_native_roundtrip_preserves_editability_style_group_and_history(tmp_path):
    doc = VectorDocument(100, 150)
    obj = rectangle(fill="#2277ff", layer="Cut", group="group1")
    doc.add(obj); doc.snapshot(); obj.commands[1][1] += 3
    assert doc.undo(); assert doc.objects[0].commands[1][1] == 40
    assert doc.redo(); assert doc.objects[0].commands[1][1] == 43
    path = tmp_path / "drawing.engvec"; doc.save(path)
    reopened = VectorDocument.load(path)
    assert reopened.width == 100 and reopened.height == 150
    assert reopened.objects[0].fill == "#2277ff"
    assert reopened.objects[0].group == "group1"
    assert reopened.objects[0].commands[1][1] == 43
    assert reopened.layers[1]["name"] == "Cut"
    assert not reopened.dirty


@pytest.mark.parametrize("data", [
    {"format": "ENG Vector", "version": 2},
    {"format": "ENG Vector", "version": 1, "width_mm": math.inf, "height_mm": 10},
    {"format": "ENG Vector", "version": 1, "width_mm": 10, "height_mm": 10, "objects": [{"name": "bad", "commands": [["M", math.nan, 0]]}]},
])
def test_invalid_native_documents_are_rejected(tmp_path, data):
    path = tmp_path / "bad.engvec"; path.write_text(json.dumps(data))
    with pytest.raises((ValueError, KeyError)): VectorDocument.load(path)


def test_svg_roundtrip_true_geometry_and_physical_dimensions(tmp_path):
    doc = VectorDocument(120, 90); doc.add(rectangle(fill="none", stroke="#ff0000", stroke_width=.5))
    path = tmp_path / "output.svg"; doc.export_svg(path)
    text = path.read_text()
    assert "<path" in text and "<image" not in text and 'width="120mm"' in text
    reopened = import_svg(path)
    assert reopened.width == pytest.approx(120)
    assert reopened.height == pytest.approx(90)
    bounds = reopened.objects[0].path().boundingRect()
    assert bounds == QRectF(10, 20, 30, 40)
    assert reopened.objects[0].stroke_width == pytest.approx(.5)


def test_svg_inches_transform_and_bezier_nodes(tmp_path):
    path = tmp_path / "inch.svg"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="1in" viewBox="0 0 200 100"><g transform="translate(20 10) scale(2)"><path d="M0 0 C5 0 5 10 10 10 L10 0Z" fill="red"/></g></svg>')
    doc = import_svg(path)
    assert doc.width == pytest.approx(50.8)
    assert doc.height == pytest.approx(25.4)
    bounds = doc.objects[0].path().boundingRect()
    assert bounds.x() == pytest.approx(20 * .254)
    assert bounds.y() == pytest.approx(10 * .254)
    assert bounds.width() == pytest.approx(20 * .254)
    assert any(command[0] == "C" for command in doc.objects[0].commands)


def test_svg_parser_arcs_shorthand_and_relative_geometry():
    path = parse_svg_path("M10 10 h20 v20 h-20z M50 50 c10 0 10 10 20 10 s10-10 20-10 q10-10 20 0 t20 0 a10 10 0 0 1 20 0")
    assert path.elementCount() > 20
    assert path.currentPosition().x() == pytest.approx(150)
    assert path.currentPosition().y() == pytest.approx(50)
    assert path.contains(QPointF(20, 20))
    with pytest.raises(ValueError): parse_svg_path("M 0 0 C 2 3")


def test_svg_entity_expansion_and_external_images_are_not_loaded(tmp_path):
    path = tmp_path / "unsafe.svg"
    path.write_text('<!DOCTYPE svg [<!ENTITY ext SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg"><text>&ext;</text></svg>')
    with pytest.raises(Exception): import_svg(path)
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><image href="https://example.invalid/private.png"/><script>alert(1)</script><rect width="10" height="10"/></svg>')
    doc = import_svg(path)
    assert len(doc.objects) == 1
    assert "image" in doc.warnings[0] and "script" in doc.warnings[0]


def test_dxf_closed_polyline_roundtrip_in_mm_and_inches(tmp_path):
    import ezdxf
    doc = VectorDocument(100, 100); doc.add(rectangle())
    path = tmp_path / "output.dxf"; doc.export_dxf(path)
    raw = ezdxf.readfile(path)
    assert raw.units == 4
    polyline = list(raw.modelspace())[0]
    assert polyline.dxftype() == "LWPOLYLINE" and polyline.closed
    reopened = import_dxf(path)
    assert reopened.objects[0].path().boundingRect() == QRectF(10, 20, 30, 40)
    inches = ezdxf.new(); inches.units = 1
    inches.modelspace().add_lwpolyline([(0, 0), (2, 0), (2, -1), (0, -1)], close=True)
    inch_path = tmp_path / "inch.dxf"; inches.saveas(inch_path)
    converted = import_dxf(inch_path)
    bounds = converted.objects[0].path().boundingRect()
    assert bounds.width() == pytest.approx(50.8)
    assert bounds.height() == pytest.approx(25.4)


def test_dxf_circle_import_has_editable_bezier_geometry(tmp_path):
    import ezdxf
    raw = ezdxf.new(); raw.units = 4; raw.modelspace().add_circle((40, -40), 10)
    path = tmp_path / "circle.dxf"; raw.saveas(path)
    doc = import_dxf(path)
    assert any(c[0] == "C" for c in doc.objects[0].commands)
    assert doc.objects[0].path().boundingRect().width() == pytest.approx(20)


def test_boolean_difference_retains_hole_and_is_undoable():
    doc = VectorDocument(); outer = rectangle(10, 10, 50, 50); inner = rectangle(20, 20, 10, 10)
    doc.add(outer); doc.add(inner)
    result = doc.boolean({outer.id, inner.id}, "subtract")
    assert result.path().contains(QPointF(15, 15))
    assert not result.path().contains(QPointF(25, 25))
    assert doc.undo() and len(doc.objects) == 2
    assert doc.redo() and len(doc.objects) == 1


def test_transform_preserves_physical_coordinates_and_respects_lock():
    doc = VectorDocument(); obj = rectangle(); locked = rectangle(60, 10, 10, 10, locked=True)
    doc.add(obj); doc.add(locked)
    doc.transform({obj.id, locked.id}, QTransform.fromScale(2, 2))
    assert doc.objects[0].path().boundingRect() == QRectF(20, 40, 60, 80)
    assert doc.objects[1].path().boundingRect() == QRectF(60, 10, 10, 10)


def test_text_native_and_svg_roundtrip(qapp, tmp_path):
    doc = VectorDocument(); obj = VectorObject.text_object("ENG 123", 20, 30, size=8, fill="black", stroke="none")
    doc.add(obj); path = tmp_path / "text.engvec"; doc.save(path)
    reopened = VectorDocument.load(path)
    assert reopened.objects[0].text["content"] == "ENG 123"
    svg = tmp_path / "text.svg"; doc.export_svg(svg)
    assert "<text" in svg.read_text()
    reimported = import_svg(svg)
    assert reimported.objects[0].text["size"] == pytest.approx(8)


def test_vector_pdf_and_raster_export_have_expected_size(qapp, tmp_path):
    import pymupdf
    doc = VectorDocument(50.8, 25.4); doc.add(rectangle(2, 2, 10, 10))
    image = doc.render_image(dpi=100)
    assert (image.width(), image.height()) == (200, 100)
    path = tmp_path / "vector.pdf"; doc.export_pdf(path)
    with pymupdf.open(path) as pdf:
        assert pdf[0].rect.width == pytest.approx(144, abs=.1)
        assert pdf[0].rect.height == pytest.approx(72, abs=.1)
        assert pdf[0].get_drawings()
        assert not pdf[0].get_images()


def test_output_original_and_existing_files_protected(tmp_path):
    doc = VectorDocument(); doc.add(rectangle())
    path = tmp_path / "out.svg"; path.write_text("original")
    with pytest.raises(FileExistsError): doc.export_svg(path)
    doc.source_path = str(path)
    with pytest.raises(ValueError, match="original"): doc.export_svg(path, overwrite=True)
    assert path.read_text() == "original"


def test_cutting_checks_identify_open_duplicate_and_degenerate_paths():
    doc = VectorDocument(100, 100)
    doc.add(rectangle()); doc.add(rectangle())
    doc.add(VectorObject("Open", [["M", 0, 0], ["L", 20, 0]], fill="none"))
    warnings = doc.cutting_checks()
    assert any("duplicate" in warning for warning in warnings)
    assert any("open" in warning.lower() for warning in warnings)
    assert any("thinner" in warning for warning in warnings)


def test_threshold_trace_has_real_closed_contours_holes_and_scale(tmp_path):
    image = Image.new("RGBA", (200, 100), "white")
    draw = ImageDraw.Draw(image); draw.rectangle((20, 20, 179, 79), fill="black"); draw.rectangle((60, 35, 100, 60), fill="white")
    doc, info = trace_image(image, threshold=128, min_area=2, simplify=.2, dpi=100)
    assert doc.width == pytest.approx(50.8) and doc.height == pytest.approx(25.4)
    assert info["contours"] == 2
    path = doc.objects[0].path()
    assert path.contains(QPointF(30 * .254, 30 * .254))
    assert not path.contains(QPointF(80 * .254, 45 * .254))
    assert not any("open" in warning for warning in doc.cutting_checks())
    output = tmp_path / "traced.svg"; doc.export_svg(output)
    assert "<image" not in output.read_text()


def test_color_trace_transparency_noise_and_simplification():
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image); draw.rectangle((10, 10, 40, 80), fill="red"); draw.ellipse((50, 10, 85, 80), fill="blue"); draw.point((95, 95), fill="red")
    doc, info = trace_image(image, mode="color", colors=3, min_area=5, simplify=.5)
    assert len(doc.objects) == 2
    assert info["removed"] >= 1
    for obj in doc.objects:
        assert not obj.path().contains(QPointF(0, 0))
    original = sum(len(obj.commands) for obj in doc.objects)
    for obj in doc.objects: simplify_object(obj, .4)
    assert sum(len(obj.commands) for obj in doc.objects) <= original


def test_trace_resolution_limit_reported():
    image = Image.new("RGB", (3000, 100), "white")
    ImageDraw.Draw(image).rectangle((30, 20, 2000, 80), fill="black")
    doc, info = trace_image(image, max_side=1000)
    assert info["analysis_size"][0] == 1000
    assert info["analysis_scale"] == pytest.approx(1 / 3)
    assert doc.width == pytest.approx(3000 * 25.4 / 96)
    assert "uncertainty" in doc.warnings[0]


def test_exclusion_and_crossing_checks(qapp):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainterPath
    from eng_viewer.core.vector_document import VectorDocument, VectorObject, commands_from_path
    a=QPainterPath();a.addRect(QRectF(0,0,20,20))
    b=QPainterPath();b.addRect(QRectF(10,0,20,20))
    doc=VectorDocument(50,50)
    first=VectorObject('A',commands_from_path(a));second=VectorObject('B',commands_from_path(b))
    doc.add(first);doc.add(second)
    result=doc.boolean({first.id,second.id},'exclude')
    from PySide6.QtCore import QPointF
    assert result.path().contains(QPointF(5,10))
    assert not result.path().contains(QPointF(15,10))
    crossing=VectorObject('Bow tie', [['M',0,0],['L',20,20],['L',0,20],['L',20,0],['Z']])
    doc=VectorDocument(30,30,[crossing])
    assert any('self-intersecting' in warning for warning in doc.cutting_checks())


def test_vector_guides_and_units_survive_project(tmp_path):
    from eng_viewer.core.vector_document import VectorDocument
    d=VectorDocument();d.guides=[['x',12.3],['y',54.2]];d.units='in'
    path=tmp_path/'guides.engvec';d.save(path)
    restored=VectorDocument.load(path)
    assert restored.guides==d.guides and restored.units=='in'


def test_trace_corner_background_removal_preserves_interior():
    image = Image.new("RGB", (100, 100), "red")
    ImageDraw.Draw(image).rectangle((20, 20, 80, 80), fill="blue")
    doc, info = trace_image(image, mode="color", colors=2, remove_background=True)
    assert len(doc.objects) == 1 and info["contours"] == 1
    assert doc.objects[0].fill == "#0000ff"
    assert not doc.objects[0].path().contains(QPointF(1, 1))
