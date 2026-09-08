#!/usr/bin/env python3
"""Read TAB, chord charts, and engraved-TAB PDFs into events.

Does not build MusicXML -- that is tab_write.

SVG pages are rasterised (MuseScore/Inkscape exports) then read like a screenshot.
"""

import itertools
import os
import re
from pathlib import Path

from tab_model import TabError
from svg_page import svg_to_pil

try:
    import numpy as np
except ImportError:
    np = None

# ------------------------------------------------- stage 1b: ASCII TAB input

TAB_LINE_CHARS = set("-|0123456789")

# technique marks and annotations that may sit inside a real TAB line
# (hammer-on, pull-off, bends, slides, vibrato, mutes, ghost notes, ...)
TAB_NEUTRAL_CHARS = set("hpbrsxXvtg~.*()^<>/\\ ")

def clean_pasted_tab(text):
    """Normalize text pasted from tab websites."""
    text = re.sub(r"\[/?(?:tab|ch)\]", "", text, flags=re.IGNORECASE)
    return (text.replace("–", "-").replace("—", "-")
                .replace("\t", " "))

def looks_like_tab_line(line):
    s = line.strip()
    if s.count("-") < 4:
        return False
    core = sum(1 for ch in s if ch in TAB_LINE_CHARS)
    neutral = sum(1 for ch in s if ch in TAB_NEUTRAL_CHARS)
    denom = max(1, len(s) - neutral)
    return core / denom >= 0.7

def trim_trailing_junk(line):
    """Drop annotations after the closing barline ('...| x2', '...| let ring')
    while keeping genuine tab that continues past the last '|'."""
    cut = line.rfind("|")
    if cut == -1:
        return line
    tail = line[cut + 1:]
    if any(c.isdigit() for c in tail) and "-" in tail:
        return line
    return line[:cut + 1]

