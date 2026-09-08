#!/usr/bin/env python3
"""Instruments, settings, and the fretting cost model.

Shared by the reader (tab_read) and the MusicXML writer (tab_write).
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

class TabError(Exception):
    """Bad input or an unreadable score. The CLI prints this as ERROR: ..."""

class Instrument:
    def __init__(self, key, label, tuning, written_offset, max_fret, clef=("G", 2)):
        self.key = key
        self.label = label
        self.tuning = tuning                  # sounding MIDI, index 0 = string 1
        self.written_offset = written_offset  # sounding = written + this
        self.max_fret = max_fret
        self.clef = clef                      # clef for a built notation staff
        self.lo = min(tuning)
        self.hi = max(tuning) + max_fret
        # re-entrant tunings (high-G ukulele) are not pitch-ordered, so the
        # no-crossed-strings rule in the chord search must be switched off
        self.monotonic = all(tuning[i] > tuning[i + 1] for i in range(len(tuning) - 1))

INSTRUMENTS = {
    # 24 frets: an electric commonly has 22 or 24, and lead lines (and the
    # top of a bend) reach well past the 19th
    "guitar": Instrument("guitar", "Guitar", [64, 59, 55, 50, 45, 40], -12, 24),
    "ukulele": Instrument("ukulele", "Ukulele", [69, 64, 60, 67], 0, 15),
    "ukulele-low-g": Instrument("ukulele-low-g", "Ukulele (low G)", [69, 64, 60, 55], 0, 15),
    "bass": Instrument("bass", "Bass", [43, 38, 33, 28], -12, 24, clef=("F", 4)),
}

STEP_TO_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

SHARP_SPELLING = {0: ("C", 0), 1: ("C", 1), 2: ("D", 0), 3: ("D", 1),
                  4: ("E", 0), 5: ("F", 0), 6: ("F", 1), 7: ("G", 0),
                  8: ("G", 1), 9: ("A", 0), 10: ("A", 1), 11: ("B", 0)}

# Cost model for the fretting search. The starred ones can be tuned through
# settings.json "magic" (see apply_settings).
COST_FRET_HEIGHT = 0.25     # * per fret number: prefer low positions

COST_OPEN_BONUS = -0.30     # * open strings are easy

COST_HIGH_FRET = 2.0        # extra penalty above fret 12

COST_SPAN = 0.70            # per fret of span within a chord

COST_SHIFT = 1.00           # * per fret of hand-position movement

COST_STRING_HOP = 0.05      # per string of jump between single notes

SPAN_LIMIT = 4              # * widest comfortable chord stretch, in frets

COMBOS_PER_FRAME = 15       # beam width of the search

COMBOS_ENUM_CAP = 400       # hard cap while enumerating one frame

# ------------------------------------------------------------------ settings

def _settings_path():
    """Dev runs keep settings next to the scripts; the frozen TABifier.exe
    keeps them in %APPDATA%\\TABifier (seeded from the bundled copy) so they
    survive wherever the exe is moved."""
    if getattr(sys, "frozen", False):
        d = Path(os.environ.get("APPDATA", str(Path.home()))) / "TABifier"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "settings.json"
        if not p.exists():
            bundled = Path(getattr(sys, "_MEIPASS", ".")) / "settings.json"
            if bundled.exists():
                shutil.copyfile(bundled, p)
        return p
    return Path(__file__).with_name("settings.json")

SETTINGS_PATH = _settings_path()

NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d)$")

def load_settings():
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"  note: ignoring unreadable settings.json ({e})")
        return {}

def parse_note(token):
    m = NOTE_RE.match(token.strip())
    if not m:
        raise ValueError(f"cannot read note '{token}' (expected like E2 or F#3)")
    step, acc, octave = m.groups()
    midi = (int(octave) + 1) * 12 + STEP_TO_SEMITONE[step.upper()]
    if acc == "#":
        midi += 1
    elif acc == "b":
        midi -= 1
    return midi

def parse_tuning(text):
    """'E2 A2 D3 G3 B3 E4' (low string first, as players say it) ->
    internal string-1-first MIDI list."""
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    if not 2 <= len(tokens) <= 8:
        raise ValueError(f"expected 2-8 strings, got {len(tokens)}")
    return [parse_note(t) for t in reversed(tokens)]

def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "custom"

def instrument_from_custom(d):
    label = str(d.get("name", "")).strip()
    if not label:
        raise ValueError("custom instrument has no name")
    tuning = parse_tuning(str(d.get("tuning", "")))
    frets = int(d.get("frets", 19))
    if not 5 <= frets <= 30:
        raise ValueError(f"frets must be 5-30, got {frets}")
    offset = -12 if d.get("octave_up", True) else 0
    clef = ("F", 4) if str(d.get("clef", "treble")).lower() == "bass" else ("G", 2)
    return Instrument(slugify(label), label, tuning, offset, frets, clef=clef)

def apply_settings(settings):
    """Apply magic knobs to the cost model; return the full instrument dict."""
    global COST_OPEN_BONUS, COST_FRET_HEIGHT, COST_SHIFT, SPAN_LIMIT
    m = settings.get("magic", {})

    def num(key, default, lo, hi):
        try:
            v = float(m.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    COST_OPEN_BONUS = -num("prefer_open_strings", 30, 0, 100) / 100.0
    COST_FRET_HEIGHT = num("stay_low", 25, 0, 100) / 100.0
    COST_SHIFT = num("avoid_jumps", 50, 0, 100) / 50.0
    SPAN_LIMIT = int(num("max_stretch", 4, 2, 6))

    instruments = dict(INSTRUMENTS)
    for d in settings.get("custom_instruments", []):
        try:
            instr = instrument_from_custom(d)
        except (ValueError, KeyError, TypeError) as e:
            print(f"  note: skipping custom instrument {d.get('name', '?')!r}: {e}")
            continue
        if instr.key in INSTRUMENTS:
            print(f"  note: custom instrument overrides built-in '{instr.key}'")
        instruments[instr.key] = instr
    return instruments

def with_capo(instr, capo):
    """A capo is just the same instrument with shorter, higher strings."""
    if capo <= 0:
        return instr
    if capo > instr.max_fret - 5:
        raise TabError(f"capo {capo} leaves too little neck "
                       f"(max {instr.max_fret - 5} for {instr.label})")
    return Instrument(instr.key, f"{instr.label} (capo {capo})",
                      [t + capo for t in instr.tuning], instr.written_offset,
                      instr.max_fret - capo, clef=instr.clef)

CANONICAL_ATTR_ORDER = [
    "footnote", "level", "divisions", "key", "time", "staves", "part-symbol",
    "instruments", "clef", "staff-details", "transpose", "directive",
    "measure-style",
]

IMAGE_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".svg"}

XML_EXTS = {".musicxml", ".xml", ".mxl"}

TAB_TEXT_EXTS = {".txt", ".tab"}
