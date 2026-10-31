"""Editable vector geometry in millimetres, independent of the workspace widgets.

The native JSON format is declarative (no scripts, external references or pickle).
SVG imports are a safe, explicit geometry subset. DXF curves are read as Beziers;
DXF export flattens curves to real polylines at a user-visible physical tolerance.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPageLayout, QPageSize, QPdfWriter, QPen, QTransform

from .raster_limits import check_pixels

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_OBJECTS = 10_000
MAX_NODES = 250_000
UNITS = {"mm": 1.0, "cm": 10.0, "in": 25.4, "px": 25.4 / 96, "pt": 25.4 / 72}
_NUM = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"


def finite(value, low=-1_000_000, high=1_000_000):
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Vector coordinates or settings are outside the supported range.")
    return value


def path_from_commands(commands):
    if len(commands) > MAX_NODES:
        raise ValueError("The path has too many nodes.")
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    for command in commands:
        if not isinstance(command, (tuple, list)) or not command:
            raise ValueError("Invalid path command.")
        op, *coords = command
        if op not in {"M", "L", "C", "Q", "Z"} or len(coords) != {"M": 2, "L": 2, "C": 6, "Q": 4, "Z": 0}[op]:
            raise ValueError("Unsupported path command.")
        coords = [finite(v) for v in coords]
        if op == "M": path.moveTo(*coords)
        elif op == "L": path.lineTo(*coords)
        elif op == "C": path.cubicTo(*coords)
        elif op == "Q": path.quadTo(*coords)
        elif op == "Z": path.closeSubpath()
    return path


def commands_from_path(path):
    commands, i, start = [], 0, None
    while i < path.elementCount():
        element = path.elementAt(i)
        if element.isMoveTo():
            commands.append(["M", element.x, element.y]); start = (element.x, element.y)
        elif element.isLineTo():
            last = i + 1 == path.elementCount() or path.elementAt(i + 1).isMoveTo()
            if last and start and math.hypot(element.x - start[0], element.y - start[1]) < 1e-8:
                commands.append(["Z"])
            else:
                commands.append(["L", element.x, element.y])
        elif element.type == QPainterPath.ElementType.CurveToElement:
            c2, end = path.elementAt(i + 1), path.elementAt(i + 2)
            commands.append(["C", element.x, element.y, c2.x, c2.y, end.x, end.y]); i += 2
        i += 1
    return commands


def _color(value):
    value = str(value)
    if value == "none": return value
    color = QColor(value)
    if not color.isValid(): raise ValueError(f"Unsupported vector color: {value[:40]}")
    return color.name(QColor.NameFormat.HexArgb) if color.alpha() != 255 else color.name()


def _svg_paint(value, opacity="1"):
    """Convert CSS alpha ordering to Qt's internal ARGB representation."""
    if value == "none": return value
    opacity = finite(opacity, 0, 1)
    if re.fullmatch(r"#[0-9a-fA-F]{8}", value):
        value = "#" + value[-2:] + value[1:7]
    elif re.fullmatch(r"#[0-9a-fA-F]{4}", value):
        value = "#" + value[-1] * 2 + "".join(character * 2 for character in value[1:4])
    match = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", value)
    if match:
        r, g, b, a = match.groups()
        color = QColor(round(finite(r, 0, 255)), round(finite(g, 0, 255)), round(finite(b, 0, 255)))
        color.setAlphaF(finite(a, 0, 1) if a is not None else 1)
    else: color = QColor(_color(value))
    color.setAlphaF(color.alphaF() * opacity)
    return color.name(QColor.NameFormat.HexArgb) if color.alpha() != 255 else color.name()