def parse_ascii_tab(text, instr):
    """Parse ASCII TAB text into [(duration_units, [(string, fret), ...]), ...].

    Rhythm is inferred from horizontal spacing: the smallest gap between
    events is one unit, wider gaps scale up proportionally.
    """
    n = len(instr.tuning)
    lines = [ln.rstrip() for ln in clean_pasted_tab(text).splitlines()]
    systems = []
    shorter = []
    mismatched = []
    run = []
    shift = 0
    for ln in lines + [""]:
        if looks_like_tab_line(ln):
            run.append(ln)
        else:
            if run:
                if len(run) == n:
                    systems.append(run)
                elif 3 <= len(run) < n:
                    shorter.append(run)
                elif 3 <= len(run) <= 8:
                    mismatched.append(len(run))
                run = []
    if not systems and shorter:
        src_n = len(shorter[0])
        if all(len(s) == src_n for s in shorter):
            shift = n - src_n
            systems = shorter
            print(f"  note: {src_n}-string TAB on {n}-string {instr.label} — "
                  "blank strings on top (bass riff on guitar)")
        else:
            raise TabError(f"found TAB with {src_n} lines per system "
                     f"but '{instr.label}' has {n} strings -- pick the right "
                     "instrument (guitar=6, ukulele/bass=4)")
    if not systems:
        if mismatched:
            raise TabError(f"found TAB with {mismatched[0]} lines per system "
                     f"but '{instr.label}' has {n} strings -- pick the right "
                     "instrument (guitar=6, ukulele/bass=4)")
        raise TabError("no TAB lines found. Lines should look like "
                 "e|--0--2--3--| (mostly dashes and numbers).")
    if mismatched:
        print(f"  note: skipped {len(mismatched)} TAB block(s) with a "
              "different string count")

    seq = []        # flat list of (system_break_before, col, [(string, fret)])
    gaps = []
    for sysl in systems:
        sysl = [trim_trailing_junk(l) for l in sysl]
        starts = [ln.find("|") for ln in sysl]
        if all(0 <= s <= 3 for s in starts):
            sysl = [ln[s + 1:] for ln, s in zip(sysl, starts)]
        width = max(len(l) for l in sysl)
        sysl = [l.ljust(width) for l in sysl]
        barcols = {c for c in range(width)
                   if sum(1 for l in sysl if l[c] == "|") >= max(2, n // 2)}
        by_col = {}
        for si, line in enumerate(sysl):
            found = []
            c = 0
            while c < width:
                if line[c].isdigit():
                    j = c
                    while j + 1 < width and line[j + 1].isdigit():
                        j += 1
                    fret = int(line[c:j + 1])
                    before = line[c - 1].lower() if c > 0 else " "
                    after = line[j + 1:j + 5]
                    # a fret in brackets is an optional or ghost note, e.g.
                    # "(4)" for a longer name -- not part of the tune
                    bracketed = before == "(" and after[:1] == ")"
                    if fret <= instr.max_fret and not bracketed:
                        found.append((c, fret, before, after))
                    c = j + 1
                else:
                    c += 1
            # A technique letter sits between the two frets it joins, so it
            # describes what the SECOND note does to the first.
            kept = []
            for col, fret, before, tail in found:
                marks = set()
                low = tail.lower()
                if low.startswith(("(ph)", "(p)")):
                    marks.add("pinch")          # pinched harmonic
                elif low.startswith("(h)"):
                    marks.add("harmonic")       # natural harmonic
                if low.startswith("~"):
                    marks.update(("vibrato", "vibrato_stop"))
                if low[:1] == "b" and not low[1:2].isdigit():
                    marks.add("bend:2")         # "17b": target left unwritten
                if before in "br" and kept:
                    # a bend is not a second pick -- the note already ringing
                    # is pushed up, or let back down again
                    source = kept[-1]
                    if before == "b":
                        source[2].add("bend:%d" % (fret - source[1]))
                    else:
                        rise = [m for m in source[2] if m.startswith("bend:")]
                        step = -int(rise[0].split(":")[1]) if rise else 0
                        source[2].add("release:%d" % step)
                    continue
                if before in "hp/\\" and kept:
                    source = kept[-1]
                    if before == "h":
                        source[2].add("hammer_start")
                        marks.add("hammer_stop")
                    elif before == "p":
                        source[2].add("pull_start")
                        marks.add("pull_stop")
                    else:
                        source[2].add("slide_start")
                        marks.add("slide_stop")
                kept.append([col, fret, marks])
            for col, fret, marks in kept:
                by_col.setdefault(col, []).append([si + 1 + shift, fret, marks])
        cols = sorted(by_col)
        clusters = []
        for c in cols:
            if clusters and c - clusters[-1][-1] <= 1:
                clusters[-1].append(c)
            else:
                clusters.append([c])
        sys_seq = []
        for cl in clusters:
            notes = []
            seen_strings = set()
            for c in cl:
                for entry in by_col[c]:
                    if entry[0] not in seen_strings:
                        seen_strings.add(entry[0])
                        notes.append(entry)
            sys_seq.append((cl[0], notes))
        for (c1, _), (c2, _) in zip(sys_seq, sys_seq[1:]):
            gap = (c2 - c1) - len([b for b in barcols if c1 < b < c2])
            if gap > 0:
                gaps.append(gap)
        for i, (c, notes) in enumerate(sys_seq):
            nxt = sys_seq[i + 1][0] if i + 1 < len(sys_seq) else None
            if nxt is None:
                seq.append((None, notes))
            else:
                seq.append(((nxt - c) - len([b for b in barcols if c < b < nxt]), notes))

    if not seq:
        raise TabError("no notes found in the TAB")
    unit = min(gaps) if gaps else 1
    events = []
    for gap, notes in seq:
        units = 1 if gap is None else max(1, min(16, round(gap / unit)))
        events.append((units, notes))
    return events

# --------------------------------------- stage 1b2: TAB embedded in a PDF

def _pdf_h_runs(page, min_cover):
    """Horizontal strokes (lines or thin rects) merged by y.

    Returns [(y, x_left, x_right)] for candidates whose merged segments
    cover at least min_cover horizontally -- staff lines survive even when
    an engraver breaks them into pieces or draws them as rectangles.
    """
    segs = []
    for l in page.lines:
        if abs(l["top"] - l["bottom"]) < 0.7:
            segs.append(((l["top"] + l["bottom"]) / 2, l["x0"], l["x1"]))
    for r in page.rects:
        if (r["bottom"] - r["top"]) < 1.3 and (r["x1"] - r["x0"]) > 10:
            segs.append(((r["top"] + r["bottom"]) / 2, r["x0"], r["x1"]))
    segs.sort()
    out = []
    i = 0
    while i < len(segs):
        j = i
        ys, xs = [], []
        while j < len(segs) and segs[j][0] - segs[i][0] < 0.8:
            ys.append(segs[j][0])
            xs.append((segs[j][1], segs[j][2]))
            j += 1
        xs.sort()
        cover = 0
        cx0, cx1 = xs[0]
        for a, b in xs[1:]:
            if a > cx1:
                cover += cx1 - cx0
                cx0, cx1 = a, b
            else:
                cx1 = max(cx1, b)
        cover += cx1 - cx0
        if cover >= min_cover:
            out.append((sum(ys) / len(ys), min(x[0] for x in xs),
                        max(x[1] for x in xs)))
        i = j
    return out

def _pdf_verticals(page):
    """Vertical strokes (lines or thin rects): stems and barlines."""
    out = []
    for l in page.lines:
        if abs(l["x0"] - l["x1"]) < 0.7:
            out.append(((l["x0"] + l["x1"]) / 2, l["top"], l["bottom"]))
    for r in page.rects:
        if (r["x1"] - r["x0"]) < 1.3 and (r["bottom"] - r["top"]) > 2:
            out.append(((r["x0"] + r["x1"]) / 2, r["top"], r["bottom"]))
    return out

def _pdf_beam_boxes(page, g):
    """Beam candidates: flat rects, thick strokes, or slanted filled shapes
    (with an aspect guard so slurs don't qualify)."""
    boxes = []
    for r in page.rects:
        h = r["bottom"] - r["top"]
        w = r["x1"] - r["x0"]
        if h < 0.45 * g and w > 0.5 * g:
            boxes.append((r["x0"], r["x1"], (r["top"] + r["bottom"]) / 2))
    for c in getattr(page, "curves", []):
        h = c["bottom"] - c["top"]
        w = c["x1"] - c["x0"]
        if h <= 1.3 * g and w > 0.8 * g and w >= 2.5 * h and c.get("fill"):
            boxes.append((c["x0"], c["x1"], (c["top"] + c["bottom"]) / 2))
    for l in page.lines:
        if (abs(l["top"] - l["bottom"]) < 0.7 and (l["x1"] - l["x0"]) > 0.8 * g
                and l.get("linewidth", 0) >= 1.2):
            boxes.append((l["x0"], l["x1"], (l["top"] + l["bottom"]) / 2))
    return boxes

def _font_family(name):
    """'QLCAAA+Arial-BoldMT' -> 'Arial-BoldMT'.

    PDFs subset the same font several times with different prefixes, so
    comparing raw names would split one typeface into many.
    """
    return (name or "").split("+")[-1]

MUSIC_FONT_HINTS = ("Reprise", "Opus", "Maestro", "Sonata", "Bravura",
                    "MuseJazz", "Petaluma", "Leland", "Emmentaler")

def _pdf_headings(page, s_top, g):
    """Section names printed above a system, e.g. a chorus marker.

    Returns [(x, text)] for wording set in a text (non-music) font just
    above the staff -- bold, or fully capitalised, and at least three
    letters, which keeps tempo marks and the title out of it.
    """
    cands = []
    for c in page.chars:
        ym = (c["top"] + c["bottom"]) / 2
        if not (s_top - 7 * g < ym < s_top - 1.3 * g):
            continue
        fam = _font_family(c["fontname"])
        if any(h in fam for h in MUSIC_FONT_HINTS):
            continue
        cands.append((c, fam))
    words = []
    for c, fam in sorted(cands, key=lambda t: (round(t[0]["top"], 1),
                                               t[0]["x0"])):
        if (words and abs(words[-1][3] - c["top"]) < 2.0
                and c["x0"] - words[-1][1] < 3.5):
            words[-1][0] += c["text"]
            words[-1][1] = c["x1"]
        else:
            words.append([c["text"], c["x1"], c["x0"], c["top"],
                          "Bold" in fam])
    out = []
    for text, _x1, x0, _top, bold in words:
        t = text.strip()
        if sum(1 for ch in t if ch.isalpha()) >= 3 and (bold or t == t.upper()):
            out.append((x0, t))
    return out

def _pdf_diagonals(page):
    """Short slanted strokes: how engravers draw a slide between frets."""
    out = []
    for l in page.lines:
        dx = abs(l["x1"] - l["x0"])
        dy = abs(l["bottom"] - l["top"])
        if dx > 0.7 and dy > 0.7:
            out.append((l["x0"], l["x1"], (l["top"] + l["bottom"]) / 2, dx, dy))
    return out

def _merge_digit_words(words):
    """Rejoin two-digit frets that some PDFs emit as separate characters."""
    words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    out = []
    for w in words:
        if (out and abs(out[-1]["top"] - w["top"]) < 1.0
                and 0 <= w["x0"] - out[-1]["x1"] < 1.2
                and len(out[-1]["text"]) + len(w["text"]) <= 2):
            merged = dict(out[-1])
            merged["text"] += w["text"]
            merged["x1"] = w["x1"]
            out[-1] = merged
        else:
            out.append(dict(w))
    return out

# Grace notes are printed in the same font as real frets, just smaller --
# the "tiny fingering numbers" tucked in front of the note they ornament.
GRACE_SIZE_RATIO = 0.85

class Barline(list):
    """Sentinel event payload meaning "the source draws a barline here".

    It subclasses an empty list so every consumer that just counts notes
    or tests truthiness sees nothing; only events_to_tree looks for it.
    """

BARLINE = (0, Barline())

# Values a bare stem can stand for. With no beam and no flag, this style of
# tab draws a crotchet, a minim and a semibreve identically -- the engraver
# leaves it to the bar to say which is which, so that is how we read them.
BARE_VALUES = (4, 8, 16)

def solve_bare_stems(durs, certain, dotted, widths, cap):
    """Choose a value for each bare stem so the bar adds up exactly.

    Beamed, flagged and dotted readings are taken as given; only the bare
    stems are free. Where several combinations fit, the printed spacing
    decides -- engravers lay notes out roughly in proportion to their length.
    Returns the completed duration list, or None if nothing fits.
    """
    free = [k for k in range(len(durs)) if not certain[k]]
    if not free or len(free) > 6:
        return None
    fixed = sum(durs[k] for k in range(len(durs)) if certain[k])
    room = cap - fixed
    if room <= 0:
        return None
    choices = []
    for k in free:
        vals = [v * 3 // 2 if dotted[k] else v for v in BARE_VALUES]
        vals = [v for v in vals if v <= room]
        if not vals:
            return None
        choices.append(vals)
    total_w = sum(widths) or 1.0
    best = None
    for combo in itertools.product(*choices):
        if sum(combo) != room:
            continue
        trial = list(durs)
        for k, v in zip(free, combo):
            trial[k] = v
        score = sum(abs(trial[i] / cap - widths[i] / total_w)
                    for i in range(len(trial)))
        if best is None or score < best[0]:
            best = (score, trial)
    return best[1] if best else None

def _smufl_cp(text):
    """First character's codepoint if it is in the SMuFL Private Use area."""
    if not text:
        return None
    cp = ord(text[0])
    return cp if 0xE000 <= cp <= 0xF8FF else None


def _is_flag_glyph(text):
    cp = _smufl_cp(text)
    return cp is not None and 0xE240 <= cp <= 0xE25F


def _is_rest_glyph(text):
    cp = _smufl_cp(text)
    return cp is not None and 0xE4E0 <= cp <= 0xE4FF


def _is_wiggle_glyph(text):
    """Guitar vibrato / wiggle segments (SMuFL U+EAA0–U+EABF)."""
    cp = _smufl_cp(text)
    return cp is not None and 0xEAA0 <= cp <= 0xEABF


def _debug_frets(notes):
    """'3', '[32]', '(1)3' -- grace notes shown in brackets. Debug only."""
    txt = ""
    for nn in sorted(notes, key=lambda n: n[0]):
        f = str(nn[1])
        txt += f"({f})" if len(nn) > 2 and "grace" in nn[2] else f
    return txt

def _spacing_units(clusters):
    """Fallback rhythm from horizontal spacing (like ASCII TAB)."""
    gaps = []
    for a, b in zip(clusters, clusters[1:]):
        if a[0] == b[0] and a[1] == b[1]:
            gaps.append(b[2] - a[2])
    unit = min(g for g in gaps if g > 0) if gaps else 10.0
    out = []
    for i, (pno, si, x, notes, _units, _certain, _dot) in enumerate(clusters):
        nxt = clusters[i + 1] if i + 1 < len(clusters) else None
        if nxt and nxt[0] == pno and nxt[1] == si:
            units = max(1, min(16, round((nxt[2] - x) / unit)))
        else:
            units = 1
        out.append((units, notes))
    return out


# ------------------------------------------------- raster TAB (screenshots)

def _ink_holes(ink):
    """(hole_count, centre-y of the first hole as a 0-1 fraction)."""
    h, w = ink.shape
    bg = ~ink
    vis = np.zeros_like(bg, dtype=bool)
    stack = []
    for x in range(w):
        if bg[0, x]:
            stack.append((0, x))
        if bg[h - 1, x]:
            stack.append((h - 1, x))
    for y in range(h):
        if bg[y, 0]:
            stack.append((y, 0))
        if bg[y, w - 1]:
            stack.append((y, w - 1))
    while stack:
        y, x = stack.pop()
        if y < 0 or x < 0 or y >= h or x >= w or vis[y, x] or not bg[y, x]:
            continue
        vis[y, x] = True
        stack.extend(((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)))
    hole = bg & ~vis
    n = 0
    first_cy = 0.5
    seen = np.zeros_like(hole)
    for y in range(h):
        for x in range(w):
            if not hole[y, x] or seen[y, x]:
                continue
            n += 1
            st = [(y, x)]
            sy = 0
            k = 0
            while st:
                yy, xx = st.pop()
                if (yy < 0 or xx < 0 or yy >= h or xx >= w
                        or seen[yy, xx] or not hole[yy, xx]):
                    continue
                seen[yy, xx] = True
                sy += yy
                k += 1
                st.extend(((yy - 1, xx), (yy + 1, xx),
                           (yy, xx - 1), (yy, xx + 1)))
            if n == 1 and k:
                first_cy = sy / k / h
    return n, first_cy


_CANON_H, _CANON_W = 24, 16
_DIGIT_TEMPLATES = None


def _font_paths():
    names = ("times.ttf", "timesbd.ttf", "arial.ttf", "georgia.ttf",
             "calibri.ttf", "segoeui.ttf", "tahoma.ttf", "verdana.ttf")
    out = []
    win = Path(r"C:\Windows\Fonts")
    if win.exists():
        for name in names:
            p = win / name
            if p.exists():
                out.append(p)
    extras = [
        Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/Library/Fonts/Times New Roman.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        Path("/Library/Fonts/Georgia.ttf"),
    ]
    for p in extras:
        if p.exists() and p not in out:
            out.append(p)
    return out


def _norm_digit(ink):
    """Binary crop -> fixed-size mask for template compare."""
    from PIL import Image
    rows = np.any(ink, axis=1)
    cols = np.any(ink, axis=0)
    if not rows.any() or not cols.any():
        return np.zeros((_CANON_H, _CANON_W), dtype=bool)
    ink = ink[rows][:, cols]
    h, w = ink.shape
    im = Image.fromarray(ink.astype(np.uint8) * 255)
    nw = max(1, int(round(w * _CANON_H / h)))
    im = im.resize((nw, _CANON_H), Image.BILINEAR)
    arr = np.asarray(im) > 80
    if nw > _CANON_W:
        arr = np.asarray(
            Image.fromarray(arr.astype(np.uint8) * 255)
            .resize((_CANON_W, _CANON_H), Image.BILINEAR)) > 80
        nw = _CANON_W
    out = np.zeros((_CANON_H, _CANON_W), dtype=bool)
    left = (_CANON_W - nw) // 2
    out[:, left:left + nw] = arr[:, :nw]
    return out


def _digit_templates():
    global _DIGIT_TEMPLATES
    if _DIGIT_TEMPLATES is not None:
        return _DIGIT_TEMPLATES
    from PIL import Image, ImageDraw, ImageFont
    tmpls = []
    for fp in _font_paths():
        try:
            font = ImageFont.truetype(str(fp), 28)
        except OSError:
            continue
        for d in range(10):
            im = Image.new("L", (64, 52), 0)
            ImageDraw.Draw(im).text((6, 0), str(d), fill=255, font=font)
            arr = np.asarray(im) > 128
            if arr.any():
                tmpls.append((d, _norm_digit(arr)))
    _DIGIT_TEMPLATES = tmpls
    return tmpls


def _iou(a, b):
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / union if union else 0.0


def _unstaff(ink):
    """Wipe a thin mid-height bar so a staff line through a 0 is a hole again."""
    h, w = ink.shape
    if h < 8:
        return ink
    mid = h // 2
    t = max(1, h // 12)
    strip = ink[mid - t:mid + t + 1, :]
    above = ink[max(0, mid - t - 3):mid - t, :]
    below = ink[mid + t + 1:min(h, mid + t + 4), :]
    if not strip.size:
        return ink
    sm = float(strip.mean())
    if sm > 0.55 and (not above.size or sm > float(above.mean()) * 1.15) and (
            not below.size or sm > float(below.mean()) * 1.15):
        out = ink.copy()
        out[mid - t:mid + t + 1, :] = False
        return out
    return ink


def _template_scores(ink):
    nrm = _norm_digit(ink)
    scores = {}
    for d, t in _digit_templates():
        s = _iou(nrm, t)
        if s > scores.get(d, 0):
            scores[d] = s
    return scores


def _geometry_digit(ink, h, w, dens, nh, hcy):
    """Fallback when no fonts are installed to build templates."""
    aspect = h / max(w, 1)
    if w <= 6 and aspect >= 2.0:
        left = float(ink[:, : max(1, w // 2)].mean())
        right = float(ink[:, w // 2:].mean())
        if abs(left - right) > 0.22:
            return None, 0.0
        return 1, 0.55
    if nh >= 2:
        return 8, 0.5
    if nh == 1:
        if 0.35 <= hcy <= 0.65:
            return 0, 0.6
        if hcy < 0.35:
            return (4 if dens > 0.55 else 9), 0.5
        return 6, 0.5
    top = float(ink[: max(1, h // 2)].mean())
    bot = float(ink[h // 2:].mean())
    if aspect >= 2.4:
        return 1, 0.5
    if top > bot * 1.45:
        return 7, 0.45
    tl = float(ink[: max(1, h // 3), : max(1, w // 2)].mean())
    bl = float(ink[2 * h // 3:, : max(1, w // 2)].mean())
    if tl > 0.48 and bl < 0.32:
        return 5, 0.45
    if bot > top * 1.3 and tl < 0.35:
        return 2, 0.45
    return 3, 0.4


def _digit_guess(ink):
    """(digit or None, score 0-1). None = not a fret number."""
    rows = np.any(ink, axis=1)
    cols = np.any(ink, axis=0)
    if not rows.any() or not cols.any():
        return None, 0.0
    ink = ink[rows][:, cols]
    h, w = ink.shape
    if h < 5 or w < 3:
        return None, 0.0
    dens = float(ink.mean())
    if w >= 2.2 * h and dens < 0.6:
        return None, 0.0
    if h <= 6 and w > 2 * h:
        return None, 0.0
    if dens < 0.14:
        return None, 0.0
    destaff = _unstaff(ink)
    nh, hcy = _ink_holes(ink)
    nh2, hcy2 = _ink_holes(destaff)
    tmpls = _digit_templates()
    if not tmpls:
        n_use, cy = (nh2, hcy2) if nh2 else (nh, hcy)
        return _geometry_digit(ink, h, w, dens, n_use, cy)
    scores = _template_scores(ink)
    for d, s in _template_scores(destaff).items():
        if s > scores.get(d, 0):
            scores[d] = s
    best_d = max(scores, key=scores.get)
    best_s = scores[best_d]
    aspect = h / max(w, 1)
    # a staff line through a 3 can look slightly more like an 8
    if (best_d == 8 and scores.get(3, 0) >= best_s - 0.06
            and scores.get(3, 0) >= 0.45):
        best_d, best_s = 3, scores[3]
    # serif 1 vs open 4: the real 1 is a thin stroke
    if (best_d == 4 and aspect >= 1.85
            and scores.get(1, 0) >= best_s - 0.08):
        best_d, best_s = 1, scores[1]
    n_use, cy = (nh2, hcy2) if nh2 >= nh else (nh, hcy)
    if (n_use == 1 and 0.34 <= cy <= 0.66 and best_d in (1, 7)
            and best_s < 0.50):
        best_d, best_s = 0, max(best_s, 0.55)
    # split-zero halves are C / reverse-C, not a 1
    if best_d == 1 and 4 <= w <= 0.35 * h + 2:
        left = float(ink[:, : w // 2].mean())
        right = float(ink[:, w // 2:].mean())
        if abs(left - right) > 0.28:
            return None, best_s
    if best_s < 0.30:
        return None, best_s
    return best_d, best_s


def classify_tab_digit(ink):
    """Guess 0-9 from a tight binary crop of one TAB fret number."""
    d, _s = _digit_guess(ink)
    return d


def _staff_ys(dark, min_frac=0.48):
    row = dark.mean(axis=1)
    hits = [i for i, v in enumerate(row) if v >= min_frac]
    ys = []
    i = 0
    while i < len(hits):
        j = i
        while j + 1 < len(hits) and hits[j + 1] <= hits[j] + 1:
            j += 1
        chunk = hits[i:j + 1]
        ys.append(chunk[len(chunk) // 2])
        i = j + 1
    return ys


def _group_staff_systems(ys, n, min_gap=8, dark=None):
    systems = []
    used = set()
    i = 0
    while i + n <= len(ys):
        run = ys[i:i + n]
        gaps = [run[j + 1] - run[j] for j in range(n - 1)]
        g0 = gaps[0]
        if g0 >= min_gap and max(gaps) - min(gaps) <= max(2, 0.28 * g0):
            systems.append(run)
            used.update(range(i, i + n))
            i += n
        else:
            i += 1
    # a faint lowest string drops the 4th line; complete 3-line runs
    # only when a real staff line is sitting there. Inventing one in empty
    # space lands on the rhythm track under the TAB and reads as notes.
    if n == 4:
        i = 0
        while i + 3 <= len(ys):
            if i in used or (i + 1) in used or (i + 2) in used:
                i += 1
                continue
            run = ys[i:i + 3]
            gaps = [run[1] - run[0], run[2] - run[1]]
            g0 = gaps[0]
            if g0 >= min_gap and abs(gaps[0] - gaps[1]) <= max(2, 0.28 * g0):
                y4 = run[2] + g0
                if _staff_line_at(dark, y4, run[2]):
                    systems.append(run + [y4])
                else:
                    systems.append(list(run))
                used.update((i, i + 1, i + 2))
                i += 3
            else:
                i += 1
        systems.sort(key=lambda r: r[0])
    return systems


def _staff_line_at(dark, y, ref_y):
    """True if row y looks like a staff line, using ref_y as the expected ink."""
    if dark is None:
        return True
    h = dark.shape[0]
    if not (0 <= y < h):
        return False
    ink = float(dark[y].mean())
    ref = float(dark[ref_y].mean()) if 0 <= ref_y < h else 0.5
    return ink >= 0.20 and ink >= 0.35 * max(ref, 0.2)


def _punch_staff_lines(dark, ys, g=None):
    """Clear the staff line, then fill columns where a glyph crosses it."""
    cleaned = dark.copy()
    h = dark.shape[0]
    probe = max(6, int(round(0.4 * g))) if g else 6
    for y in ys:
        lo, hi = max(0, y - 1), min(h, y + 2)
        cleaned[lo:hi, :] = False
        above = dark[max(0, y - probe):max(0, y - 1), :]
        below = dark[min(h, y + 2):min(h, y + probe + 1), :]
        if above.size == 0 or below.size == 0:
            continue
        cross = above.any(axis=0) & below.any(axis=0)
        cleaned[lo:hi, cross] = True
    return cleaned


def _barline_xs(dark, run, g):
    y0, y1 = run[0], run[-1]
    band = dark[y0:y1 + 1, :]
    if band.size == 0:
        return []
    col = band.mean(axis=0)
    xs = []
    on = None
    for x, v in enumerate(col):
        if v >= 0.40 and on is None:
            on = x
        elif v < 0.40 and on is not None:
            if 1 <= x - on <= max(6, g // 4):
                xs.append((on + x) // 2)
            on = None
    if on is not None and 1 <= len(col) - on <= max(6, g // 4):
        xs.append((on + len(col)) // 2)
    # a clump of short hits at the far left is the TAB clef, not barlines
    if len(xs) >= 3 and xs[2] - xs[0] < 1.5 * g and xs[0] < 0.12 * len(col):
        k = 0
        while k < len(xs) and xs[k] < xs[0] + 1.5 * g:
            k += 1
        xs = xs[k:]
    return xs


def _blobs_on_row(cleaned, y, g, x_lo, x_hi):
    half = max(4, int(round(0.4 * g)))
    y0, y1 = max(0, y - half), min(cleaned.shape[0], y + half)
    band = cleaned[y0:y1, x_lo:x_hi]
    col = band.any(axis=0)
    runs, on = [], None
    for x, v in enumerate(col):
        if v and on is None:
            on = x
        elif not v and on is not None:
            runs.append((on, x))
            on = None
    if on is not None:
        runs.append((on, band.shape[1]))
    out = []
    min_h = max(5, int(0.22 * g))
    for a, b in runs:
        if b - a < 3:
            continue
        sl = band[:, a:b]
        rs, cs = np.any(sl, axis=1), np.any(sl, axis=0)
        if not rs.any() or not cs.any():
            continue
        sl = sl[rs][:, cs]
        if sl.shape[0] < min_h:
            continue
        if sl.shape[0] <= 6 and sl.shape[1] > 2.5 * sl.shape[0]:
            continue
        out.append((x_lo + a, x_lo + b, sl))
    return out


def _valley_split(x0, sl, typical_w):
    col = sl.mean(axis=0)
    cut = float(col.max()) * 0.22
    parts, on = [], None
    w = sl.shape[1]
    for x, v in enumerate(col):
        if v > cut and on is None:
            on = x
        elif v <= cut and on is not None:
            if x - on >= 4:
                parts.append((x0 + on, x0 + x, sl[:, on:x]))
            on = None
    if on is not None and w - on >= 4:
        parts.append((x0 + on, x0 + w, sl[:, on:]))
    return parts


def _split_wide_blobs(blobs, typical_w, g=20):
    """A run of 3s on one string lands as one fat blob; split on ink valleys.

    Do not split a single 0/4/8 whose hole looks like a valley.
    """
    out = []
    min_wide = max((1.8 * typical_w) if typical_w else 0, 0.55 * g)
    for x0, x1, sl in blobs:
        d, s = _digit_guess(sl)
        if d is not None and s >= 0.40:
            out.append((x0, x1, sl))
            continue
        if sl.shape[1] > min_wide:
            parts = _valley_split(x0, sl, typical_w)
            if len(parts) >= 2 and all(
                    _digit_guess(p[2])[0] is not None for p in parts):
                out.extend(parts)
                continue
        out.append((x0, x1, sl))
    return out


def _local_ink(a, y0, y1, x0, x1):
    h, w = a.shape
    y0, y1 = max(0, int(y0)), min(h, int(y1))
    x0, x1 = max(0, int(x0)), min(w, int(x1))
    if y1 - y0 < 5 or x1 - x0 < 3:
        return None
    band = a[y0:y1, x0:x1]
    med = float(np.median(band))
    return band < (med - 30)


def _merge_broken_glyphs(hits, a, y, g):
    """Join over-segmented halves of one digit (a 0 split into two 1s)."""
    hits = sorted(hits, key=lambda t: t[0])
    out = []
    i = 0
    half = max(4, int(round(0.45 * g)))
    while i < len(hits):
        if i + 1 < len(hits):
            x0 = hits[i][3]
            x1 = hits[i + 1][4]
            gap = hits[i + 1][3] - hits[i][4]
            span = x1 - x0
            if 0 <= gap <= 0.42 * g and span <= 0.85 * g:
                ink = _local_ink(a, y - half, y + half + 1, x0 - 1, x1 + 1)
                if ink is not None:
                    d, s = _digit_guess(ink)
                    parts = max(hits[i][5] or 0.0, hits[i + 1][5] or 0.0)
                    loose = hits[i][1] is None or hits[i + 1][1] is None
                    if (d is not None and s >= 0.32
                            and (d in (0, 4, 6, 8, 9) or loose
                                 or s > parts + 0.06)):
                        out.append(((x0 + x1) / 2, d, x1 - x0, x0, x1, s))
                        i += 2
                        continue
        out.append(hits[i])
        i += 1
    return out


def _merge_multidigit(hits, g=20):
    """Turn neighbouring 1+0 / 1+2 on the same string into fret 10 / 12."""
    hits = sorted(hits, key=lambda t: t[0])
    out = []
    i = 0
    while i < len(hits):
        h = hits[i]
        x, d, w = h[0], h[1], h[2]
        if (d == 1 and i + 1 < len(hits) and hits[i + 1][1] <= 9):
            nxt = hits[i + 1]
            gap = nxt[3] - h[4] if len(h) > 4 else (nxt[0] - x)
            if gap < 0.22 * g and nxt[0] - x < 0.28 * g:
                merged = 10 + nxt[1]
                out.append(((x + nxt[0]) / 2, merged,
                            w + nxt[2], h[3], nxt[4],
                            min(h[5], nxt[5]) if len(h) > 5 else 1.0))
                i += 2
                continue
        out.append(h)
        i += 1
    return out


def _drop_orphan_halves(hits, g):
    """A leftover half of a 0 sits as a 1 hugging the 0 we already kept."""
    hits = sorted(hits, key=lambda t: t[0])
    drop = set()
    for i, h in enumerate(hits):
        if h[1] != 1:
            continue
        for j, o in enumerate(hits):
            if i == j or o[1] not in (0, 6, 8, 9):
                continue
            gap = min(abs(h[3] - o[4]), abs(o[3] - h[4]), abs(h[0] - o[0]))
            if gap < 0.22 * g:
                drop.add(i)
                break
    return [h for i, h in enumerate(hits) if i not in drop]


def _clef_cut_x(per_string, g, width):
    """Right edge of a stacked TAB (or similar) clef at the far left, else 0."""
    left_lim = int(0.16 * width)
    pts = []
    for li, hits in enumerate(per_string):
        for h in hits:
            if h[0] < left_lim:
                pts.append((li, h[0], h[3], h[4], h[1], h[2]))
    if len(pts) < 3:
        return 0
    pts.sort(key=lambda t: t[1])
    clusters = []
    for p in pts:
        if clusters and p[1] - clusters[-1][-1][1] < 0.75 * g:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    for cl in clusters:
        strings = {p[0] for p in cl}
        if len(strings) < 3 or min(p[2] for p in cl) >= 0.12 * width:
            continue
        return max(p[3] for p in cl) + int(0.2 * g)
    return 0


def _stem_xs(dark, y0, y1, g, x_lo, x_hi):
    band = dark[y0:y1, x_lo:x_hi]
    if band.size == 0:
        return []
    h, w = band.shape
    col = band.mean(axis=0)
    stems = []
    x = 0
    max_w = max(5, g // 6)
    while x < w:
        if col[x] > 0.22:
            x0 = x
            while x < w and col[x] > 0.10:
                x += 1
            if 1 <= x - x0 <= max_w:
                sl = band[:, x0:x]
                rs = np.any(sl, axis=1)
                if rs.any():
                    ys = np.where(rs)[0]
                    if ys[-1] - ys[0] >= 0.45 * h:
                        stems.append(x_lo + (x0 + x) // 2)
            continue
        x += 1
    return stems


def _beam_rows(dark, y0, y1, g, x_lo, x_hi):
    band = dark[y0:y1, x_lo:x_hi]
    if band.size == 0:
        return []
    row = band.mean(axis=1)
    rows = []
    on = None
    thick = max(8, int(0.55 * g))
    for i, v in enumerate(row):
        if v >= 0.12 and on is None:
            on = i
        elif v < 0.12 and on is not None:
            if 2 <= i - on <= thick:
                rows.append(y0 + (on + i) // 2)
            on = None
    if on is not None and 2 <= len(row) - on <= thick:
        rows.append(y0 + (on + len(row)) // 2)
    return rows


def _stem_dotted(dark, sx, y0, y1, g, beams):
    h, w = dark.shape
    xa, xb = int(sx + 0.12 * g), int(sx + 0.7 * g)
    xa, xb = max(0, xa), min(w, xb)
    if xb - xa < 3 or y1 <= y0:
        return False
    box = dark[y0:y1, xa:xb].copy()
    for by in beams:
        if y0 <= by < y1:
            lo = max(0, by - y0 - 3)
            hi = min(box.shape[0], by - y0 + 4)
            box[lo:hi, :] = False
    if not box.any():
        return False
    vis = np.zeros_like(box, dtype=bool)
    bh, bw = box.shape
    for yy in range(bh):
        for xx in range(bw):
            if not box[yy, xx] or vis[yy, xx]:
                continue
            stack = [(yy, xx)]
            cells = []
            minx = miny = 10 ** 9
            maxx = maxy = -1
            while stack:
                y, x = stack.pop()
                if (y < 0 or x < 0 or y >= bh or x >= bw
                        or vis[y, x] or not box[y, x]):
                    continue
                vis[y, x] = True
                cells.append((y, x))
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
                stack.extend(((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)))
            ww, hh = maxx - minx + 1, maxy - miny + 1
            if 2 <= ww <= 0.35 * g and 2 <= hh <= 0.35 * g and 6 <= len(cells) <= 80:
                return True
    return False


def _system_rhythm(dark, run, g, x_lo, x_hi):
    """Stems/beams/dots above or below a TAB system -> [(x, units, certain, dotted)]."""
    h, w = dark.shape
    y_top, y_bot = run[0], run[-1]
    bands = [
        ("below", min(h, y_bot + int(0.35 * g)), min(h, y_bot + int(4.2 * g))),
        ("above", max(0, y_top - int(4.2 * g)), max(0, y_top - int(0.25 * g))),
    ]
    best = None
    for side, y0, y1 in bands:
        if y1 - y0 < int(1.2 * g):
            continue
        stems = _stem_xs(dark, y0, y1, g, x_lo, x_hi)
        if best is None or len(stems) > len(best[0]):
            best = (stems, y0, y1, side)
    if best is None or len(best[0]) < 3:
        return []
    stems, y0, y1, _side = best
    beams = _beam_rows(dark, y0, y1, g, x_lo, x_hi)
    out = []
    for sx in stems:
        sx_i = int(sx)
        bc = 0
        for by in beams:
            # the stem itself inks every beam row; look beside it for a
            # horizontal beam, not at the stem column
            left0, left1 = max(0, sx_i - int(0.9 * g)), max(0, sx_i - 2)
            right0, right1 = min(w, sx_i + 3), min(w, sx_i + int(0.9 * g))
            y0b, y1b = max(0, by - 1), min(h, by + 2)
            left = dark[y0b:y1b, left0:left1].mean() if left1 > left0 else 0.0
            right = dark[y0b:y1b, right0:right1].mean() if right1 > right0 else 0.0
            if left > 0.28 or right > 0.28:
                bc += 1
        units = {0: 4, 1: 2}.get(bc, 1)
        certain = bc > 0
        dotted = _stem_dotted(dark, sx, y0, y1, g, beams)
        if dotted and units >= 2:
            units = units * 3 // 2
        out.append((sx, units, certain, dotted))
    return out


def _attach_raster_slurs(dark, run, g, clusters, shift):
    """Hammer / pull / tie from an arc drawn between two notes on one string."""
    by_string = {}
    for x, notes in clusters:
        for nn in notes:
            by_string.setdefault(nn[0], []).append((x, nn))
    h, w = dark.shape
    for s, seq in by_string.items():
        li = s - 1 - shift
        if not (0 <= li < len(run)):
            continue
        y = run[li]
        seq.sort(key=lambda t: t[0])
        for (x1, n1), (x2, n2) in zip(seq, seq[1:]):
            gap = x2 - x1
            if gap > 3.5 * g or gap < 0.25 * g:
                continue
            y0, y1 = y - int(0.7 * g), y - int(0.08 * g)
            y0, y1 = max(0, y0), min(h, max(y0 + 1, y1))
            xa, xb = max(0, int(x1)), min(w, int(x2))
            if xb - xa < 4 or y1 <= y0:
                continue
            dens = float(dark[y0:y1, xa:xb].mean())
            if not (0.03 <= dens <= 0.28):
                continue
            if n1[1] == n2[1]:
                n1[2].add("tie_start")
                n2[2].add("tie_stop")
            elif n2[1] > n1[1]:
                n1[2].add("hammer_start")
                n2[2].add("hammer_stop")
            else:
                n1[2].add("pull_start")
                n2[2].add("pull_stop")


def _bar_from_clusters(seg, cap, x0, x1, g):
    """seg: [(x, notes, units|None, certain, dotted)]."""
    if not seg:
        return [(cap, [])]
    n_stem = sum(1 for s in seg if s[2] is not None)
    if n_stem < max(1, int(0.55 * len(seg))):
        return _bar_events_from_xs([(s[0], s[1]) for s in seg], cap)
    durs = [s[2] if s[2] is not None else 4 for s in seg]
    certain = [bool(s[3]) for s in seg]
    dotted = [bool(s[4]) for s in seg]
    soft = [k for k, c in enumerate(certain) if not c]
    while sum(durs) > cap and soft:
        k = max(soft, key=lambda i: durs[i])
        if durs[k] <= 1:
            break
        durs[k] //= 2
        if durs[k] <= 1:
            soft.remove(k)
    if sum(durs) < cap:
        widths = [(seg[k + 1][0] - seg[k][0]) if k + 1 < len(seg)
                  else max(x1 - seg[k][0], 1.0)
                  for k in range(len(seg))]
        solved = solve_bare_stems(durs, certain, dotted, widths, cap)
        if solved:
            durs = solved
    if sum(durs) > cap:
        while sum(durs) > cap:
            k = max(range(len(durs)), key=lambda i: durs[i])
            if durs[k] <= 1:
                break
            durs[k] //= 2
    events = [(durs[i], seg[i][1]) for i in range(len(seg))]
    missing = cap - sum(durs)
    if missing > 0:
        events.append((missing, []))
    return events


def _bar_events_from_xs(items, cap):
    """items: [(x, notes)]; notes are [[s,f,marks], ...]. Spacing rhythm."""
    if not items:
        return [(cap, [])]
    items = sorted(items, key=lambda t: t[0])
    if len(items) == 1:
        return [(cap, items[0][1])]
    gaps = [items[i + 1][0] - items[i][0] for i in range(len(items) - 1)]
    unit = min(g for g in gaps if g > 0) if any(g > 0 for g in gaps) else 10
    durs = [max(1, min(16, round(g / unit))) for g in gaps] + [1]
    total = sum(durs)
    if total < cap:
        durs[-1] += cap - total
    elif total > cap:
        while sum(durs) > cap:
            k = max(range(len(durs)), key=lambda i: durs[i])
            if durs[k] <= 1:
                break
            durs[k] //= 2
    return [(durs[i], items[i][1]) for i in range(len(items))]


def raster_tab_events(path, instr, beats=4, beat_type=4):
    """Read engraved TAB from a screenshot, scan, or raster PDF page.

    Staff lines and fret numbers are pixels (a phone photo of a TAB, a
    MuseScore window dump). Returns the same triple as pdf_tab_events,
    or None if this image is not TAB.
    """
    if np is None:
        return None
    try:
        from PIL import Image, ImageOps  # noqa: F401
    except ImportError:
        return None
    path = Path(path)
    cap = beats * 16 // beat_type
    n = len(instr.tuning)
    arrays = _raster_pages(path)
    if not arrays:
        return None

    all_events = []
    bar_meta = []
    total_notes = 0
    padded = False
    used_rhythm = False
    for a in arrays:
        events, meta, nnotes, shift, rhy = _raster_tab_page(
            a, n, cap, instr.max_fret)
        if shift:
            padded = True
        if rhy:
            used_rhythm = True
        if nnotes:
            all_events.extend(events)
            bar_meta.extend(meta)
            total_notes += nnotes
    if total_notes < 8:
        return None
    if padded:
        print("  note: 4-string TAB on guitar — empty B and high E")
    how = "printed rhythm" if used_rhythm else "rhythm from spacing"
    print(f"  read {total_notes} TAB notes off the image"
          f" ({len(bar_meta)} bars, {how})")
    return all_events, True, bar_meta


def open_page_image(path):
    """Load a page (PNG/JPG/TIFF/BMP/SVG) as a PIL image, upright."""
    from PIL import Image, ImageOps
    path = Path(path)
    if path.suffix.lower() == ".svg":
        return svg_to_pil(path)
    return ImageOps.exif_transpose(Image.open(path))


def _raster_pages(path):
    from PIL import Image, ImageOps
    ext = path.suffix.lower()
    if ext == ".svg":
        return [_scale_gray(svg_to_pil(path).convert("L"))]
    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
        im = ImageOps.exif_transpose(Image.open(path)).convert("L")
        return [_scale_gray(im)]
    if ext == ".pdf":
        try:
            import pypdfium2 as pdfium
        except ImportError:
            return []
        try:
            doc = pdfium.PdfDocument(str(path))
            out = []
            for page in doc:
                pil = page.render(scale=2.0).to_pil().convert("L")
                out.append(_scale_gray(pil))
            return out
        except Exception:
            return []
    return []


def _scale_gray(im):
    from PIL import Image
    w, h = im.size
    # digits on a typical screenshot are ~5 px; 3x makes them classifiable
    scale = 3 if max(w, h) < 1600 else (2 if max(w, h) < 2400 else 1)
    if scale > 1:
        im = im.resize((w * scale, h * scale), Image.BICUBIC)
    return np.asarray(im)


def _raster_tab_page(a, n, cap, max_fret):
    med = float(np.median(a))
    dark = a < (med - 70)
    ys = _staff_ys(dark)
    sys4 = _group_staff_systems(ys, 4, dark=dark)
    sys6 = _group_staff_systems(ys, 6, dark=dark)
    staff_n, shift = n, 0
    if n == 6 and len(sys4) >= 1 and len(sys4) > len(sys6):
        staff_n, shift = 4, 2
    elif n == 4 and len(sys6) >= 1 and len(sys6) > len(sys4):
        raise TabError(
            "this looks like 6-string guitar TAB. "
            "Pick Guitar at the top and try again.")
    systems = _group_staff_systems(ys, staff_n, dark=dark)
    if not systems:
        return [], [], 0, shift, False
    events = []
    meta = []
    notes_n = 0
    used_rhythm = False
    width = a.shape[1]
    debug = os.environ.get("TABIFIER_DEBUG_RASTER")
    for si, run in enumerate(systems):
        g = int(np.median(np.diff(run))) if staff_n > 1 else 20
        cleaned = _punch_staff_lines(dark, run, g)
        bars = _barline_xs(dark, run, g)
        x_left = int(0.02 * width)
        per_string = []
        for li, y in enumerate(run):
            blobs = _blobs_on_row(cleaned, y, g, x_left, width)
            sized = sorted(b - a0 for a0, b, _s in blobs
                           if 0.2 * g < (b - a0) < 0.7 * g)
            typical = (sized[len(sized) // 2] if sized else 12)
            blobs = _split_wide_blobs(blobs, typical, g)
            hits = []
            half = max(4, int(round(0.45 * g)))
            for a0, a1, sl in blobs:
                cx = (a0 + a1) / 2
                if any(abs(cx - bx) <= max(3, g / 6) for bx in bars):
                    continue
                d, s = _digit_guess(sl)
                if d is None:
                    ink = _local_ink(a, y - half, y + half + 1, a0, a1)
                    if ink is not None:
                        d, s = _digit_guess(ink)
                hits.append((cx, d, a1 - a0, a0, a1, s))
            hits = _merge_broken_glyphs(hits, a, y, g)
            hits = [h for h in hits if h[1] is not None]
            hits = _merge_multidigit(hits, g)
            hits = _drop_orphan_halves(hits, g)
            per_string.append(hits)
        cut = _clef_cut_x(per_string, g, width)
        if cut:
            per_string = [[h for h in hits if h[0] >= cut]
                          for hits in per_string]
        x_lo = max(cut, x_left)
        raw = []
        for li, hits in enumerate(per_string):
            for x, fret, _w, _x0, _x1, _s in hits:
                if 0 <= fret <= max_fret:
                    raw.append((x, li + 1 + shift, fret))
        if not raw:
            continue
        raw.sort()
        clusters = []
        for x, s, f in raw:
            if clusters and abs(x - clusters[-1][0]) <= max(6, g * 0.35):
                if all(s != nn[0] for nn in clusters[-1][1]):
                    clusters[-1][1].append([s, f, set()])
            else:
                clusters.append([x, [[s, f, set()]]])
        kept = []
        extra_bars = []
        for x, notes in clusters:
            frets = {nn[1] for nn in notes}
            if len(notes) >= 3 and frets <= {1, 8}:
                extra_bars.append(int(x))
            else:
                kept.append([x, notes])
        clusters = kept
        if extra_bars:
            bars = sorted(set(bars) | set(extra_bars))
        _attach_raster_slurs(dark, run, g, clusters, shift)
        rhythm = _system_rhythm(dark, run, g, x_lo, width)
        placed = []
        for x, notes in clusters:
            stem = min(rhythm, key=lambda st: abs(st[0] - x), default=None)
            if stem is not None and abs(stem[0] - x) <= 0.55 * g:
                placed.append((x, notes, stem[1], stem[2], stem[3]))
            else:
                placed.append((x, notes, None, False, False))
        if (rhythm and placed
                and sum(1 for p in placed if p[2] is not None)
                >= 0.5 * len(placed)):
            used_rhythm = True
        edges = [x_lo] + bars + [width]
        spans = [(edges[k], edges[k + 1])
                 for k in range(len(edges) - 1)
                 if edges[k + 1] - edges[k] >= 3 * g]
        if not spans:
            spans = [(x_lo, width)]
        if len(spans) > 1:
            widths = sorted(b - aa for aa, b in spans[1:])
            typical_span = widths[len(widths) // 2]
            aa, bb = spans[0]
            if ((bb - aa) < 0.5 * typical_span
                    and not any(aa <= p[0] < bb for p in placed)):
                spans.pop(0)
        for mi, (s0, s1) in enumerate(spans):
            seg = [p for p in placed if s0 <= p[0] < s1]
            bar = _bar_from_clusters(seg, cap, s0, s1, g)
            notes_n += sum(len(nn) for _u, nn in bar)
            events.extend(bar)
            events.append(BARLINE)
            meta.append({"new_system": si > 0 and mi == 0})
            if debug:
                frets = " ".join(
                    "".join(str(n[1]) for n in p[1]) for p in seg)
                print(f"   sys{si} bar{mi}: {frets}")
    return events, meta, notes_n, shift, used_rhythm


def engraved_tab_pages(paths, instr, beats=4, beat_type=4):
    """Read one or more TAB PDFs/images. None if none of them is TAB."""
    events, metas = [], []
    absolute = True
    total = 0
    for i, path in enumerate(paths):
        path = Path(path)
        result = None
        if path.suffix.lower() == ".pdf":
            result = pdf_tab_events(path, instr, beats, beat_type)
        if result is None:
            result = raster_tab_events(path, instr, beats, beat_type)
        if result is None:
            if i and total:
                raise TabError(f"could not read TAB from {path.name}")
            return None
        ev, abs_i, meta = result
        absolute = absolute and bool(abs_i)
        nnotes = sum(len(nn) for _u, nn in ev if not isinstance(nn, Barline))
        total += nnotes
        if i and meta:
            meta[0]["new_system"] = True
        events.extend(ev)
        if meta:
            metas.extend(meta)
    if total < 8:
        return None
    return events, absolute, metas or None


def pdf_tab_events(path, instr, beats=4, beat_type=4):
    """Read an engraved TAB out of a PDF: notes, strings, AND rhythm.

    Works on PDFs where staff lines are vector graphics and fret numbers
    are text (Guitar Pro / Songsterr / MuseScore exports). Geometry does
    the work: runs of `n` equally spaced long horizontal lines are TAB
    staves; each digit (in the dominant text font -- filters out time
    signature digits from the music font) maps to its nearest line for the
    string. Rhythm comes from the printed stems and beams above the staff
    (no beam = quarter, one = eighth, two = sixteenth); barlines cut
    measures, and any measure that comes up short gets a rest where the
    empty space is. Returns (events, absolute) where events are
    (units, [(string, fret), ...]) with units in sixteenths when absolute,
    and rests as empty note lists -- or None if the PDF has no readable TAB.
    """
    try:
        import pdfplumber
    except ImportError:
        return None
    from collections import Counter
    n = len(instr.tuning)
    cap = beats * 16 // beat_type
    clusters = []        # (page, system, x, [(string, fret)], stem_units)
    stems_matched = 0
    measures = []        # (seg_start, seg_end, [clusters], is_first, gap)
    sys_notes = []       # every system's clusters, in reading order
    slide_out = []       # (system, string, note) - slide leaving a system
    slide_in = []        # (system, string, note) - slide arriving at a system
    try:
        with pdfplumber.open(str(path)) as pdf:
            for pno, page in enumerate(pdf.pages):
                hruns = sorted(_pdf_h_runs(page, page.width * 0.3))
                systems = []
                i = 0
                while i + n <= len(hruns):
                    run = hruns[i:i + n]
                    g = [run[j + 1][0] - run[j][0] for j in range(n - 1)]
                    if max(g) - min(g) < 1.5 and 4 < g[0] < 22:
                        systems.append(([r[0] for r in run], g[0],
                                        min(r[1] for r in run),
                                        max(r[2] for r in run)))
                        i += n
                    else:
                        i += 1
                if not systems:
                    continue

                raw = [w for w in page.extract_words(
                           keep_blank_chars=False,
                           extra_attrs=["fontname", "size"])
                       if w["text"].isdigit()]
                digit_words = [w for w in _merge_digit_words(raw)
                               if int(w["text"]) <= instr.max_fret]
                cands = []
                for w in digit_words:
                    yc = (w["top"] + w["bottom"]) / 2
                    best = None
                    for si, (run, g, _l, _r) in enumerate(systems):
                        for li, ly in enumerate(run):
                            d = abs(yc - ly)
                            if best is None or d < best[0]:
                                best = (d, si, li, g)
                    if best and best[0] < best[3] * 0.45:
                        cands.append((w, best[1], best[2]))
                if not cands:
                    continue
                # fret numbers share one text font; time signatures use the
                # music font -- keep only the dominant font family's digits
                # (family, not raw name: PDFs subset one font many times)
                main_font = Counter(_font_family(w["fontname"])
                                    for w, _s, _l in cands).most_common(1)[0][0]
                cands = [c for c in cands
                         if _font_family(c[0]["fontname"]) == main_font]
                # Real frets are all printed at one size; a grace note is the
                # same font a couple of points smaller. Counting those little
                # numbers as notes hands them a beat they never had and pushes
                # every bar after them out of shape, so tag them here.
                main_size = Counter(round(w["size"], 1)
                                    for w, _s, _l in cands).most_common(1)[0][0]
                cands = [(w, s, l, w["size"] < main_size * GRACE_SIZE_RATIO)
                         for w, s, l in cands]

                verts = _pdf_verticals(page)
                sys_mids = [(run[0] + run[-1]) / 2
                            for run, _g, _l, _r in systems]

                def nearest_system(yc):
                    return min(range(len(sys_mids)),
                               key=lambda k: abs(yc - sys_mids[k]))

                for si, (run, g, s_left, s_right) in enumerate(systems):
                    s_top, s_bot = run[0], run[-1]
                    staff_h = (n - 1) * g
                    band_lo, band_hi = s_top - 8 * g, s_bot + 8 * g
                    sys_words = sorted((c for c in cands if c[1] == si),
                                       key=lambda c: c[0]["x0"])
                    if not sys_words:
                        continue
                    # stems may sit above, below, or through the TAB staff;
                    # barlines span it fully
                    stems, bars = [], []
                    for xc, vt, vb in verts:
                        yc = (vt + vb) / 2
                        if not (band_lo <= yc <= band_hi):
                            continue
                        if nearest_system(yc) != si:
                            continue
                        h = vb - vt
                        if (h >= staff_h - 2 and vt <= s_top + 2
                                and vb >= s_bot - 2):
                            bars.append(xc)
                        elif 0.8 * g < h < 7 * g:
                            stems.append((xc, vt, vb))
                    beams = [b for b in _pdf_beam_boxes(page, g)
                             if band_lo <= b[2] <= band_hi
                             and nearest_system(b[2]) == si]
                    # decoration glyphs (music font): ties, slurs, vibrato,
                    # flags all live here
                    deco = [c for c in page.chars
                            if _font_family(c["fontname"]) != main_font
                            and not c["text"].isdigit()
                            and band_lo <= (c["top"] + c["bottom"]) / 2 <= band_hi
                            and nearest_system(
                                (c["top"] + c["bottom"]) / 2) == si]

                    # Augmentation dots. The note values are printed as bare
                    # stems and beams in a strip just above the staff, and a
                    # dotted one carries a small mark a little to the RIGHT of
                    # its stem. That offset is what separates a dot from a
                    # flag, which is drawn hard against its own stem; the dots
                    # of a repeat sign are ruled out because they sit inside
                    # the staff rather than above it. Missing these is why
                    # dotted bars used to come up a sixteenth short.
                    dot_stems = []
                    for c in deco:
                        if not (s_top - 2 * g <= c["top"] < s_top):
                            continue
                        if (c["x1"] - c["x0"]) >= 0.8 * g:
                            continue
                        offs = [c["x0"] - sx for sx, _t, _b in stems]
                        if any(abs(v) <= 0.3 * g for v in offs):
                            continue          # sitting on a stem: it's a flag
                        owner = [v for v in offs if 0.3 * g < v < 1.5 * g]
                        if owner:
                            dot_stems.append(c["x0"] - min(owner))

                    # slur/tie arcs: thin curves drawn beside a string line.
                    # engravers emit them twice (stroke + fill), so dedupe.
                    seen_arcs = set()
                    arcs = []
                    for c in getattr(page, "curves", []):
                        ym = (c["top"] + c["bottom"]) / 2
                        if not (band_lo <= ym <= band_hi):
                            continue
                        if nearest_system(ym) != si:
                            continue
                        if (c["bottom"] - c["top"]) > 1.8 * g:
                            continue
                        if not (1.5 < (c["x1"] - c["x0"]) < 10 * g):
                            continue
                        key = (round(c["x0"], 1), round(c["x1"], 1), round(ym, 1))
                        if key in seen_arcs:
                            continue
                        seen_arcs.add(key)
                        arcs.append(c)

                    # clusters keep their own x while the geometry below reads
                    # them, grace notes included -- an arc or slide drawn from
                    # a grace note has to find it where it is printed
                    sys_clusters = []
                    for w, _si, li, is_grace in sys_words:
                        x = w["x0"]
                        fret = int(w["text"])
                        if (sys_clusters and x - sys_clusters[-1][0] <= 3.0
                                and sys_clusters[-1][2] == is_grace):
                            if all(li + 1 != s
                                   for s, _f, _m in sys_clusters[-1][1]):
                                sys_clusters[-1][1].append([li + 1, fret, set()])
                        else:
                            sys_clusters.append([x, [[li + 1, fret, set()]],
                                                 is_grace])
                    sys_notes.append(sys_clusters)
                    sys_index = len(sys_notes) - 1

                    # Each arc physically joins two notes on one string: find
                    # the note it leaves and the note it lands on. Same fret
                    # = tie (hold), rising = hammer-on, falling = pull-off.
                    def note_on_string(li, x, forward):
                        best = None
                        for cx, notes, _gr in sys_clusters:
                            hit = next((nn for nn in notes if nn[0] == li + 1),
                                       None)
                            if hit is None:
                                continue
                            if forward and cx >= x and (best is None
                                                        or cx < best[0]):
                                best = (cx, hit)
                            if not forward and cx <= x and (best is None
                                                            or cx > best[0]):
                                best = (cx, hit)
                        return best

                    # a slide is a short slanted stroke drawn ON the string
                    # between two frets; which way it slides follows from
                    # the fret numbers themselves
                    for dx0, dx1, dym, ddx, ddy in _pdf_diagonals(page):
                        if not (0.8 < ddx < 2.5 * g and 0.8 < ddy < 2.0 * g):
                            continue
                        if nearest_system(dym) != si:
                            continue
                        li = min(range(n), key=lambda k: abs(dym - run[k]))
                        if abs(dym - run[li]) > 0.5 * g:
                            continue
                        left = note_on_string(li, dx0 + 1.0, False)
                        right = note_on_string(li, dx1 - 1.0, True)
                        if left is not None and right is not None:
                            if right[0] <= left[0] or right[0] - left[0] > 60:
                                continue
                            left[1][2].add("slide_start")
                            right[1][2].add("slide_stop")
                        elif left is not None and dx1 > s_right - 4 * g:
                            # A slide into the next system is drawn as a tail
                            # running off the end of this one -- there is no
                            # note to its right to land on here. Remember it
                            # and pair it up once every system has been read.
                            slide_out.append((sys_index, li, left[1]))
                        elif right is not None and dx0 < s_left + 4 * g:
                            slide_in.append((sys_index, li, right[1]))

                    for c in arcs:
                        ym = (c["top"] + c["bottom"]) / 2
                        li = min(range(n), key=lambda k: abs(ym - run[k]))
                        if abs(ym - run[li]) > 0.9 * g:
                            continue
                        left = note_on_string(li, c["x0"] + 2.5, False)
                        right = note_on_string(li, c["x1"] - 4.0, True)
                        if left is None or right is None:
                            continue
                        if right[0] <= left[0] or right[0] - left[0] > 60:
                            continue
                        na, nb = left[1], right[1]
                        if na[1] == nb[1]:
                            na[2].add("tie_start")
                            nb[2].add("tie_stop")
                        elif nb[1] > na[1]:
                            na[2].add("hammer_start")
                            nb[2].add("hammer_stop")
                        else:
                            na[2].add("pull_start")
                            nb[2].add("pull_stop")

                    rest_xs = [(c["x0"] + c["x1"]) / 2 for c in deco
                               if _is_rest_glyph(c["text"])]

                    # vibrato: a run of wiggle glyphs under the staff (they
                    # sit well below it, past the fret numbers' descenders).
                    # Time signatures, rests and the TAB clef also live in
                    # this band, so only SMuFL wiggle/vibrato codepoints
                    # count -- treating a '4' time-sig as a squiggle used
                    # to hang vibrato on beat 1 of every MuseScore TAB.
                    below = sorted((c for c in deco
                                    if _is_wiggle_glyph(c["text"])
                                    and s_bot + 0.3 * g
                                    < (c["top"] + c["bottom"]) / 2
                                    <= s_bot + 5 * g),
                                   key=lambda c: c["x0"])
                    wig_runs = []
                    for c in below:
                        if (wig_runs
                                and c["x0"] - wig_runs[-1][1] < 0.8 * g):
                            wig_runs[-1][1] = max(wig_runs[-1][1], c["x1"])
                        else:
                            wig_runs.append([c["x0"], c["x1"]])
                    for wx0, wx1 in wig_runs:
                        if wx1 - wx0 < 1.2 * g:
                            continue
                        seq = sorted(((x, notes) for x, notes, is_grace
                                      in sys_clusters if not is_grace),
                                     key=lambda t: t[0])
                        # The squiggle starts under the first note it shakes,
                        # give or take the engraver nudging it clear of the
                        # fret number -- so take the FIRST note from there,
                        # not the last one within reach.
                        i0 = next((i for i, (x, _n) in enumerate(seq)
                                   if x >= wx0 - 0.5 * g), None)
                        if i0 is None:
                            continue
                        start_note = max(seq[i0][1], key=lambda nn: nn[0])
                        start_note[2].add("vibrato")
                        # last note the squiggle is drawn over
                        i_last = i0
                        for i in range(i0, len(seq)):
                            if seq[i][0] <= wx1:
                                i_last = i
                            else:
                                break
                        # A note tied onward is still sounding, so the shake
                        # carries on through the tie even though the squiggle
                        # stops being drawn.
                        while i_last + 1 < len(seq):
                            here = next((n for n in seq[i_last][1]
                                         if n[0] == start_note[0]), None)
                            if here is None or "tie_start" not in here[2]:
                                break
                            i_last += 1
                        # The line is drawn through the whole of the note
                        # it ends on, so the last shaken note IS the anchor --
                        # reaching on to the next attack overshoots by a note.
                        i_stop = i_last
                        # stay on the string it started on where we can, so
                        # the mark spans one line of the tab
                        stop_note = next((nn for nn in seq[i_stop][1]
                                          if nn[0] == start_note[0]), None)
                        if stop_note is None:
                            stop_note = max(seq[i_stop][1],
                                            key=lambda nn: nn[0])
                        stop_note[2].add("vibrato_stop")
                        if os.environ.get("TABIFIER_DEBUG_VIB"):
                            print("   vib run %.1f-%.1f start@%.1f "
                                  "last@%.1f stop@%.1f"
                                  % (wx0, wx1, seq[i0][0], seq[i_last][0],
                                     seq[i_stop][0]))

                    # The geometry is read, so the grace notes can now fold
                    # into the note each one ornaments. They keep whatever
                    # hammer-on or slide the engraver drew from them, but they
                    # carry no time of their own -- the bar's beats belong to
                    # the real notes.
                    timed = []
                    pending = []
                    for x, notes, is_grace in sys_clusters:
                        if is_grace:
                            for nn in notes:
                                nn[2].add("grace")
                            pending.extend(notes)
                        else:
                            timed.append((x, pending + notes))
                            pending = []
                    if pending and timed:
                        timed[-1][1].extend(pending)   # trailing grace, no host

                    # A grace note prints its own small stem and flag. Drop
                    # those stems so the real note beside one cannot adopt it
                    # and come out the wrong length.
                    if any(gr for _x, _n, gr in sys_clusters):
                        stems = [st for st in stems
                                 if not min(sys_clusters,
                                            key=lambda c: abs(c[0] - st[0]))[2]]

                    placed = []
                    s_mid = (s_top + s_bot) / 2
                    for x, notes in timed:
                        stem = min(stems, key=lambda st: abs(st[0] - x),
                                   default=None)
                        units = None
                        certain = False
                        dotted = False
                        if stem is not None and abs(stem[0] - x) <= 4.5:
                            stems_matched += 1
                            bc = sum(1 for b in beams
                                     if b[0] - 1 <= stem[0] <= b[1] + 1)
                            units = {0: 4, 1: 2}.get(bc, 1)
                            certain = bc > 0
                            if units == 4:
                                # A flag glyph at the stem's far end makes it
                                # an eighth even without a beam. MuseScore
                                # parks the flag a few spatium below the stem
                                # tip, so SMuFL flag codepoints matching the
                                # stem's x are enough; the old y-near-tip
                                # test remains for engravers that draw flags
                                # as anonymous music-font marks.
                                far_y = (stem[1]
                                         if s_mid - stem[1] > stem[2] - s_mid
                                         else stem[2])
                                flagged = False
                                for c in deco:
                                    cx = (c["x0"] + c["x1"]) / 2
                                    if (min(abs(c["x0"] - stem[0]),
                                            abs(cx - stem[0])) > 0.5 * g):
                                        continue
                                    if _is_flag_glyph(c["text"]):
                                        flagged = True
                                        break
                                    if (abs((c["top"] + c["bottom"]) / 2
                                            - far_y) < 1.3 * g
                                            and not _is_rest_glyph(c["text"])):
                                        flagged = True
                                        break
                                if flagged:
                                    units = 2
                                    certain = True
                            if units >= 2 and any(abs(sx - stem[0]) < 0.5
                                                  for sx in dot_stems):
                                units = units * 3 // 2      # dotted: half again
                                dotted = True
                        entry = (pno, si, x, notes, units, certain, dotted)
                        clusters.append(entry)
                        placed.append(entry)

                    # Double bars and repeat signs are drawn as two lines
                    # side by side, so group them into one barline; the
                    # repeat dots sit beside the group -- to its right to
                    # open a repeat, to its left to close one.
                    groups = []
                    for b in sorted(bars):
                        if groups and b - groups[-1][-1] < 1.2 * g:
                            groups[-1].append(b)
                        else:
                            groups.append([b])
                    in_staff = [c for c in deco
                                if s_top - 1 < (c["top"] + c["bottom"]) / 2
                                < s_bot + 1]
                    kinds = {}
                    for grp in groups:
                        dots_l = sum(1 for c in in_staff
                                     if 0 < grp[0] - (c["x0"] + c["x1"]) / 2
                                     < 9)
                        dots_r = sum(1 for c in in_staff
                                     if 0 < (c["x0"] + c["x1"]) / 2 - grp[-1]
                                     < 9)
                        if dots_r >= 2:
                            kinds[grp[0]] = "repeat_start"
                        elif dots_l >= 2:
                            kinds[grp[0]] = "repeat_end"
                        elif len(grp) > 1:
                            kinds[grp[0]] = "double"

                    def kind_near(x, _k=kinds):
                        for kx, kk in _k.items():
                            if abs(kx - x) < 3.5:
                                return kk
                        return None

                    labels = _pdf_headings(page, s_top, g)
                    inner = [grp[0] for grp in groups
                             if s_left + 2 < grp[0] < s_right - 2]
                    edges = [s_left] + inner + [s_right]
                    spans = [(edges[k], edges[k + 1])
                             for k in range(len(edges) - 1)
                             if edges[k + 1] - edges[k] >= 3 * g]
                    # A system opening on a repeat sign leaves a sliver between
                    # the staff's edge and that sign, holding nothing but the
                    # TAB clef. It is a gutter, not a bar -- and an empty bar
                    # here is precisely a phantom that shifts every bar number
                    # after it.
                    if len(spans) > 1:
                        widths = sorted(b - a for a, b in spans[1:])
                        typical = widths[len(widths) // 2]
                        a, b = spans[0]
                        if ((b - a) < 0.5 * typical
                                and not any(a <= c[2] < b for c in placed)):
                            spans.pop(0)
                    for mi, (a, b) in enumerate(spans):
                        seg = [c for c in placed if a <= c[2] < b]
                        meta = {
                            "new_system": mi == 0 and len(measures) > 0,
                            "heading": next((t for hx, t in labels
                                             if a <= hx < b), None),
                            "start_repeat": kind_near(a) == "repeat_start",
                            "end": {"repeat_end": "repeat",
                                    "double": "double"}.get(kind_near(b)),
                            "rests": [x for x in rest_xs if a <= x < b],
                        }
                        measures.append((a, b, seg, mi == 0, g, meta))
    except Exception as e:
        print(f"  note: could not read the PDF's TAB text ({e})")
        return None

    # Join up the slides that crossed a system break. The note objects are
    # shared with the clusters already collected, so marking them here reaches
    # the events built below.
    def on_string(sc, li, last):
        for _x, notes, _gr in (reversed(sc) if last else sc):
            for nn in notes:
                if nn[0] == li + 1:
                    return nn
        return None

    for k, li, note in slide_out:
        target = (on_string(sys_notes[k + 1], li, False)
                  if k + 1 < len(sys_notes) else None)
        if target is not None and target is not note:
            note[2].add("slide_start")
            target[2].add("slide_stop")

    for k, li, note in slide_in:
        source = on_string(sys_notes[k - 1], li, True) if k else None
        if source is not None and source is not note:
            source[2].add("slide_start")
            note[2].add("slide_stop")

    total_notes = sum(len(c[3]) for c in clusters)
    if total_notes < 8:
        return None

    # build events; track how confident the rhythm reading really is
    events = []
    fill_units = 0
    note_units = 0
    overfull = 0
    debug = os.environ.get("TABIFIER_DEBUG_PDF")
    bar_meta = []
    for mno, (seg_start, seg_end, seg, is_first, g, meta) in enumerate(
            measures, 1):
        bar_meta.append(meta)
        bar = []
        durs = [c[4] if c[4] is not None else 4 for c in seg]
        if seg:
            # An unbeamed, unflagged note was assumed to be a quarter; that is
            # the one guess that can push a bar over its limit (a missed flag
            # turns an eighth into a quarter). Halve those, longest first,
            # until the bar fits -- beamed notes are never touched.
            soft = [k for k, c in enumerate(seg) if not c[5]]
            while sum(durs) > cap and soft:
                k = max(soft, key=lambda i: durs[i])
                if durs[k] <= 1:
                    break
                durs[k] //= 2
                if durs[k] <= 1:
                    soft.remove(k)
            if sum(durs) > cap:
                overfull += 1
                # Still too long: something was misread. Squeeze the longest
                # notes here, inside this bar, so the overflow cannot run on
                # into the next one and drag the rest of the piece with it.
                while sum(durs) > cap:
                    k = max(range(len(durs)), key=lambda i: durs[i])
                    if durs[k] <= 1:
                        break
                    durs[k] //= 2
            elif sum(durs) < cap:
                # The bar is short because bare stems were all read as
                # crotchets. Let them take the values that fill the bar --
                # if none do, the shortfall is real silence and the rest
                # placement below handles it.
                printed_rest = bool(meta.get("rests"))
                # A single unbeamed note in the LEFT half of the bar, with
                # empty space after it, is an attack plus a rest. Stretching
                # it to a whole note scores perfectly in solve_bare_stems
                # (one note always fills 100% of the width) and is always
                # wrong when the engraver printed a rest.
                trail_empty = (len(seg) == 1 and not seg[0][5]
                               and (seg_end - seg[0][2])
                               > 0.45 * max(seg_end - seg_start, 1))
                if not printed_rest and not trail_empty:
                    widths = [(seg[k + 1][2] - seg[k][2]) if k + 1 < len(seg)
                              else max(seg_end - seg[k][2], 1.0)
                              for k in range(len(seg))]
                    solved = solve_bare_stems(durs, [c[5] for c in seg],
                                              [c[6] for c in seg], widths, cap)
                    if solved:
                        durs = solved
        if debug:
            print("   bar %2d: %2d notes  units %-28s %2d/%d  %s"
                  % (mno, len(seg), str(durs), sum(durs), cap,
                     " ".join(_debug_frets(c[3]) for c in seg)))
        if not seg:
            bar.append((cap, []))        # genuinely empty bar
        else:
            content = sum(durs)
            note_units += content
            missing = cap - content
            if missing > 0:
                start = seg_start + (4 * g if is_first else 0)
                lead = seg[0][2] - start
                trail = seg_end - (seg[-1][2] + 8)
                # a note tied (or vibrato'd) into the next one is SUSTAINING
                # across the gap after it -- that space is held sound, never a
                # rest, so the held note absorbs the missing time instead
                held = [any("tie_start" in nn[2] or "vibrato" in nn[2]
                            for nn in c[3] if len(nn) > 2)
                        for c in seg]
                internal = [(seg[k + 1][2] - seg[k][2], k)
                            for k in range(len(seg) - 1) if not held[k]]
                widest = max(internal, default=(0, -1))
                rest_at = 0
                if widest[0] > lead and widest[0] > trail:
                    rest_at = widest[1] + 1
                elif trail > lead * 1.5 and not held[-1]:
                    rest_at = len(seg)
                held_gaps = [(seg[k + 1][2] - seg[k][2], k)
                             for k in range(len(seg) - 1) if held[k]]
                if held[-1]:
                    # a bar ending on a note that rings (tied or with vibrato)
                    # is sustaining into the space after it, not resting --
                    # without this the note gets shoved to the end of the bar
                    # behind rests it never had
                    held_gaps.append((trail, len(seg) - 1))
                biggest_held = max(held_gaps, default=(0, -1))
                if (biggest_held[1] >= 0
                        and biggest_held[0] >= max(widest[0], lead, trail)):
                    # sustain dominates: lengthen the held note, no rest at all
                    durs[biggest_held[1]] += missing
                    note_units += missing
                    for k, c in enumerate(seg):
                        bar.append((durs[k], c[3]))
                else:
                    for k, c in enumerate(seg):
                        if k == rest_at:
                            bar.append((missing, []))
                        bar.append((durs[k], c[3]))
                    if rest_at == len(seg):
                        bar.append((missing, []))
                    fill_units += missing
            else:
                for k, c in enumerate(seg):
                    bar.append((durs[k], c[3]))
        events.extend(bar)
        events.append(BARLINE)           # the source says the bar ends here

    ties = sum(1 for c in clusters for nn in c[3] if "tie_start" in nn[2])
    hps = sum(1 for c in clusters for nn in c[3]
              if "hammer_start" in nn[2] or "pull_start" in nn[2])
    vibs = sum(1 for c in clusters for nn in c[3] if "vibrato" in nn[2])
    slides = sum(1 for c in clusters for nn in c[3] if "slide_start" in nn[2])
    if ties or hps or vibs or slides:
        print(f"  articulations: {ties} tie(s), {hps} hammer-on/pull-off(s), "
              f"{slides} slide(s), {vibs} vibrato(s)")

    ratio = stems_matched / max(1, len(clusters))
    restf = fill_units / max(1, fill_units + note_units)
    if (ratio < 0.65 or restf > 0.45
            or overfull > max(1, len(measures)) * 0.2):
        print(f"  note: printed rhythm unclear on this PDF (stems matched "
              f"{int(ratio * 100)}%, rest fill {int(restf * 100)}%) - using "
              "spacing-based rhythm instead; double-check note lengths")
        return _spacing_units(clusters), False, None
    print(f"  rhythm read from printed stems and beams "
          f"({int(ratio * 100)}% of notes matched)")
    return events, True, bar_meta

def extract_pdf_text(path):
    """Layout-preserving text of a PDF (for ASCII-tab or chart PDFs)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        return "\n\n".join(p.extract_text(extraction_mode="layout") or ""
                           for p in reader.pages)
    except Exception:
        return None

# ------------------------------------------- stage 1c: chord chart input

HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")

META_RE = re.compile(r"^\s*(capo|tuning|key|tempo|strum|difficulty|artist|"
                     r"song|title|album|by )", re.IGNORECASE)

NEUTRAL_TOKEN_RE = re.compile(r"^(\||-+|–|—|/|%|x\d+|\(x\d+\))$", re.IGNORECASE)

CHORD_TOKEN_RE = re.compile(
    r"^\(?([A-G][#b]?)([A-Za-z0-9#b+°ø]*)(?:/([A-G][#b]?))?\)?[\*]?$")

# suffix -> (musicxml kind, intervals from root, core interval indexes)
# core = intervals that must sound for the chord to be recognizable
CHORD_QUALITIES = {
    "":      ("major",            (0, 4, 7),         (0, 4)),
    "maj":   ("major",            (0, 4, 7),         (0, 4)),
    "M":     ("major",            (0, 4, 7),         (0, 4)),
    "m":     ("minor",            (0, 3, 7),         (0, 3)),
    "min":   ("minor",            (0, 3, 7),         (0, 3)),
    "-":     ("minor",            (0, 3, 7),         (0, 3)),
    "5":     ("power",            (0, 7),            (0, 7)),
    "7":     ("dominant",         (0, 4, 7, 10),     (0, 4, 10)),
    "maj7":  ("major-seventh",    (0, 4, 7, 11),     (0, 4, 11)),
    "M7":    ("major-seventh",    (0, 4, 7, 11),     (0, 4, 11)),
    "m7":    ("minor-seventh",    (0, 3, 7, 10),     (0, 3, 10)),
    "min7":  ("minor-seventh",    (0, 3, 7, 10),     (0, 3, 10)),
    "dim":   ("diminished",       (0, 3, 6),         (0, 3, 6)),
    "o":     ("diminished",       (0, 3, 6),         (0, 3, 6)),
    "dim7":  ("diminished-seventh", (0, 3, 6, 9),    (0, 3, 6, 9)),
    "m7b5":  ("half-diminished",  (0, 3, 6, 10),     (0, 3, 6, 10)),
    "ø":     ("half-diminished",  (0, 3, 6, 10),     (0, 3, 6, 10)),
    "aug":   ("augmented",        (0, 4, 8),         (0, 4, 8)),
    "+":     ("augmented",        (0, 4, 8),         (0, 4, 8)),
    "sus2":  ("suspended-second", (0, 2, 7),         (0, 2)),
    "2":     ("suspended-second", (0, 2, 7),         (0, 2)),
    "sus4":  ("suspended-fourth", (0, 5, 7),         (0, 5)),
    "sus":   ("suspended-fourth", (0, 5, 7),         (0, 5)),
    "7sus4": ("suspended-fourth", (0, 5, 7, 10),     (0, 5, 10)),
    "6":     ("major-sixth",      (0, 4, 7, 9),      (0, 4, 9)),
    "m6":    ("minor-sixth",      (0, 3, 7, 9),      (0, 3, 9)),
    "add9":  ("major",            (0, 4, 7, 14),     (0, 4, 14)),
    "9":     ("dominant-ninth",   (0, 4, 7, 10, 14), (0, 4, 10)),
    "maj9":  ("major-ninth",      (0, 4, 7, 11, 14), (0, 4, 11)),
    "m9":    ("minor-ninth",      (0, 3, 7, 10, 14), (0, 3, 10)),
    "11":    ("dominant-11th",    (0, 4, 7, 10, 17), (0, 10, 17)),
    "13":    ("dominant-13th",    (0, 4, 7, 10, 21), (0, 4, 10)),
}

PC_OF = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# A chord suffix has a grammar: an optional quality, an optional number,
# then any number of extensions. Matching that (rather than just allowing
# the letters it uses) is what keeps words like "Bass" and "Add" out.
SUFFIX_RE = re.compile(
    r"^(?:maj|min|dim|aug|sus|add|m|M|o|°|ø|\+|-)?"
    r"(?:2|4|5|6|7|9|11|13)?"
    r"(?:(?:sus|add|no|b|#)(?:2|4|5|6|9|11|13))*$")

class Chord:
    def __init__(self, display, root_pc, root_step, root_alter,
                 kind, kind_text, pcs_all, pcs_core, bass_pc,
                 bass_step=None, bass_alter=0):
        self.display = display
        self.root_pc = root_pc
        self.root_step = root_step
        self.root_alter = root_alter
        self.kind = kind
        self.kind_text = kind_text
        self.pcs_all = pcs_all
        self.pcs_core = pcs_core
        self.bass_pc = bass_pc
        self.bass_step = bass_step
        self.bass_alter = bass_alter

def parse_chord_token(token):
    """'Am7' / 'Bb' / 'C/G' -> Chord, or None if it isn't a chord."""
    m = CHORD_TOKEN_RE.match(token.strip())
    if not m:
        return None
    root, tail, bass = m.groups()
    root_step = root[0].upper()
    root_alter = {"#": 1, "b": -1}.get(root[1:], 0)
    root_pc = (PC_OF[root_step] + root_alter) % 12
    if tail and tail not in CHORD_QUALITIES and not SUFFIX_RE.match(tail):
        return None                   # a word, not a chord: "Chords", "Bass"
    quality = CHORD_QUALITIES.get(tail)
    if quality is None:
        # unknown suffix: fall back to a plain triad but keep the label
        base = "m" if tail.startswith(("m", "min")) and \
            not tail.startswith("maj") else ""
        kind, intervals, core = CHORD_QUALITIES[base]
        kind = "other"
    else:
        kind, intervals, core = quality
    bass_pc = bass_step = None
    bass_alter = 0
    if bass:
        bass_step = bass[0].upper()
        bass_alter = {"#": 1, "b": -1}.get(bass[1:], 0)
        bass_pc = (PC_OF[bass_step] + bass_alter) % 12
    pcs_all = {(root_pc + i) % 12 for i in intervals}
    if bass_pc is not None:
        pcs_all.add(bass_pc)
    pcs_core = {(root_pc + i) % 12 for i in core}
    return Chord(token.strip("()*"), root_pc, root_step, root_alter,
                 kind, tail, pcs_all, pcs_core, bass_pc,
                 bass_step, bass_alter)

def is_chord_line(line):
    tokens = [t for t in line.split() if not NEUTRAL_TOKEN_RE.match(t)]
    if not tokens:
        return None
    parsed = []
    for t in tokens:
        c = parse_chord_token(t)
        if c is None:
            return None
        parsed.append(c)
    return parsed

def legend_lines(lines):
    """Line numbers of a chord index - the list of chord shapes a web page
    prints above the song. Each line holds one chord and nothing else, and
    unlike real music there are no words underneath. Only counted before
    the first section heading, where such a list actually lives."""
    limit = len(lines)
    for i, ln in enumerate(lines):
        if HEADER_RE.match(ln):
            limit = i
            break
    runs = []
    current = []
    for i in range(limit):
        line = lines[i]
        if not line.strip():
            continue                  # blank lines do not break the run
        chords = is_chord_line(line)
        if chords and len(chords) == 1 and len(line.split()) == 1:
            current.append(i)
        else:
            if len(current) >= 3:
                runs.append(current)
            current = []
    if len(current) >= 3:
        runs.append(current)
    return {i for run in runs for i in run}

# "Tuning: E A D G B E" is page furniture; "Song for Someone" is a title,
# so the label has to be followed by a colon to count as furniture here.
TITLE_SKIP_RE = re.compile(
    r"^\s*(capo|tuning|key|tempo|strumming|difficulty|album|artist|"
    r"author|genre|year|track)\s*:", re.IGNORECASE)

# lines a song page adds around the music
TITLE_NOISE = ("view", "rating", "contribut", "correction", "favorit",
               "favourite", "print", "report", "official", "subscri",
               "this month", "last edit", "pro play", "add to playlist")

def guess_title(text):
    """The first real line of a pasted page is nearly always the song."""
    for raw in clean_pasted_tab(text).splitlines()[:12]:
        line = raw.strip()
        if not line or len(line) > 90:
            continue
        if HEADER_RE.match(line) or TITLE_SKIP_RE.match(line):
            continue
        if looks_like_tab_line(line) or is_chord_line(line):
            continue
        if line.lower().strip(" :-") in ("chords", "chord", "tab", "tabs",
                                         "lyrics", "intro"):
            continue
        letters = sum(1 for ch in line if ch.isalpha())
        if letters < 3:
            continue
        if sum(1 for ch in line if ch.isdigit()) > letters:
            continue                  # a counter, not a name
        low = line.lower()
        if any(word in low for word in TITLE_NOISE):
            continue
        return line
    return None

def parse_chord_chart(text):
    """-> list of (section_or_None, Chord, lyric_fragment_or_None)."""
    lines = clean_pasted_tab(text).splitlines()
    skip = legend_lines(lines)
    if skip:
        print("  note: ignored the page's list of chord shapes "
              "(%d lines)" % len(skip))
    items = []
    pending_section = None
    skipped_tab = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if i in skip or not line.strip():
            i += 1
            continue
        if looks_like_tab_line(line):
            skipped_tab = True
            i += 1
            continue
        h = HEADER_RE.match(line)
        if h:
            pending_section = h.group(1).strip()
            i += 1
            continue
        if META_RE.match(line):
            i += 1
            continue
        chords = is_chord_line(line)
        if chords:
            # column positions of each chord token
            cols = []
            for m in re.finditer(r"\S+", line):
                if not NEUTRAL_TOKEN_RE.match(m.group()):
                    cols.append(m.start())
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            lyric = None
            if (nxt.strip() and not HEADER_RE.match(nxt) and not META_RE.match(nxt)
                    and not looks_like_tab_line(nxt) and is_chord_line(nxt) is None):
                lyric = nxt
                i += 1
            for k, chord in enumerate(chords):
                start = 0 if k == 0 else cols[k]
                end = cols[k + 1] if k + 1 < len(cols) else None
                frag = None
                if lyric is not None:
                    frag = lyric[start:end].strip() or None
                items.append((pending_section, chord, frag))
                pending_section = None
            i += 1
            continue
        # stray text: treat as a lyric continuation of the last chord
        if items:
            sec, chord, frag = items[-1]
            extra = line.strip()
            items[-1] = (sec, chord, (frag + " " + extra) if frag else extra)
        i += 1
    if skipped_tab:
        print("  note: chart contained TAB lines; they were skipped "
              "(convert them separately in TAB mode)")
    return items

def text_evidence(text):
    """-> (tab_system_count, chord_line_count) for mode decisions."""
    lines = clean_pasted_tab(text).splitlines()
    chord_lines = sum(1 for l in lines
                      if l.strip() and not looks_like_tab_line(l)
                      and is_chord_line(l) is not None)
    systems = 0
    run = 0
    for l in lines + [""]:
        if looks_like_tab_line(l):
            run += 1
        else:
            if run >= 3:
                systems += 1
            run = 0
    return systems, chord_lines

def sniff_text_mode(text):
    """Decide whether pasted text is ASCII TAB or a chord chart.

    Mixed pages (charts with an embedded riff) go by whichever dominates.
    """
    systems, chord_lines = text_evidence(text)
    if chord_lines >= 2 and chord_lines >= systems:
        return "chart"
    if systems:
        return "tab"
    return "chart" if chord_lines else "tab"


def pdf_text_mode(text):
    """Like sniff_text_mode, or None if the PDF text is not TAB or a chart.

    LilyPond and scanned-score PDFs often have a text layer of font
    leftovers that is neither; those must fall through to OMR. Mixed
    chord-chart + riff pages follow sniff_text_mode (the chart wins).
    """
    systems, chord_lines = text_evidence(text)
    if systems == 0 and chord_lines < 2:
        return None
    return sniff_text_mode(text)
