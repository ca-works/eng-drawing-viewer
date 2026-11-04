"""Geometry for on-canvas layer transforms (no Qt).

A layer frame is ``(cx, cy, w, h, degrees)``: the centre, signed size and clockwise turn of
the layer's content on the canvas. Handles are named by their box position ``(s, t)`` with
s and t in {-0.5, 0, 0.5}; dragging one returns a new frame, and ``frame_delta`` gives the
canvas-space affine transform from the old frame to the new one.
"""
from __future__ import annotations

import math

from ...core.image_document import affine_invert, affine_multiply

HANDLES = [(-0.5, -0.5), (0.0, -0.5), (0.5, -0.5), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5), (-0.5, 0.5), (-0.5, 0.0)]
ROTATE_OFFSET = 26  # screen pixels between the top edge and the rotation handle
MIN_SIZE = 1.0


def frame_matrix(frame):
    """Affine map from box coordinates (s, t) ∈ [-½, ½]² to the canvas."""
    cx, cy, w, h, degrees = frame
    r = math.radians(degrees)
    c, s = math.cos(r), math.sin(r)
    return (w * c, -h * s, cx, w * s, h * c, cy)


def frame_point(frame, s, t):
    a, b, c, d, e, f = frame_matrix(frame)
    return a * s + b * t + c, d * s + e * t + f


def to_box(frame, x, y):
    """Canvas point → box-aligned offset from the centre (unscaled pixels along the box axes)."""
    cx, cy, _w, _h, degrees = frame
    r = math.radians(degrees)
    dx, dy = x - cx, y - cy
    return dx * math.cos(r) + dy * math.sin(r), -dx * math.sin(r) + dy * math.cos(r)


def corners(frame):
    return [frame_point(frame, s, t) for s, t in ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5))]


def bounds(frame):
    pts = corners(frame)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def rotate_handle(frame, zoom):
    """Canvas position of the rotation knob, a fixed screen distance above the top edge."""
    cx, cy, w, h, degrees = frame
    top = frame_point(frame, 0.0, -0.5 if h >= 0 else 0.5)
    r = math.radians(degrees)
    # "Up" along the box's own vertical axis.
    ux, uy = math.sin(r), -math.cos(r)
    distance = ROTATE_OFFSET / max(zoom, 1e-6)
    return top[0] + ux * distance, top[1] + uy * distance


def hit_test(frame, x, y, zoom, radius=7):
    """What a press at canvas (x, y) grabs: "rotate", a handle (s, t), "inside", "rotate-zone" or None."""
    if frame is None:
        return None
    reach = radius / max(zoom, 1e-6)
    rx, ry = rotate_handle(frame, zoom)
    if math.hypot(x - rx, y - ry) <= reach * 1.3:
        return "rotate"
    for handle in HANDLES:
        hx, hy = frame_point(frame, *handle)
        if abs(x - hx) <= reach and abs(y - hy) <= reach:
            return handle
    u, v = to_box(frame, x, y)
    half_w, half_h = abs(frame[2]) / 2, abs(frame[3]) / 2
    if abs(u) <= half_w and abs(v) <= half_h:
        return "inside"
    # Just outside a corner, the pointer rotates (as in Photoshop).
    if abs(u) <= half_w + 3 * reach and abs(v) <= half_h + 3 * reach and (abs(u) > half_w and abs(v) > half_h):
        return "rotate-zone"
    return None


def scale_frame(frame, handle, x, y, keep_aspect=True, from_center=False):
    """The frame after dragging ``handle`` to canvas (x, y).

    The opposite handle stays put (the centre with ``from_center``). Corner drags keep
    the aspect ratio unless ``keep_aspect`` is False; edge drags change one dimension.
    Dragging past the opposite side mirrors the layer.
    """
    cx, cy, w, h, degrees = frame
    hs, ht = handle
    u, v = to_box(frame, x, y)
    if from_center:
        # The handle sits at hs·w from the centre.
        new_w = u / hs if hs else w
        new_h = v / ht if ht else h
    else:
        # The handle sits 2·hs·w from the opposite (anchor) handle at -hs·w.
        new_w = (u + hs * w) / (2 * hs) if hs else w
        new_h = (v + ht * h) / (2 * ht) if ht else h
    if keep_aspect and hs and ht and w and h:
        k = (new_w * w + new_h * h) / (w * w + h * h)
        new_w, new_h = k * w, k * h
    new_w = math.copysign(max(MIN_SIZE, abs(new_w)), new_w or 1)
    new_h = math.copysign(max(MIN_SIZE, abs(new_h)), new_h or 1)
    if from_center:
        offset_u = offset_v = 0.0
    else:
        offset_u = hs * (new_w - w) if hs else 0.0
        offset_v = ht * (new_h - h) if ht else 0.0
    r = math.radians(degrees)
    ncx = cx + offset_u * math.cos(r) - offset_v * math.sin(r)
    ncy = cy + offset_u * math.sin(r) + offset_v * math.cos(r)
    return ncx, ncy, new_w, new_h, degrees


def rotate_frame(frame, start, current, snap=False):
    """The frame turned by the angle the pointer swept around its centre (15° steps with ``snap``)."""
    cx, cy, w, h, degrees = frame
    a0 = math.degrees(math.atan2(start[1] - cy, start[0] - cx))
    a1 = math.degrees(math.atan2(current[1] - cy, current[0] - cx))
    angle = degrees + a1 - a0
    if snap:
        angle = round(angle / 15) * 15
    angle = (angle + 180) % 360 - 180
    return cx, cy, w, h, angle


def frame_delta(old, new):
    """Canvas-space affine transform taking the ``old`` frame onto the ``new`` one."""
    return affine_multiply(affine_invert(frame_matrix(old)), frame_matrix(new))


def snap_move(frame, dx, dy, targets_x, targets_y, threshold):
    """Adjust a move so the frame's edges or centre land on nearby targets.

    Returns (dx, dy, lines): lines are ("x", value) / ("y", value) guides to draw.
    """
    left, top, right, bottom = bounds(frame)
    lines = []
    best = None
    for edge in (left + dx, (left + right) / 2 + dx, right + dx):
        for target in targets_x:
            gap = target - edge
            if abs(gap) <= threshold and (best is None or abs(gap) < abs(best[0])):
                best = (gap, target)
    if best is not None:
        dx += best[0]
        lines.append(("x", best[1]))
    best = None
    for edge in (top + dy, (top + bottom) / 2 + dy, bottom + dy):
        for target in targets_y:
            gap = target - edge
            if abs(gap) <= threshold and (best is None or abs(gap) < abs(best[0])):
                best = (gap, target)
    if best is not None:
        dy += best[0]
        lines.append(("y", best[1]))
    return dx, dy, lines


def handle_cursor_angle(frame, handle):
    """Screen angle (degrees) of a handle's resize direction, for choosing a cursor."""
    hs, ht = handle
    return (math.degrees(math.atan2(ht, hs)) + frame[4]) % 180