@dataclass
class VectorObject:
    name: str
    commands: list
    fill: str = "#ffffff"
    stroke: str = "#202936"
    stroke_width: float = 0.3
    opacity: float = 1.0
    layer: str = "Artwork"
    group: str = ""
    visible: bool = True
    locked: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    text: dict | None = None
    fill_rule: str = "evenodd"

    def __post_init__(self):
        self.fill, self.stroke = _color(self.fill), _color(self.stroke)
        self.stroke_width = finite(self.stroke_width, 0, 1000)
        self.opacity = finite(self.opacity, 0, 1)
        self.name, self.layer = str(self.name)[:256], str(self.layer)[:256]
        if self.fill_rule not in {"evenodd", "nonzero"}: raise ValueError("Invalid vector fill rule.")
        path_from_commands(self.commands)
        if self.text is not None:
            required = {"content", "x", "y", "size", "family"}
            if not isinstance(self.text, dict) or not required <= self.text.keys():
                raise ValueError("Invalid text object.")
            self.text = {"content": str(self.text["content"])[:10000], "x": finite(self.text["x"]),
                         "y": finite(self.text["y"]), "size": finite(self.text["size"], 0.1, 1000),
                         "family": str(self.text["family"])[:256]}

    def path(self):
        path = path_from_commands(self.commands)
        path.setFillRule(Qt.FillRule.OddEvenFill if self.fill_rule == "evenodd" else Qt.FillRule.WindingFill)
        return path

    def transformed(self, transform):
        result = copy.deepcopy(self)
        result.commands = commands_from_path(transform.map(self.path()))
        # Transformed text becomes outlined geometry. The user is told by UI.
        result.text = None
        return result

    @classmethod
    def text_object(cls, content, x, y, size=6, family="Arial", **style):
        # Scale a font at 100 pixels into millimetre geometry, independent of DPI.
        font = QFont(family); font.setPixelSize(100)
        path = QPainterPath(); path.addText(0, 0, font, str(content)[:10000])
        transform = QTransform(); transform.translate(x, y); transform.scale(size / 100, size / 100)
        return cls("Text", commands_from_path(transform.map(path)),
                   text={"content": str(content), "x": x, "y": y, "size": size, "family": family}, **style)


