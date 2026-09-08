"""Rasterize a page of SVG (MuseScore / Inkscape / browser export) to PIL.

Score SVGs are almost all polylines and cubic paths, no bitmap. We draw
them ourselves so TABifier does not need Cairo. Optional: cairosvg,
svglib, Inkscape, or ImageMagick if they are installed.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from tab_model import TabError

_NS = re.compile(r"\{[^}]+\}")
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_CMD = set("MmLlHhVvCcSsQqTtAaZz")


def svg_to_pil(path, *, target=2800):
    """Render one SVG file to an RGB PIL image (white background)."""
    path = Path(path)
    im = _render_builtin(path, target)
    if im is not None:
        return im
    im = _render_fallback(path)
    if im is not None:
        return im
    raise TabError(
        f"could not read SVG '{path.name}'. "
        "Export a PDF or PNG from the same score, or install cairosvg."
    )


def _local(tag):
    return _NS.sub("", tag)


def _parse_len(s, default=None):
    if s is None or s == "":
        return default
    s = str(s).strip()
    m = re.match(r"^([-+0-9.eE]+)(px|pt|mm|cm|in)?$", s)
    if not m:
        return default
    v = float(m.group(1))
    u = m.group(2) or "px"
    return {
        "px": v,
        "pt": v * 96 / 72,
        "mm": v / 25.4 * 96,
        "cm": v / 2.54 * 96,
        "in": v * 96,
    }[u]


def _colour(s):
    if s is None:
        return None
    s = s.strip().lower()
    if s in ("none", "transparent"):
        return None
    named = {"black": (0, 0, 0), "white": (255, 255, 255)}
    if s in named:
        return named[s]
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) >= 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return (0, 0, 0)


def _is_white(rgb):
    return rgb is not None and min(rgb) >= 250


def _viewbox(root):
    vb = root.get("viewBox") or root.get("viewbox")
    if vb:
        parts = [float(x) for x in vb.replace(",", " ").split()]
        if len(parts) == 4:
            return parts
    w = _parse_len(root.get("width"), 800)
    h = _parse_len(root.get("height"), 600)
    return [0.0, 0.0, w, h]


def _tokens(d):
    out = []
    i, n = 0, len(d)
    while i < n:
        c = d[i]
        if c in _CMD:
            out.append(c)
            i += 1
        elif c in " \t\r\n,":
            i += 1
        else:
            m = _NUM.match(d, i)
            if not m:
                i += 1
                continue
            out.append(float(m.group()))
            i = m.end()
    return out


def _cubic(p0, p1, p2, p3, steps=10):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = (u * u * u * p0[0] + 3 * u * u * t * p1[0]
             + 3 * u * t * t * p2[0] + t * t * t * p3[0])
        y = (u * u * u * p0[1] + 3 * u * u * t * p1[1]
             + 3 * u * t * t * p2[1] + t * t * t * p3[1])
        pts.append((x, y))
    return pts


def _quad(p0, p1, p2, steps=8):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _arc_points(p0, rx, ry, phi_deg, large, sweep, p1, steps=16):
    """Flatten an SVG elliptical arc to line segments (endpoint parameterization)."""
    x1, y1 = p0
    x2, y2 = p1
    rx, ry = abs(rx), abs(ry)
    if rx < 1e-6 or ry < 1e-6 or (x1 == x2 and y1 == y2):
        return [p0, p1]
    phi = math.radians(phi_deg)
    ca, sa = math.cos(phi), math.sin(phi)
    dx = (x1 - x2) / 2
    dy = (y1 - y2) / 2
    x1p = ca * dx + sa * dy
    y1p = -sa * dx + ca * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0, num / den))
    if int(large) == int(sweep):
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = coef * -ry * x1p / rx
    cx = ca * cxp - sa * cyp + (x1 + x2) / 2
    cy = sa * cxp + ca * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        n1 = math.hypot(ux, uy)
        n2 = math.hypot(vx, vy)
        if n1 < 1e-12 or n2 < 1e-12:
            return 0.0
        cos = max(-1.0, min(1.0, (ux * vx + uy * vy) / (n1 * n2)))
        ang = math.acos(cos)
        if ux * vy - uy * vx < 0:
            ang = -ang
        return ang

    th1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if int(sweep) == 0 and dth > 0:
        dth -= 2 * math.pi
    elif int(sweep) == 1 and dth < 0:
        dth += 2 * math.pi
    pts = [p0]
    n = max(4, int(round(steps * abs(dth) / math.pi)))
    for i in range(1, n + 1):
        th = th1 + dth * i / n
        x = ca * rx * math.cos(th) - sa * ry * math.sin(th) + cx
        y = sa * rx * math.cos(th) + ca * ry * math.sin(th) + cy
        pts.append((x, y))
    pts[-1] = p1
    return pts


def _path_subpaths(d):
    """Return a list of polylines (each a list of (x,y)) from an SVG path."""
    toks = _tokens(d)
    sub, cur, start, prev_c = [], None, None, None
    cmd, i = "M", 0

    def take(k):
        """Next k numbers, or None. Never eats a command letter."""
        nonlocal i
        if i + k > len(toks):
            return None
        vals = toks[i:i + k]
        if any(isinstance(x, str) for x in vals):
            return None
        i += k
        return vals

    paths = []

    def flush():
        nonlocal sub
        if len(sub) >= 2:
            paths.append(sub)
        sub = []

    while i < len(toks):
        t = toks[i]
        if isinstance(t, str):
            cmd = t
            i += 1
            if cmd in "Zz":
                if start is not None and sub:
                    sub.append(start)
                flush()
                cur = start
                prev_c = None
            continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            v = take(2)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            x, y = v
            if rel and cur is not None:
                x += cur[0]
                y += cur[1]
            flush()
            cur = start = (x, y)
            sub = [cur]
            cmd = "l" if rel else "L"
            prev_c = None
        elif c == "L":
            v = take(2)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            x, y = v
            if rel and cur is not None:
                x += cur[0]
                y += cur[1]
            cur = (x, y)
            sub.append(cur)
            prev_c = None
        elif c == "H":
            v = take(1)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            if cur is None:
                break
            x = v[0] + (cur[0] if rel else 0)
            cur = (x, cur[1])
            sub.append(cur)
            prev_c = None
        elif c == "V":
            v = take(1)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            if cur is None:
                break
            y = v[0] + (cur[1] if rel else 0)
            cur = (cur[0], y)
            sub.append(cur)
            prev_c = None
        elif c == "C":
            v = take(6)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            if cur is None:
                break
            x1, y1, x2, y2, x, y = v
            if rel:
                x1 += cur[0]
                y1 += cur[1]
                x2 += cur[0]
                y2 += cur[1]
                x += cur[0]
                y += cur[1]
            sub.extend(_cubic(cur, (x1, y1), (x2, y2), (x, y))[1:])
            prev_c = (x2, y2)
            cur = (x, y)
        elif c == "S":
            v = take(4)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            if cur is None:
                break
            x2, y2, x, y = v
            if rel:
                x2 += cur[0]
                y2 += cur[1]
                x += cur[0]
                y += cur[1]
            if prev_c is None:
                x1, y1 = cur
            else:
                x1 = 2 * cur[0] - prev_c[0]
                y1 = 2 * cur[1] - prev_c[1]
            sub.extend(_cubic(cur, (x1, y1), (x2, y2), (x, y))[1:])
            prev_c = (x2, y2)
            cur = (x, y)
        elif c == "Q":
            v = take(4)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            if cur is None:
                break
            x1, y1, x, y = v
            if rel:
                x1 += cur[0]
                y1 += cur[1]
                x += cur[0]
                y += cur[1]
            sub.extend(_quad(cur, (x1, y1), (x, y))[1:])
            prev_c = (x1, y1)
            cur = (x, y)
        elif c == "T":
            v = take(2)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            if cur is None:
                break
            x, y = v
            if rel:
                x += cur[0]
                y += cur[1]
            if prev_c is None:
                x1, y1 = cur
            else:
                x1 = 2 * cur[0] - prev_c[0]
                y1 = 2 * cur[1] - prev_c[1]
            sub.extend(_quad(cur, (x1, y1), (x, y))[1:])
            prev_c = (x1, y1)
            cur = (x, y)
        elif c == "A":
            v = take(7)
            if v is None:
                if i < len(toks) and isinstance(toks[i], str):
                    continue
                break
            if cur is None:
                break
            rx, ry, phi, large, sweep, x, y = v
            if rel:
                x += cur[0]
                y += cur[1]
            sub.extend(_arc_points(cur, rx, ry, phi, large, sweep, (x, y))[1:])
            cur = (x, y)
            prev_c = None
        else:
            i += 1
    flush()
    return paths


def _pts(s):
    nums = [float(x) for x in _NUM.findall(s or "")]
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]


def _style(el, parent):
    st = dict(parent)
    raw = el.get("style") or ""
    for part in raw.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            st[k.strip()] = v.strip()
    for k in ("fill", "stroke", "stroke-width", "fill-rule"):
        if el.get(k) is not None:
            st[k] = el.get(k)
    return st


def _parse_transform(s):
    """Return a 2D affine matrix (a,b,c,d,e,f) for x' = ax+cy+e, y' = bx+dy+f."""
    if not s:
        return (1, 0, 0, 1, 0, 0)
    m = (1, 0, 0, 1, 0, 0)
    for fn, args in re.findall(r"([a-zA-Z]+)\s*\(([^)]*)\)", s):
        n = [float(x) for x in _NUM.findall(args)]
        if fn == "matrix" and len(n) == 6:
            t = tuple(n)
        elif fn == "translate":
            t = (1, 0, 0, 1, n[0], n[1] if len(n) > 1 else 0)
        elif fn == "scale":
            sx = n[0]
            sy = n[1] if len(n) > 1 else sx
            t = (sx, 0, 0, sy, 0, 0)
        elif fn == "rotate":
            a = math.radians(n[0])
            ca, sa = math.cos(a), math.sin(a)
            t = (ca, sa, -sa, ca, 0, 0)
            if len(n) >= 3:
                cx, cy = n[1], n[2]
                t = _mm((1, 0, 0, 1, cx, cy), _mm(t, (1, 0, 0, 1, -cx, -cy)))
        else:
            continue
        m = _mm(m, t)
    return m


def _mm(A, B):
    a, b, c, d, e, f = A
    a2, b2, c2, d2, e2, f2 = B
    return (a * a2 + c * b2, b * a2 + d * b2,
            a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _xf(m, p):
    a, b, c, d, e, f = m
    x, y = p
    return (a * x + c * y + e, b * x + d * y + f)


def _render_builtin(path, target):
    try:
        from PIL import Image, ImageDraw, ImageChops
    except ImportError:
        return None
    try:
        root = ET.parse(str(path)).getroot()
    except ET.ParseError:
        return None
    vx, vy, vw, vh = _viewbox(root)
    if vw <= 0 or vh <= 0:
        return None
    w_attr = root.get("width") or ""
    if str(w_attr).endswith("mm"):
        out_w = float(str(w_attr)[:-2]) / 25.4 * 300
    else:
        out_w = target
    scale = out_w / vw
    W = max(8, int(round(vw * scale)))
    H = max(8, int(round(vh * scale)))
    # keep a reasonable cap so a huge viewBox cannot explode memory
    cap = 4500
    if max(W, H) > cap:
        scale *= cap / max(W, H)
        W = max(8, int(round(vw * scale)))
        H = max(8, int(round(vh * scale)))

    def px(p):
        x, y = p
        return (int(round((x - vx) * scale)), int(round((y - vy) * scale)))

    rgb = Image.new("RGB", (W, H), (255, 255, 255))
    ink = Image.new("1", (W, H), 0)
    n_drawn = [0]

    def draw_poly(pts, st, xf):
        if len(pts) < 2:
            return
        mapped = [px(_xf(xf, p)) for p in pts]
        fill = _colour(st.get("fill", "none"))
        stroke = _colour(st.get("stroke"))
        if fill is None and stroke is None:
            # SVG default fill is black
            fill = (0, 0, 0)
        sw = st.get("stroke-width")
        try:
            width = max(1, int(round(float(sw) * scale))) if sw else 1
        except ValueError:
            width = 1
        if fill is not None and not _is_white(fill) and len(mapped) >= 3:
            tmp = Image.new("1", (W, H), 0)
            ImageDraw.Draw(tmp).polygon(mapped, fill=1)
            nonlocal_xor(tmp)
            n_drawn[0] += 1
        if stroke is not None and not _is_white(stroke):
            ImageDraw.Draw(ink).line(mapped, fill=1, width=width)
            n_drawn[0] += 1

    def nonlocal_xor(tmp):
        dest = ink
        dest.paste(ImageChops.logical_xor(dest, tmp))

    def walk(el, st, xf):
        st = _style(el, st)
        xf = _mm(xf, _parse_transform(el.get("transform")))
        name = _local(el.tag)
        if name == "polyline":
            draw_poly(_pts(el.get("points", "")), st, xf)
        elif name == "polygon":
            pts = _pts(el.get("points", ""))
            if pts:
                pts = pts + [pts[0]]
            draw_poly(pts, st, xf)
        elif name == "line":
            p0 = (float(el.get("x1", 0)), float(el.get("y1", 0)))
            p1 = (float(el.get("x2", 0)), float(el.get("y2", 0)))
            draw_poly([p0, p1], st, xf)
        elif name == "rect":
            x = float(el.get("x", 0))
            y = float(el.get("y", 0))
            w = float(el.get("width", 0))
            h = float(el.get("height", 0))
            draw_poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)],
                      st, xf)
        elif name == "circle":
            cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
            r = float(el.get("r", 0))
            pts = [(cx + r * math.cos(a), cy + r * math.sin(a))
                   for a in (i * 2 * math.pi / 24 for i in range(25))]
            draw_poly(pts, st, xf)
        elif name == "path":
            d = el.get("d") or ""
            fill = _colour(st.get("fill", "#000"))
            if fill is not None and not _is_white(fill):
                xor_acc = Image.new("1", (W, H), 0)
                for poly in _path_subpaths(d):
                    mapped = [px(_xf(xf, p)) for p in poly]
                    if len(mapped) >= 3:
                        tmp = Image.new("1", (W, H), 0)
                        ImageDraw.Draw(tmp).polygon(mapped, fill=1)
                        xor_acc = ImageChops.logical_xor(xor_acc, tmp)
                        n_drawn[0] += 1
                dest = ink
                dest.paste(ImageChops.logical_xor(dest, xor_acc))
            stroke = _colour(st.get("stroke"))
            if stroke is not None and not _is_white(stroke):
                try:
                    width = max(1, int(round(float(st.get("stroke-width", 1))
                                             * scale)))
                except ValueError:
                    width = 1
                for poly in _path_subpaths(d):
                    mapped = [px(_xf(xf, p)) for p in poly]
                    if len(mapped) >= 2:
                        ImageDraw.Draw(ink).line(mapped, fill=1, width=width)
                        n_drawn[0] += 1
        elif name in ("text",):
            # outlined fonts are paths; leftover <text> is drawn as a stub
            pass
        for child in list(el):
            if _local(child.tag) in ("defs", "clipPath", "title", "desc",
                                     "metadata", "style"):
                continue
            walk(child, st, xf)

    walk(root, {"fill": "#000", "stroke": "none"}, (1, 0, 0, 1, 0, 0))
    if n_drawn[0] < 1:
        return None
    rgb.paste((0, 0, 0), mask=ink.convert("L"))
    return rgb


def _render_fallback(path):
    """cairosvg / svglib / Inkscape / ImageMagick if the host has them."""
    try:
        import cairosvg
        from PIL import Image
        import io
        png = cairosvg.svg2png(url=str(path), dpi=300)
        return Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        pass
    try:
        from svglib.svglib import renderSVG
        from reportlab.graphics import renderPDF
        import pypdfium2 as pdfium
        import io
        from PIL import Image
        drawing = renderSVG(str(path))
        buf = io.BytesIO()
        renderPDF.drawToFile(drawing, buf)
        buf.seek(0)
        doc = pdfium.PdfDocument(buf)
        return doc[0].render(scale=300 / 72).to_pil().convert("RGB")
    except Exception:
        pass
    import shutil
    import subprocess
    import tempfile
    from PIL import Image
    inkscape = shutil.which("inkscape")
    if inkscape:
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                out = Path(fh.name)
            subprocess.run(
                [inkscape, str(path), "--export-type=png",
                 f"--export-filename={out}", "--export-dpi=300"],
                check=True, capture_output=True, timeout=60)
            im = Image.open(out).convert("RGB")
            out.unlink(missing_ok=True)
            return im
        except Exception:
            pass
    magick = shutil.which("magick")
    if magick:
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                out = Path(fh.name)
            subprocess.run(
                [magick, "-density", "300", str(path), str(out)],
                check=True, capture_output=True, timeout=60)
            im = Image.open(out).convert("RGB")
            out.unlink(missing_ok=True)
            return im
        except Exception:
            pass
    return None
