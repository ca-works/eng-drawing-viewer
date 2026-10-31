"""Bounded segment checks for cutting preparation; never a machine certification."""
import math
from PySide6.QtGui import QTransform


def crossing_checks(objects, max_segments=20000, max_comparisons=200000):
    segments, warnings, canonical = [], [], set()
    contour_id = 0
    for obj in objects:
        for polygon in obj.path().toSubpathPolygons(QTransform.fromScale(20, 20)):
            points = [(p.x() / 20, p.y() / 20) for p in polygon]
            if len(points) < 2:
                continue
            closed = math.dist(points[0], points[-1]) < 1e-7
            distinct = points[:-1] if closed else points
            keypoints = [(round(x, 5), round(y, 5)) for x, y in distinct]
            def cycle(values):
                k = min(range(len(values)), key=values.__getitem__)
                return tuple(values[k:] + values[:k])
            key = min(cycle(keypoints), cycle(list(reversed(keypoints)))) if closed else min(tuple(keypoints), tuple(reversed(keypoints)))
            if key in canonical:
                warnings.append(f'{obj.name}: duplicate contour (including rotated start point).')
            canonical.add(key)
            count = len(points) - 1
            for index, (a, b) in enumerate(zip(points, points[1:])):
                if math.dist(a, b) < 1e-9:
                    continue
                segments.append((a, b, contour_id, index, count, closed, obj.name))
                if len(segments) > max_segments:
                    return warnings + ['Intersection check incomplete: simplify geometry below 20,000 segments.']
            contour_id += 1
    if not segments:
        return warnings
    xs = [p[0] for s in segments for p in s[:2]]
    ys = [p[1] for s in segments for p in s[:2]]
    cell = max(.1, math.sqrt(max(1, (max(xs) - min(xs)) * (max(ys) - min(ys))) / len(segments)))
    grid, tested = {}, set()
    checks = cells = 0
    def intersects(a, b, c, d):
        if max(a[0], b[0]) < min(c[0], d[0]) - 1e-9 or max(c[0], d[0]) < min(a[0], b[0]) - 1e-9:
            return False
        if max(a[1], b[1]) < min(c[1], d[1]) - 1e-9 or max(c[1], d[1]) < min(a[1], b[1]) - 1e-9:
            return False
        def cross(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        u, v, w, z = cross(a,b,c), cross(a,b,d), cross(c,d,a), cross(c,d,b)
        return u * v <= 1e-12 and w * z <= 1e-12
    for i, s in enumerate(segments):
        a, b, contour, index, count, closed, name = s
        x0, x1 = math.floor(min(a[0], b[0]) / cell), math.floor(max(a[0], b[0]) / cell)
        y0, y1 = math.floor(min(a[1], b[1]) / cell), math.floor(max(a[1], b[1]) / cell)
        cells += (x1 - x0 + 1) * (y1 - y0 + 1)
        if cells > 300000:
            return warnings + ['Intersection check incomplete: geometry exceeds the spatial-index budget.']
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                bucket = grid.setdefault((x, y), [])
                for j in bucket:
                    if (j, i) in tested:
                        continue
                    tested.add((j, i))
                    t = segments[j]
                    if contour == t[2] and (abs(index - t[3]) == 1 or (closed and {index, t[3]} == {0, count - 1})):
                        continue
                    checks += 1
                    if checks > max_comparisons:
                        return warnings + ['Intersection check incomplete: simplify overlapping geometry.']
                    if intersects(a, b, t[0], t[1]):
                        warnings.append(f'{name}: ' + ('self-intersecting or overlapping contour.' if contour == t[2] else f'crosses or touches {t[6]}.'))
                        if len(warnings) >= 30:
                            return list(dict.fromkeys(warnings)) + ['Additional intersections may exist; showing a bounded sample.']
                bucket.append(i)
    return list(dict.fromkeys(warnings))