class VectorDocument:
    def __init__(self, width=210, height=297, objects=None):
        self.width, self.height = finite(width, .01, 100000), finite(height, .01, 100000)
        self.objects = list(objects or [])
        self.layers = [{"name": "Artwork", "visible": True, "locked": False}]
        self.units = "mm"
        self.source_path = ""
        self.warnings = []
        self.guides = []
        self.dirty = False
        self._undo, self._redo = [], []

    def state(self):
        return {"version": 1, "format": "ENG Vector", "width_mm": self.width, "height_mm": self.height,
                "units": self.units, "layers": copy.deepcopy(self.layers),
                "objects": [asdict(obj) for obj in self.objects], "guides": copy.deepcopy(self.guides),
                "warnings": list(self.warnings)}

    @classmethod
    def from_state(cls, state):
        if state.get("format") != "ENG Vector" or state.get("version") != 1:
            raise ValueError("Unsupported ENG vector document version.")
        raw_objects = state.get("objects", [])
        if not isinstance(raw_objects, list) or len(raw_objects) > MAX_OBJECTS:
            raise ValueError("Too many vector objects.")
        if sum(len(obj.get("commands", [])) for obj in raw_objects) > MAX_NODES:
            raise ValueError("Too many vector nodes.")
        doc = cls(state["width_mm"], state["height_mm"], [VectorObject(**obj) for obj in raw_objects])
        layers = state.get("layers", [])
        if not isinstance(layers, list) or len(layers) > 1000:
            raise ValueError("Invalid layer list.")
        doc.layers = [{"name": str(layer["name"])[:256], "visible": bool(layer.get("visible", True)),
                       "locked": bool(layer.get("locked", False))} for layer in layers] or doc.layers
        doc.units = state.get("units", "mm") if state.get("units") in UNITS else "mm"
        guides = state.get("guides", [])
        if not isinstance(guides, list) or len(guides) > 1000:
            raise ValueError("Invalid guide list")
        for guide in guides:
            if not isinstance(guide, (list, tuple)) or len(guide) != 2 or guide[0] not in {"x", "y"}:
                raise ValueError("Invalid guide")
            doc.guides.append([guide[0], finite(guide[1])])
        doc.warnings = [str(w)[:2000] for w in state.get("warnings", [])[:100]]
        return doc

    def snapshot(self):
        state = self.state()
        self._undo.append(state); self._redo.clear()
        # Bounded history by node count and number of operations.
        while len(self._undo) > 40 or (len(self._undo) > 1 and sum(sum(len(o["commands"]) for o in s["objects"]) for s in self._undo) > 500000):
            self._undo.pop(0)
        self.dirty = True

    def _restore(self, state):
        restored = self.from_state(state)
        self.width, self.height = restored.width, restored.height
        self.units, self.layers, self.objects = restored.units, restored.layers, restored.objects
        self.guides, self.warnings = restored.guides, restored.warnings
        self.dirty = True

    def undo(self):
        if not self._undo: return False
        self._redo.append(self.state()); self._restore(self._undo.pop()); return True

    def redo(self):
        if not self._redo: return False
        self._undo.append(self.state()); self._restore(self._redo.pop()); return True

    def add(self, obj):
        if len(self.objects) >= MAX_OBJECTS or sum(len(o.commands) for o in self.objects) + len(obj.commands) > MAX_NODES:
            raise ValueError("Document geometry limit reached; simplify existing paths first.")
        self.snapshot(); self.objects.append(obj)
        if not any(layer["name"] == obj.layer for layer in self.layers):
            self.layers.append({"name": obj.layer, "visible": True, "locked": False})

    def transform(self, ids, transform):
        self.snapshot()
        self.objects = [obj.transformed(transform) if obj.id in ids and not self.is_locked(obj) else obj for obj in self.objects]

    def is_visible(self, obj):
        return obj.visible and next((layer["visible"] for layer in self.layers if layer["name"] == obj.layer), True)

    def is_locked(self, obj):
        return obj.locked or next((layer["locked"] for layer in self.layers if layer["name"] == obj.layer), False)

    def boolean(self, ids, operation):
        selected = [obj for obj in self.objects if obj.id in ids and not self.is_locked(obj)]
        if len(selected) < 2: raise ValueError("Select at least two unlocked paths.")
        if operation not in {"union", "subtract", "intersect", "exclude"}: raise ValueError("Unknown boolean operation.")
        path = selected[0].path()
        for obj in selected[1:]:
            if operation == "exclude":
                other = obj.path()
                path = path.united(other).subtracted(path.intersected(other))
            else:
                path = {"union": path.united, "subtract": path.subtracted, "intersect": path.intersected}[operation](obj.path())
        self.snapshot()
        self.objects = [obj for obj in self.objects if obj not in selected]
        obj = VectorObject(operation.title(), commands_from_path(path), fill=selected[0].fill,
                           stroke=selected[0].stroke, stroke_width=selected[0].stroke_width, layer=selected[0].layer)
        self.objects.append(obj)
        return obj

    def paint(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for obj in self.objects:
            if not self.is_visible(obj): continue
            painter.setOpacity(obj.opacity)
            painter.setBrush(Qt.BrushStyle.NoBrush if obj.fill == "none" else QColor(obj.fill))
            pen = QPen(QColor(obj.stroke), obj.stroke_width) if obj.stroke != "none" and obj.stroke_width > 0 else QPen(Qt.PenStyle.NoPen)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin); painter.setPen(pen)
            painter.drawPath(obj.path())
        painter.setOpacity(1)

    def render_image(self, dpi=150, transparent=True):
        dpi = finite(dpi, 1, 2400)
        width, height = max(1, round(self.width * dpi / 25.4)), max(1, round(self.height * dpi / 25.4))
        check_pixels(width, height)
        if width * height > 80_000_000: raise ValueError("Raster export is limited to 80 megapixels. Reduce DPI.")
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent if transparent else Qt.GlobalColor.white)
        painter = QPainter(image); painter.scale(width / self.width, height / self.height)
        self.paint(painter); painter.end()
        image.setDotsPerMeterX(round(dpi / .0254)); image.setDotsPerMeterY(round(dpi / .0254))
        return image

    def save(self, path, overwrite=False):
        _atomic_bytes(path, json.dumps(self.state(), ensure_ascii=False, allow_nan=False).encode(), self.source_path, overwrite)
        self.dirty = False

    @classmethod
    def load(cls, path):
        state = json.loads(_read_bytes(path))
        result = cls.from_state(state); result.source_path = str(Path(path).resolve()); return result

    def export_svg(self, path, overwrite=False):
        root = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "width": f"{self.width:.8g}mm",
                                  "height": f"{self.height:.8g}mm", "viewBox": f"0 0 {self.width:.10g} {self.height:.10g}"})
        for layer in self.layers:
            group = ET.SubElement(root, "g", {"data-eng-layer": layer["name"]})
            if not layer["visible"]: group.set("display", "none")
            for obj in self.objects:
                if obj.layer != layer["name"]: continue
                attrs = {"fill": obj.fill, "stroke": obj.stroke, "stroke-width": str(obj.stroke_width),
                         "fill-rule": obj.fill_rule, "opacity": str(obj.opacity), "data-eng-name": obj.name,
                         "data-eng-group": obj.group}
                for paint in ("fill", "stroke"):
                    if attrs[paint] != "none":
                        color = QColor(attrs[paint]); attrs[paint] = color.name()
                        if color.alpha() != 255: attrs[paint + "-opacity"] = f"{color.alphaF():.8g}"
                if not obj.visible: attrs["display"] = "none"
                if obj.text:
                    attrs.update({"x": str(obj.text["x"]), "y": str(obj.text["y"]), "font-size": str(obj.text["size"]), "font-family": obj.text["family"]})
                    element = ET.SubElement(group, "text", attrs); element.text = obj.text["content"]
                else:
                    attrs["d"] = " ".join(command[0] + " ".join(f"{v:.10g}" for v in command[1:]) for command in obj.commands)
                    ET.SubElement(group, "path", attrs)
        _atomic_bytes(path, ET.tostring(root, encoding="utf-8", xml_declaration=True), self.source_path, overwrite)

    def export_dxf(self, path, tolerance=.02, overwrite=False):
        import ezdxf
        tolerance = finite(tolerance, .001, 10)
        _check_destination(path, self.source_path, overwrite)
        drawing = ezdxf.new("R2010"); drawing.units = 4
        model = drawing.modelspace()
        for obj in self.objects:
            if not self.is_visible(obj): continue
            layer = re.sub(r'[<>/\\":;?*|=]', "_", obj.layer)[:200] or "Artwork"
            if layer not in drawing.layers: drawing.layers.new(layer)
            color = QColor(obj.stroke if obj.stroke != "none" else obj.fill if obj.fill != "none" else "#000000")
            attrs = {"layer": layer, "true_color": color.red() << 16 | color.green() << 8 | color.blue()}
            # Scale before Qt flattens curves so its approximation is bounded in mm.
            for polygon in obj.path().toSubpathPolygons(QTransform.fromScale(1 / tolerance, 1 / tolerance)):
                points = [(point.x() * tolerance, -point.y() * tolerance) for point in polygon]
                closed = len(points) > 2 and math.dist(points[0], points[-1]) < 1e-7
                if closed: points.pop()
                if len(points) > 1: model.add_lwpolyline(points, close=closed, dxfattribs=attrs)
        temp = _temp_path(path)
        try:
            drawing.saveas(temp)
            _finish_temp(temp, path, overwrite)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def export_pdf(self, path, overwrite=False):
        _check_destination(path, self.source_path, overwrite)
        temp = _temp_path(path)
        try:
            writer = QPdfWriter(temp); writer.setResolution(2540)
            writer.setPageSize(QPageSize(QSizeF(self.width, self.height), QPageSize.Unit.Millimeter))
            from PySide6.QtCore import QMarginsF
            writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
            painter = QPainter(writer)
            if not painter.isActive(): raise ValueError("Could not create PDF output.")
            painter.scale(writer.resolution() / 25.4, writer.resolution() / 25.4)
            self.paint(painter); painter.end(); del writer
            _finish_temp(temp, path, overwrite)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def cutting_checks(self, tolerance=.05):
        """Geometry checks, not machine-specific certification or toolpath generation."""
        tolerance = finite(tolerance, .001, 100)
        warnings, seen = [], set()
        for obj in self.objects:
            if not self.is_visible(obj): continue
            if obj.text: warnings.append(f"{obj.name}: outline text before sending to a cutter.")
            for polygon in obj.path().toSubpathPolygons(QTransform.fromScale(10, 10)):
                points = [(p.x() / 10, p.y() / 10) for p in polygon]
                if len(points) < 2: continue
                if math.dist(points[0], points[-1]) > tolerance:
                    warnings.append(f"{obj.name}: open contour.")
                elif len(points) < 4:
                    warnings.append(f"{obj.name}: degenerate contour.")
                if min(obj.path().boundingRect().width(), obj.path().boundingRect().height()) < tolerance:
                    warnings.append(f"{obj.name}: feature thinner than {tolerance:g} mm.")
                key = tuple((round(x / tolerance), round(y / tolerance)) for x, y in points)
                if key in seen or tuple(reversed(key)) in seen:
                    warnings.append(f"{obj.name}: duplicate contour.")
                seen.add(key)
            if obj.path().boundingRect().intersected(QRectF(0, 0, self.width, self.height)) != obj.path().boundingRect():
                warnings.append(f"{obj.name}: geometry extends beyond the page.")
        from .vector_checks import crossing_checks
        return warnings + crossing_checks([obj for obj in self.objects if self.is_visible(obj)])


def _read_bytes(path):
    if Path(path).stat().st_size > MAX_FILE_BYTES: raise ValueError("Vector files are limited to 20 MB.")
    with open(path, "rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES: raise ValueError("Vector file is too large.")
    return data


def _check_destination(path, source="", overwrite=False):
    path = Path(path)
    if source and (path.resolve() == Path(source).resolve() or path.exists() and Path(source).exists() and os.path.samefile(path, source)):
        # Native projects may explicitly overwrite themselves after confirmation.
        if not (overwrite and path.suffix.lower() == ".engvec" and Path(source).suffix.lower() == ".engvec"):
            raise ValueError("Choose a new output; the original file is protected.")
    if path.exists() and not overwrite: raise FileExistsError("Output already exists; choose another name or confirm replacement.")
    if path.is_symlink(): raise ValueError("Cannot overwrite a symbolic link.")


def _temp_path(path):
    fd, temp = tempfile.mkstemp(prefix=".eng-vector-", suffix=Path(path).suffix, dir=Path(path).parent)
    os.close(fd); return temp


def _finish_temp(temp, path, overwrite):
    if overwrite: os.replace(temp, path)
    else:
        os.link(temp, path); os.unlink(temp)  # Atomic and refuses concurrent collisions.


def _atomic_bytes(path, data, source="", overwrite=False):
    _check_destination(path, source, overwrite)
    temp = _temp_path(path)
    try:
        with open(temp, "wb") as stream: stream.write(data)
        _finish_temp(temp, path, overwrite)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def _length(value, default=0):
    if value is None: return default
    match = re.fullmatch(r"\s*(" + _NUM + r")\s*(mm|cm|in|px|pt|pc)?\s*", str(value))
    if not match: raise ValueError("SVG percentage and relative dimensions are not supported.")
    return finite(match.group(1)) * ({**UNITS, "pc": 25.4 / 6}.get(match.group(2) or "px", 1))


def parse_svg_path(data):
    """SVG path parser including cubic/quadratic shorthand and elliptical arcs."""
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUM, data)
    if re.sub(r"[\s,]+", "", re.sub(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUM, "", data)):
        raise ValueError("Invalid or unsupported SVG path syntax.")
    if len(tokens) > MAX_NODES * 7: raise ValueError("SVG has too many path nodes.")
    path, i, op, previous = QPainterPath(), 0, None, ""
    point = QPointF(); start = QPointF(); last_control = QPointF()
    lengths = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
    while i < len(tokens):
        if tokens[i].isalpha():
            op = tokens[i]; i += 1
            if op.upper() == "Z":
                path.closeSubpath(); point = QPointF(start); previous = "Z"; op = None; continue
        if not op or op.upper() not in lengths: raise ValueError("SVG path is missing a command.")
        upper, relative = op.upper(), op.islower(); count = lengths[upper]
        if i + count > len(tokens) or any(t.isalpha() for t in tokens[i:i + count]): raise ValueError("Incomplete SVG path command.")
        nums = [finite(t) for t in tokens[i:i + count]]; i += count
        def xy(a, b): return QPointF(a, b) + point if relative else QPointF(a, b)
        if upper in {"M", "L", "T"}:
            end = xy(*nums)
            if upper == "M": path.moveTo(end); start = QPointF(end)
            elif upper == "L": path.lineTo(end)
            else:
                control = point * 2 - last_control if previous in {"Q", "T"} else point
                path.quadTo(control, end); last_control = control
        elif upper in {"H", "V"}:
            end = QPointF(nums[0] + (point.x() if relative else 0), point.y()) if upper == "H" else QPointF(point.x(), nums[0] + (point.y() if relative else 0))
            path.lineTo(end)
        elif upper == "C":
            c1, c2, end = xy(*nums[:2]), xy(*nums[2:4]), xy(*nums[4:]); path.cubicTo(c1, c2, end); last_control = c2
        elif upper == "S":
            c1 = point * 2 - last_control if previous in {"C", "S"} else point
            c2, end = xy(*nums[:2]), xy(*nums[2:]); path.cubicTo(c1, c2, end); last_control = c2
        elif upper == "Q":
            control, end = xy(*nums[:2]), xy(*nums[2:]); path.quadTo(control, end); last_control = control
        else:
            end = xy(*nums[5:]); _arc_to(path, point, end, *nums[:5])
        point, previous = end, upper
        if upper == "M": op = "l" if relative else "L"
    return path


def _arc_to(path, start, end, rx, ry, degrees, large, sweep):
    if large not in (0, 1) or sweep not in (0, 1): raise ValueError("SVG arc flags must be zero or one.")
    rx, ry = abs(rx), abs(ry)
    if start == end: return
    if rx == 0 or ry == 0: path.lineTo(end); return
    angle = math.radians(degrees % 360); ca, sa = math.cos(angle), math.sin(angle)
    dx, dy = (start.x() - end.x()) / 2, (start.y() - end.y()) / 2
    xp, yp = ca * dx + sa * dy, -sa * dx + ca * dy
    factor = (xp / rx) ** 2 + (yp / ry) ** 2
    if factor > 1: rx *= math.sqrt(factor); ry *= math.sqrt(factor)
    denominator = rx * rx * yp * yp + ry * ry * xp * xp
    ratio = max(0, (rx * rx * ry * ry - denominator) / max(denominator, 1e-30))
    coefficient = (-1 if large == sweep else 1) * math.sqrt(ratio)
    cxp, cyp = coefficient * rx * yp / ry, -coefficient * ry * xp / rx
    cx = ca * cxp - sa * cyp + (start.x() + end.x()) / 2
    cy = sa * cxp + ca * cyp + (start.y() + end.y()) / 2
    theta = math.atan2((yp - cyp) / ry, (xp - cxp) / rx)
    end_theta = math.atan2((-yp - cyp) / ry, (-xp - cxp) / rx)
    delta = (end_theta - theta) % (2 * math.pi)
    if not sweep and delta > 0: delta -= 2 * math.pi
    steps = max(1, math.ceil(abs(delta) / (math.pi / 2))); step = delta / steps
    def point(t): return QPointF(cx + ca * rx * math.cos(t) - sa * ry * math.sin(t), cy + sa * rx * math.cos(t) + ca * ry * math.sin(t))
    def derivative(t): return QPointF(-ca * rx * math.sin(t) - sa * ry * math.cos(t), -sa * rx * math.sin(t) + ca * ry * math.cos(t))
    for j in range(steps):
        a, b = theta + j * step, theta + (j + 1) * step
        k = 4 / 3 * math.tan(step / 4)
        path.cubicTo(point(a) + derivative(a) * k, point(b) - derivative(b) * k, end if j == steps - 1 else point(b))


def _svg_transform(value):
    result = QTransform()
    consumed = ""
    for match in re.finditer(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", value or ""):
        consumed += match.group(0)
        values = [finite(v) for v in re.findall(_NUM, match.group(2))]
        kind = match.group(1); t = QTransform()
        if kind == "matrix" and len(values) == 6: t = QTransform(*values)
        elif kind == "translate" and len(values) in (1, 2): t.translate(values[0], values[1] if len(values) == 2 else 0)
        elif kind == "scale" and len(values) in (1, 2): t.scale(values[0], values[-1])
        elif kind == "rotate" and len(values) in (1, 3):
            if len(values) == 3: t.translate(*values[1:])
            t.rotate(values[0])
            if len(values) == 3: t.translate(-values[1], -values[2])
        elif kind == "skewX" and len(values) == 1: t.shear(math.tan(math.radians(values[0])), 0)
        elif kind == "skewY" and len(values) == 1: t.shear(0, math.tan(math.radians(values[0])))
        else: raise ValueError("Unsupported SVG transformation.")
        result = t * result
    if re.sub(r"\s|,", "", consumed) != re.sub(r"\s|,", "", value or ""): raise ValueError("Invalid SVG transformation.")
    return result


def import_svg(path):
    from defusedxml import ElementTree as SafeET
    root = SafeET.fromstring(_read_bytes(path))
    if root.tag.split("}")[-1] != "svg": raise ValueError("This is not an SVG document.")
    view = [finite(v) for v in re.findall(_NUM, root.get("viewBox", ""))]
    width = _length(root.get("width"), (view[2] if len(view) == 4 else 793.7) * UNITS["px"])
    height = _length(root.get("height"), (view[3] if len(view) == 4 else 1122.5) * UNITS["px"])
    doc = VectorDocument(width, height); doc.layers = []
    transform = QTransform()
    if len(view) == 4:
        if view[2] <= 0 or view[3] <= 0: raise ValueError("Invalid SVG viewBox.")
        sx, sy = width / view[2], height / view[3]
        if root.get("preserveAspectRatio", "xMidYMid meet") != "none":
            ratio = max(sx, sy) if "slice" in root.get("preserveAspectRatio", "") else min(sx, sy)
            transform.translate((width - view[2] * ratio) / 2, (height - view[3] * ratio) / 2); sx = sy = ratio
        transform.scale(sx, sy); transform.translate(-view[0], -view[1])
    else: transform.scale(UNITS["px"], UNITS["px"])
    unsupported = set()
    def visit(element, parent_transform, inherited, layer="Artwork", depth=0):
        if depth > 64: raise ValueError("SVG groups are nested too deeply.")
        tag = element.tag.split("}")[-1]
        if tag in {"metadata", "title", "desc", "defs"}: return
        style = dict(inherited)
        style.update({k.strip(): v.strip() for k, _, v in (s.partition(":") for s in element.get("style", "").split(";")) if k})
        style.update({key: element.get(key) for key in ("fill", "stroke", "stroke-width", "opacity", "display", "fill-rule", "font-size", "font-family",
                      "clip-path", "mask", "filter", "stroke-dasharray", "text-anchor", "fill-opacity", "stroke-opacity") if element.get(key) is not None})
        for effect in ("clip-path", "mask", "filter", "stroke-dasharray"):
            if style.get(effect) not in (None, "none", "0"): unsupported.add(effect)
        if style.get("text-anchor") not in (None, "start"): unsupported.add("text-anchor alignment")
        local = _svg_transform(element.get("transform", "")) * parent_transform
        if tag in {"svg", "g"}:
            layer = element.get("data-eng-layer", layer)
            for child in element: visit(child, local, style, layer, depth + 1)
            return
        p = QPainterPath(); text_data = None
        def val(name, default="0"): return finite(element.get(name, default))
        if tag == "path": p = parse_svg_path(element.get("d", ""))
        elif tag == "rect":
            r = QRectF(val("x"), val("y"), val("width"), val("height"))
            if val("rx") or val("ry"): p.addRoundedRect(r, val("rx"), val("ry", element.get("rx", "0")))
            else: p.addRect(r)
        elif tag in {"circle", "ellipse"}:
            rx = val("r") if tag == "circle" else val("rx"); ry = val("r") if tag == "circle" else val("ry")
            p.addEllipse(QPointF(val("cx"), val("cy")), rx, ry)
        elif tag == "line": p.moveTo(val("x1"), val("y1")); p.lineTo(val("x2"), val("y2"))
        elif tag in {"polygon", "polyline"}:
            coords = [finite(v) for v in re.findall(_NUM, element.get("points", ""))]
            if len(coords) % 2: raise ValueError("Invalid SVG polygon.")
            for i in range(0, len(coords), 2):
                p.moveTo(*coords[i:i + 2]) if i == 0 else p.lineTo(*coords[i:i + 2])
            if tag == "polygon": p.closeSubpath()
        elif tag == "text":
            if list(element): unsupported.add("nested text/tspan"); return
            content = element.text or ""
            size = finite(style.get("font-size", "16"), .1, 10000)
            p = VectorObject.text_object(content, val("x"), val("y"), size, style.get("font-family", "Arial")).path()
            # Retain editable text for ordinary translation+uniform scaling only.
            if abs(local.m12()) < 1e-8 and abs(local.m21()) < 1e-8 and abs(local.m11() - local.m22()) < 1e-8:
                anchor = local.map(QPointF(val("x"), val("y")))
                text_data = {"content": content, "x": anchor.x(), "y": anchor.y(), "size": abs(size * local.m11()), "family": style.get("font-family", "Arial")}
        else: unsupported.add(tag); return
        if p.isEmpty(): return
        fill, stroke = style.get("fill", "#000000"), style.get("stroke", "none")
        if "url(" in fill or "url(" in stroke: unsupported.add("gradient/pattern paint"); return
        fill = _svg_paint(fill, style.get("fill-opacity", "1")); stroke = _svg_paint(stroke, style.get("stroke-opacity", "1"))
        p = local.map(p)
        scale = math.sqrt(abs(local.determinant()))
        obj = VectorObject(element.get("data-eng-name", tag.title()), commands_from_path(p), fill, stroke,
                           finite(style.get("stroke-width", 1), 0, 1000) * scale,
                           finite(style.get("opacity", 1), 0, 1), layer,
                           group=element.get("data-eng-group", ""), visible=style.get("display") != "none", text=text_data,
                           fill_rule=style.get("fill-rule", "nonzero"))
        if len(doc.objects) >= MAX_OBJECTS: raise ValueError("Too many SVG objects.")
        doc.objects.append(obj)
        if not any(item["name"] == layer for item in doc.layers): doc.layers.append({"name": layer, "visible": True, "locked": False})
    visit(root, transform, {})
    if sum(len(obj.commands) for obj in doc.objects) > MAX_NODES: raise ValueError("SVG has too many nodes.")
    if unsupported: doc.warnings.append("Not imported: " + ", ".join(sorted(unsupported)) + ". Review appearance before saving.")
    if not doc.layers: doc.layers = [{"name": "Artwork", "visible": True, "locked": False}]
    doc.source_path = str(Path(path).resolve())
    return doc


def import_dxf(path):
    import ezdxf
    from ezdxf import path as dxf_path
    _read_bytes(path)
    drawing = ezdxf.readfile(path); doc = VectorDocument(); doc.layers = []
    units = drawing.units
    factor = ezdxf.units.conversion_factor(units, 4) if units else 1
    if not units: doc.warnings.append("DXF has no declared units; interpreted as millimetres. Check or calibrate dimensions.")
    unsupported = set()
    for entity in drawing.modelspace():
        if len(doc.objects) >= MAX_OBJECTS: raise ValueError("Too many DXF entities.")
        kind = entity.dxftype(); layer = entity.dxf.layer
        if kind in {"TEXT", "MTEXT"}:
            position = entity.dxf.insert
            content = entity.dxf.text if kind == "TEXT" else entity.plain_text()
            size = (entity.dxf.height if kind == "TEXT" else entity.dxf.char_height) * factor
            obj = VectorObject.text_object(content, position.x * factor, -position.y * factor, size,
                                           fill="#202936", stroke="none", layer=layer)
        elif kind in {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE"}:
            source = dxf_path.make_path(entity)
            commands = [["M", source.start.x * factor, -source.start.y * factor]]
            for command in source:
                end = command.end
                if hasattr(command, "ctrl1"):
                    commands.append(["C", command.ctrl1.x * factor, -command.ctrl1.y * factor,
                                     command.ctrl2.x * factor, -command.ctrl2.y * factor, end.x * factor, -end.y * factor])
                elif hasattr(command, "ctrl"):
                    commands.append(["Q", command.ctrl.x * factor, -command.ctrl.y * factor, end.x * factor, -end.y * factor])
                else: commands.append(["M" if type(command).__name__ == "MoveTo" else "L", end.x * factor, -end.y * factor])
            if source.is_closed: commands.append(["Z"])
            obj = VectorObject(kind, commands, fill="none", stroke="#202936", layer=layer)
        else: unsupported.add(kind); continue
        doc.objects.append(obj)
        if not any(item["name"] == layer for item in doc.layers):
            definition = drawing.layers.get(layer)
            doc.layers.append({"name": layer, "visible": not definition.is_off(), "locked": definition.is_locked()})
    if doc.objects:
        bounds = doc.objects[0].path().boundingRect()
        for obj in doc.objects[1:]: bounds = bounds.united(obj.path().boundingRect())
        # Preserve world coordinates. Page can encompass negative drawing origins;
        # the UI offers Fit Artboard, which translates geometry explicitly.
        doc.width = max(1, bounds.right()); doc.height = max(1, bounds.bottom())
        if bounds.left() < 0 or bounds.top() < 0: doc.warnings.append("Drawing uses negative coordinates; Fit Artboard will place it on the page without changing dimensions.")
    if unsupported: doc.warnings.append("Not imported DXF entities: " + ", ".join(sorted(unsupported)) + ".")
    if sum(len(obj.commands) for obj in doc.objects) > MAX_NODES: raise ValueError("DXF has too many nodes.")
    if not doc.layers: doc.layers = [{"name": "Artwork", "visible": True, "locked": False}]
    doc.source_path = str(Path(path).resolve()); return doc
