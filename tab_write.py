#!/usr/bin/env python3
"""Turn parsed events / existing scores into MusicXML with a TAB staff."""

import copy
import os
import re
import xml.etree.ElementTree as ET

import tab_model as model
from tab_model import TabError
from tab_read import Barline, parse_chord_chart

def apply_bar_meta(part, bar_meta, system_breaks=True):
    """Stamp section headings, repeat signs, double bars and the source's
    own line breaks onto measures."""
    measures = part.findall("measure")
    heads = repeats = doubles = breaks = 0
    for i, (m, meta) in enumerate(zip(measures, bar_meta)):
        if not meta:
            continue
        at = 1 if m.find("attributes") is not None else 0
        if system_breaks and meta.get("new_system"):
            # start a new line exactly where the original does, so the bars
            # sit on the page the way she is used to reading them
            m.insert(0, ET.Element("print", {"new-system": "yes"}))
            at += 1
            breaks += 1
        if meta.get("heading"):
            d = ET.Element("direction", {"placement": "above"})
            dt = ET.SubElement(d, "direction-type")
            w = ET.SubElement(dt, "words", {"font-weight": "bold"})
            w.text = meta["heading"]
            m.insert(at, d)
            heads += 1
        if meta.get("start_repeat"):
            bl = ET.Element("barline", {"location": "left"})
            ET.SubElement(bl, "bar-style").text = "heavy-light"
            ET.SubElement(bl, "repeat", {"direction": "forward"})
            m.insert(at, bl)
            repeats += 1
        style = meta.get("end")
        if not style:
            continue
        for old in m.findall("barline"):
            if old.get("location") != "left":
                m.remove(old)
        bl = ET.SubElement(m, "barline", {"location": "right"})
        if style == "repeat":
            ET.SubElement(bl, "bar-style").text = "light-heavy"
            ET.SubElement(bl, "repeat", {"direction": "backward"})
            repeats += 1
        elif style == "double":
            # a double bar at the very end of the piece is the final bar
            last = i == len(measures) - 1
            ET.SubElement(bl, "bar-style").text = ("light-heavy" if last
                                                   else "light-light")
            doubles += 1
    bits = []
    if heads:
        bits.append("%d section heading(s)" % heads)
    if repeats:
        bits.append("%d repeat sign(s)" % repeats)
    if doubles:
        bits.append("%d double bar(s)" % doubles)
    if breaks:
        bits.append("%d line break(s)" % breaks)
    if bits:
        print("  kept " + ", ".join(bits))

_VOICING_CACHE = {}

def voice_chord(chord, instr):
    """Search the fretboard for a playable, good-sounding voicing.

    Returns [(string, fret), ...] for the sounded strings. Bass-register
    instruments just get the root note.
    """
    key = (instr.key, len(instr.tuning), tuple(instr.tuning), chord.display)
    if key in _VOICING_CACHE:
        return _VOICING_CACHE[key]

    want_bass = chord.bass_pc if chord.bass_pc is not None else chord.root_pc
    if min(instr.tuning) < 36:  # bass guitar: play the root
        best = None
        for s, open_midi in enumerate(instr.tuning, start=1):
            for f in range(0, 6):
                if (open_midi + f) % 12 == want_bass:
                    cand = (f, open_midi + f, (s, f))
                    if best is None or cand < best:
                        best = cand
        result = [best[2]] if best else []
        _VOICING_CACHE[key] = result
        return result

    n = len(instr.tuning)
    min_sounded = 3 if n >= 4 else 2
    best_assign, best_score = None, float("inf")

    for pos in range(0, 8):
        options = []
        for open_midi in instr.tuning:  # string 1 (highest) first
            opts = [None]
            if open_midi % 12 in chord.pcs_all:
                opts.append(0)
            for f in range(max(1, pos), min(pos + model.SPAN_LIMIT, instr.max_fret) + 1):
                if (open_midi + f) % 12 in chord.pcs_all:
                    opts.append(f)
            options.append(opts)

        def dfs(i, chosen):
            nonlocal best_assign, best_score
            if i == n:
                sounded = [(s, f) for s, f in
                           ((si + 1, fr) for si, fr in enumerate(chosen))
                           if f is not None]
                if len(sounded) < min_sounded:
                    return
                midis = [instr.tuning[s - 1] + f for s, f in sounded]
                pcs = {m % 12 for m in midis}
                if not chord.pcs_core <= pcs:
                    return
                score = 0.0
                for (_s, f), _m in zip(sounded, midis):
                    score += -0.4 if f == 0 else 0.3 + f * 0.08
                score -= len(sounded) * 0.55
                score -= len(pcs & chord.pcs_all) * 0.2
                score += pos * 0.35
                if min(midis) % 12 != want_bass:
                    score += 2.5
                # muted string sandwiched between sounded strings is awkward
                first = min(s for s, _ in sounded)
                last = max(s for s, _ in sounded)
                for si in range(first, last + 1):
                    if all(s != si for s, _ in sounded):
                        score += 1.0
                if score < best_score:
                    best_score = score
                    best_assign = sounded
                return
            for f in options[i]:
                chosen.append(f)
                dfs(i + 1, chosen)
                chosen.pop()

        dfs(0, [])

    result = best_assign or []
    _VOICING_CACHE[key] = result
    return result

def harmony_el(chord):
    h = ET.Element("harmony")
    root = ET.SubElement(h, "root")
    ET.SubElement(root, "root-step").text = chord.root_step
    if chord.root_alter:
        ET.SubElement(root, "root-alter").text = str(chord.root_alter)
    kind = ET.SubElement(h, "kind", {"text": chord.kind_text})
    kind.text = chord.kind if chord.kind != "other" else "other"
    if chord.bass_step:
        b = ET.SubElement(h, "bass")
        ET.SubElement(b, "bass-step").text = chord.bass_step
        if chord.bass_alter:
            ET.SubElement(b, "bass-alter").text = str(chord.bass_alter)
    return h

def chart_to_tree(text, instr, title):
    """One measure per chord: strummed voicing + chord symbol + lyric line."""
    items = parse_chord_chart(text)
    if not items:
        raise TabError("no chords found. A chord chart has lines of chord "
                 "names (C  G  Am  F) above the lyrics.")
    voiced = 0
    root = ET.Element("score-partwise", {"version": "3.1"})
    work = ET.SubElement(root, "work")
    ET.SubElement(work, "work-title").text = title
    part_list = ET.SubElement(root, "part-list")
    sp = ET.SubElement(part_list, "score-part", {"id": "P1"})
    # left blank on purpose: the staff labels would otherwise
    # print down the left of every system for no gain
    ET.SubElement(sp, "part-name")
    part = ET.SubElement(root, "part", {"id": "P1"})

    for num, (section, chord, lyric) in enumerate(items, start=1):
        measure = ET.SubElement(part, "measure", {"number": str(num)})
        if num == 1:
            attrs = ET.SubElement(measure, "attributes")
            ET.SubElement(attrs, "divisions").text = "4"
            key = ET.SubElement(attrs, "key")
            ET.SubElement(key, "fifths").text = "0"
            time = ET.SubElement(attrs, "time")
            ET.SubElement(time, "beats").text = "4"
            ET.SubElement(time, "beat-type").text = "4"
            clef = ET.SubElement(attrs, "clef")
            ET.SubElement(clef, "sign").text = instr.clef[0]
            ET.SubElement(clef, "line").text = str(instr.clef[1])
        if section:
            d = ET.SubElement(measure, "direction", {"placement": "above"})
            dt = ET.SubElement(d, "direction-type")
            w = ET.SubElement(dt, "words")
            w.set("font-weight", "bold")
            w.text = section
        measure.append(harmony_el(chord))
        voicing = voice_chord(chord, instr)
        if not voicing:
            note = ET.SubElement(measure, "note")
            ET.SubElement(note, "rest")
            ET.SubElement(note, "duration").text = "16"
            ET.SubElement(note, "voice").text = "1"
            ET.SubElement(note, "type").text = "whole"
            continue
        voiced += 1
        ordered = sorted(voicing,
                         key=lambda sf: -(instr.tuning[sf[0] - 1] + sf[1]))
        for j, (s, f) in enumerate(ordered):
            sounding = instr.tuning[s - 1] + f
            written = sounding - instr.written_offset
            note = ET.SubElement(measure, "note")
            if j:
                ET.SubElement(note, "chord")
            note.append(spelled_pitch_el(written))
            ET.SubElement(note, "duration").text = "16"
            ET.SubElement(note, "voice").text = "1"
            ET.SubElement(note, "type").text = "whole"
            nots = ET.SubElement(note, "notations")
            tech = ET.SubElement(nots, "technical")
            ET.SubElement(tech, "string").text = str(s)
            ET.SubElement(tech, "fret").text = str(f)
            if j == 0 and lyric:
                lyr = ET.SubElement(note, "lyric")
                ET.SubElement(lyr, "syllabic").text = "single"
                ET.SubElement(lyr, "text").text = lyric
    print(f"  found {len(items)} chords ({voiced} voiced) across the chart")
    last = part.findall("measure")[-1]
    bar = ET.SubElement(last, "barline", {"location": "right"})
    ET.SubElement(bar, "bar-style").text = "light-heavy"
    return ET.ElementTree(root)

# duration-in-16ths -> (note type, dots); longest first for greedy snapping
TYPE_LUT = [(16, "whole", 0), (12, "half", 1), (8, "half", 0), (6, "quarter", 1),
            (4, "quarter", 0), (3, "eighth", 1), (2, "eighth", 0), (1, "16th", 0)]

def snap_duration(d):
    for v, t, dots in TYPE_LUT:
        if v <= d:
            return v, t, dots
    return 1, "16th", 0

# Rests are written as whole beats rather than dotted values, and never
# straddle a beat they don't start on -- so three beats of silence prints
# as a half plus a quarter, the way engravers (and the source tabs) do it.
REST_LUT = [(16, "whole"), (8, "half"), (4, "quarter"), (2, "eighth"),
            (1, "16th")]

def rest_piece(filled, remaining):
    for v, t in REST_LUT:
        if v <= remaining and filled % v == 0:
            return v, t
    return 1, "16th"

def spelled_pitch_el(midi):
    step, alter = model.SHARP_SPELLING[midi % 12]
    p = ET.Element("pitch")
    ET.SubElement(p, "step").text = step
    if alter:
        ET.SubElement(p, "alter").text = str(alter)
    ET.SubElement(p, "octave").text = str(midi // 12 - 1)
    return p

def events_to_tree(events, instr, title, beats, beat_type, base,
                   bar_meta=None, system_breaks=True):
    """Build a one-part score-partwise tree from parsed TAB events.

    Notes carry <string>/<fret> so the TAB stage keeps the player's own
    fingerings instead of recomputing them.
    """
    base_div = {"sixteenth": 1, "eighth": 2, "quarter": 4}[base]
    measure_cap = beats * (16 // beat_type)

    root = ET.Element("score-partwise", {"version": "3.1"})
    work = ET.SubElement(root, "work")
    ET.SubElement(work, "work-title").text = title
    part_list = ET.SubElement(root, "part-list")
    sp = ET.SubElement(part_list, "score-part", {"id": "P1"})
    # left blank on purpose: the staff labels would otherwise
    # print down the left of every system for no gain
    ET.SubElement(sp, "part-name")
    part = ET.SubElement(root, "part", {"id": "P1"})

    measure_no = 1
    measure = ET.SubElement(part, "measure", {"number": str(measure_no)})
    attrs = ET.SubElement(measure, "attributes")
    ET.SubElement(attrs, "divisions").text = "4"
    key = ET.SubElement(attrs, "key")
    ET.SubElement(key, "fifths").text = "0"
    time = ET.SubElement(attrs, "time")
    ET.SubElement(time, "beats").text = str(beats)
    ET.SubElement(time, "beat-type").text = str(beat_type)
    clef = ET.SubElement(attrs, "clef")
    ET.SubElement(clef, "sign").text = instr.clef[0]
    ET.SubElement(clef, "line").text = str(instr.clef[1])

    filled = 0

    def new_measure():
        nonlocal measure, measure_no, filled
        measure_no += 1
        measure = ET.SubElement(part, "measure", {"number": str(measure_no)})
        filled = 0

    def emit_rest(dur):
        nonlocal filled
        while dur > 0:
            v, t = rest_piece(filled, dur)
            note = ET.SubElement(measure, "note")
            ET.SubElement(note, "rest")
            ET.SubElement(note, "duration").text = str(v)
            ET.SubElement(note, "voice").text = "1"
            ET.SubElement(note, "type").text = t
            filled += v
            dur -= v

    # A barline event means the source printed a barline here, so the bar ends
    # here whatever the note lengths added up to: short bars get the rest they
    # are owed, and a bar that read long stops there instead of pushing its
    # overflow into the next one. The new measure is only opened once
    # something needs to go in it, so a trailing barline leaves no empty bar.
    pending_bar = False

    def ensure_measure():
        nonlocal pending_bar
        if pending_bar:
            new_measure()
            pending_bar = False

    def emit_note(entry, v, t, dots, first, grace):
        """One <note>: a real one of length v, or a grace note with no time."""
        s, f = entry[0], entry[1]
        marks = entry[2] if len(entry) > 2 else ()
        written = instr.tuning[s - 1] + f - instr.written_offset
        note = ET.SubElement(measure, "note")
        if grace:
            ET.SubElement(note, "grace", {"slash": "yes"})
        if not first:
            ET.SubElement(note, "chord")
        note.append(spelled_pitch_el(written))
        if not grace:
            # a grace note carries no duration -- that is what keeps it out
            # of the bar's arithmetic
            ET.SubElement(note, "duration").text = str(v)
        if "tie_start" in marks:
            ET.SubElement(note, "tie", {"type": "start"})
        if "tie_stop" in marks:
            ET.SubElement(note, "tie", {"type": "stop"})
        ET.SubElement(note, "voice").text = "1"
        ET.SubElement(note, "type").text = "eighth" if grace else t
        for _ in range(0 if grace else dots):
            ET.SubElement(note, "dot")
        nots = ET.SubElement(note, "notations")
        if "tie_start" in marks:
            ET.SubElement(nots, "tied", {"type": "start"})
        if "tie_stop" in marks:
            ET.SubElement(nots, "tied", {"type": "stop"})
        if "hammer_stop" in marks or "pull_stop" in marks:
            ET.SubElement(nots, "slur", {"number": "1", "type": "stop"})
        if "hammer_start" in marks or "pull_start" in marks:
            ET.SubElement(nots, "slur", {"number": "1", "type": "start"})
        if "slide_stop" in marks:
            ET.SubElement(nots, "slide",
                          {"number": "2", "type": "stop",
                           "line-type": "solid"})
        if "slide_start" in marks:
            ET.SubElement(nots, "slide",
                          {"number": "2", "type": "start",
                           "line-type": "solid"})
        tech = ET.SubElement(nots, "technical")
        ET.SubElement(tech, "string").text = str(s)
        ET.SubElement(tech, "fret").text = str(f)
        for mark in sorted(marks):
            if mark.startswith(("bend:", "release:")):
                bend = ET.SubElement(tech, "bend")
                ET.SubElement(bend, "bend-alter").text = mark.split(":")[1]
                if mark.startswith("release:"):
                    ET.SubElement(bend, "release")
        if "harmonic" in marks or "pinch" in marks:
            harm = ET.SubElement(tech, "harmonic")
            ET.SubElement(harm, "artificial" if "pinch" in marks
                          else "natural")
        if ("vibrato" in marks or "vibrato_stop" in marks) and first:
            # a wavy line: readers import this as a trill spanner, which the
            # .mscx pass then turns into a proper guitar vibrato squiggle
            # (see apply_vibrato_lines). Start and stop can sit on different
            # notes, which is what gives the squiggle its printed length.
            orn = ET.SubElement(nots, "ornaments")
            if "vibrato" in marks:
                ET.SubElement(orn, "wavy-line", {"number": "2",
                                                 "type": "start"})
            if "vibrato_stop" in marks:
                ET.SubElement(orn, "wavy-line", {"number": "2",
                                                 "type": "stop"})
        if "hammer_start" in marks:
            ET.SubElement(tech, "hammer-on",
                          {"number": "1", "type": "start"}).text = "H"
        if "hammer_stop" in marks:
            ET.SubElement(tech, "hammer-on", {"number": "1", "type": "stop"})
        if "pull_start" in marks:
            ET.SubElement(tech, "pull-off",
                          {"number": "1", "type": "start"}).text = "P"
        if "pull_stop" in marks:
            ET.SubElement(tech, "pull-off", {"number": "1", "type": "stop"})

    for units, notes in events:
        if isinstance(notes, Barline):
            ensure_measure()
            if filled < measure_cap:
                emit_rest(measure_cap - filled)
            pending_bar = True
            continue
        want = units * base_div
        if not notes:
            # explicit rest (from PDF rhythm reading), split at barlines
            while want > 0:
                ensure_measure()
                if filled >= measure_cap:
                    new_measure()
                room = measure_cap - filled
                v, t = rest_piece(filled, min(want, room))
                note = ET.SubElement(measure, "note")
                ET.SubElement(note, "rest")
                ET.SubElement(note, "duration").text = str(v)
                ET.SubElement(note, "voice").text = "1"
                ET.SubElement(note, "type").text = t
                filled += v
                want -= v
            continue
        graces = [e for e in notes if len(e) > 2 and "grace" in e[2]]
        real = [e for e in notes if not (len(e) > 2 and "grace" in e[2])]
        while want > 0:
            ensure_measure()
            if filled >= measure_cap:
                new_measure()
            room = measure_cap - filled
            v, t, dots = snap_duration(min(want, room))
            for entry in graces:
                emit_note(entry, v, t, dots, True, True)
            first = True
            for entry in sorted(real,
                                key=lambda e: -(instr.tuning[e[0] - 1] + e[1])):
                emit_note(entry, v, t, dots, first, False)
                first = False
            filled += v
            # only the first slice keeps the notes; overflow is dropped
            # rather than tied, to keep the rhythm approximation simple
            want = 0

    if filled < measure_cap:
        emit_rest(measure_cap - filled)
    bar = ET.SubElement(measure, "barline", {"location": "right"})
    ET.SubElement(bar, "bar-style").text = "light-heavy"
    if bar_meta:
        apply_bar_meta(part, bar_meta, system_breaks)
    return ET.ElementTree(root)

# ------------------------------------------------------- stage 2: parse score

def midi_of(pitch_el) -> int:
    step = pitch_el.findtext("step")
    octave = int(pitch_el.findtext("octave"))
    alter = float(pitch_el.findtext("alter") or 0)
    return int((octave + 1) * 12 + model.STEP_TO_SEMITONE[step] + alter)

class NoteEvt:
    __slots__ = ("midi", "els", "tie_stop")

    def __init__(self, midi, el, tie_stop):
        self.midi = midi
        self.els = [el]
        self.tie_stop = tie_stop

def collect_frames(part_el):
    """Walk a (single-staff) part; group pitched notes into simultaneity frames.

    Returns an ordered list of frames; each frame is a list of NoteEvt with
    distinct MIDI pitches (unisons across voices are merged).
    """
    frames = {}
    for mi, measure in enumerate(part_el.findall("measure")):
        cursor = 0
        last_onset = 0
        grace_seq = 0
        for el in list(measure):
            tag = el.tag
            if tag == "backup":
                cursor -= int(el.findtext("duration") or 0)
            elif tag == "forward":
                cursor += int(el.findtext("duration") or 0)
            elif tag == "note":
                dur = int(el.findtext("duration") or 0)
                if el.find("rest") is not None or el.find("pitch") is None:
                    if el.find("chord") is None:
                        cursor += dur
                    continue
                is_grace = el.find("grace") is not None
                is_chord = el.find("chord") is not None
                midi = midi_of(el.find("pitch"))
                if is_grace:
                    key = (mi, cursor, 0, grace_seq)
                    grace_seq += 1
                elif is_chord:
                    key = (mi, last_onset, 1, 0)
                else:
                    key = (mi, cursor, 1, 0)
                    last_onset = cursor
                    cursor += dur
                    grace_seq = 0
                tie_stop = any(t.get("type") == "stop" for t in el.findall("tie"))
                frame = frames.setdefault(key, [])
                merged = False
                for evt in frame:
                    if evt.midi == midi:
                        evt.els.append(el)
                        evt.tie_stop = evt.tie_stop and tie_stop
                        merged = True
                        break
                if not merged:
                    frame.append(NoteEvt(midi, el, tie_stop))
    return [frames[k] for k in sorted(frames.keys())]

def pick_transpose(part_el, instr):
    """Semitones to add to written pitch to get sounding pitch.

    Guitar and bass are written an octave above sounding, so when the source
    has no transpose marking we use the instrument's convention.
    """
    tr = part_el.find("measure/attributes/transpose")
    if tr is None:
        return instr.written_offset
    chromatic = int(float(tr.findtext("chromatic") or 0))
    octave = int(tr.findtext("octave-change") or 0)
    return chromatic + 12 * octave

def best_octave_shift(frames, transpose, instr):
    """Global octave correction so the piece sits in instrument range."""
    best = (0, -1)
    for k in (0, 12, -12, 24):
        ok = 0
        for frame in frames:
            for evt in frame:
                s = evt.midi + transpose + k
                if instr.lo <= s <= instr.hi:
                    ok += 1
        if ok > best[1]:
            best = (k, ok)
    return best[0]

def existing_technical(el):
    """Return (string, fret) already on a note, if complete."""
    tech = el.find("notations/technical")
    if tech is None:
        return None
    s = tech.findtext("string")
    f = tech.findtext("fret")
    if s is None or f is None:
        return None
    return (int(s), int(f))

# --------------------------------------------------- stage 3: fretting search

def candidates(sounding, instr):
    out = []
    for i, open_midi in enumerate(instr.tuning):
        fret = sounding - open_midi
        if 0 <= fret <= instr.max_fret:
            out.append((i + 1, fret))  # (string number, fret)
    return out

def frame_combos(sounding_list, instr):
    """Enumerate playable string assignments for one frame.

    Pitches are assigned high-to-low. On pitch-ordered tunings (guitar,
    bass) strings must strictly ascend, which forbids crossed voicings; on
    re-entrant tunings (high-G ukulele) any distinct-string assignment goes.
    """
    order = sorted(range(len(sounding_list)), key=lambda i: -sounding_list[i])
    cands = [candidates(sounding_list[i], instr) for i in order]

    combos = []

    def rec(idx, min_string, used, chosen, max_span):
        if len(combos) >= model.COMBOS_ENUM_CAP:
            return
        if idx == len(order):
            fretted = [f for _, f in chosen if f > 0]
            span = (max(fretted) - min(fretted)) if fretted else 0
            if span <= max_span:
                combos.append(list(chosen))
            return
        for s, f in cands[idx]:
            if instr.monotonic:
                if s < min_string:
                    continue
            elif s in used:
                continue
            chosen.append((s, f))
            rec(idx + 1, s + 1, used | {s}, chosen, max_span)
            chosen.pop()

    for span_limit in (model.SPAN_LIMIT, model.SPAN_LIMIT + 2, instr.max_fret):
        rec(0, 1, set(), [], span_limit)
        if combos:
            break
    remapped = []
    for combo in combos:
        assign = [None] * len(sounding_list)
        for pos, note_i in enumerate(order):
            assign[note_i] = combo[pos]
        remapped.append(assign)
    return remapped

def combo_cost(assign):
    cost = 0.0
    fretted = [f for _, f in assign if f > 0]
    for _, f in assign:
        if f == 0:
            cost += model.COST_OPEN_BONUS
        else:
            cost += f * model.COST_FRET_HEIGHT
            if f > 12:
                cost += model.COST_HIGH_FRET
    if fretted:
        cost += (max(fretted) - min(fretted)) * model.COST_SPAN
    return cost

def combo_pos(assign):
    fretted = [f for _, f in assign if f > 0]
    return min(fretted) if fretted else None

def assign_frets(frames, instr):
    """Dynamic program over frames; returns per-frame chosen assignment.

    Pitches are expected to already be at sounding octave, in range.
    """
    prepared = []
    for frame in frames:
        soundings = []
        for evt in frame:
            s = evt.midi
            while s < instr.lo:
                s += 12
            while s > instr.hi:
                s -= 12
            soundings.append(s)
        combos = frame_combos(soundings, instr)
        if not combos:
            # unplayable cluster (e.g. dense OMR misread); keep highest notes
            keep = sorted(range(len(soundings)),
                          key=lambda i: -soundings[i])[:len(instr.tuning)]
            keep = sorted(keep)
            soundings2 = [soundings[i] for i in keep]
            combos2 = frame_combos(soundings2, instr)
            expanded = []
            for c in combos2:
                full = [None] * len(soundings)
                for pos, i in enumerate(keep):
                    full[i] = c[pos]
                expanded.append(full)
            combos = expanded
        combos.sort(key=lambda c: combo_cost([a for a in c if a is not None]))
        combos = combos[:model.COMBOS_PER_FRAME]
        prepared.append(combos)

    INF = float("inf")
    dp_cost = []
    dp_back = []
    for fi, combos in enumerate(prepared):
        row_cost = []
        row_back = []
        for ci, combo in enumerate(combos):
            clean = [a for a in combo if a is not None]
            local = combo_cost(clean)
            pos = combo_pos(clean)
            if fi == 0:
                row_cost.append((local, pos if pos is not None else 0))
                row_back.append(-1)
                continue
            best = (INF, 0)
            best_prev = -1
            for pi, (pcost, ppos) in enumerate(dp_cost[fi - 1]):
                trans = 0.0
                if pos is not None:
                    trans += abs(pos - ppos) * model.COST_SHIFT
                if len(clean) == 1 and len(prepared[fi - 1][pi]) == 1:
                    prev_assign = [a for a in prepared[fi - 1][pi] if a is not None]
                    if prev_assign:
                        trans += abs(clean[0][0] - prev_assign[0][0]) * model.COST_STRING_HOP
                total = pcost + local + trans
                if total < best[0]:
                    best = (total, pos if pos is not None else ppos)
                    best_prev = pi
            row_cost.append(best)
            row_back.append(best_prev)
        dp_cost.append(row_cost)
        dp_back.append(row_back)

    choice = [0] * len(prepared)
    if prepared:
        last = min(range(len(dp_cost[-1])), key=lambda ci: dp_cost[-1][ci][0])
        choice[-1] = last
        for fi in range(len(prepared) - 1, 0, -1):
            choice[fi - 1] = dp_back[fi][choice[fi]]

    result = []
    for fi, frame in enumerate(frames):
        assign = prepared[fi][choice[fi]] if prepared[fi] else []
        result.append(assign)

    # tied notes keep the fret of the note they are tied from
    seen = {}
    for fi, frame in enumerate(frames):
        for ni, evt in enumerate(frame):
            a = result[fi][ni] if ni < len(result[fi]) else None
            if evt.tie_stop and evt.midi in seen:
                result[fi][ni] = seen[evt.midi]
            elif a is not None:
                seen[evt.midi] = a
    return result

# ------------------------------------------------- stage 4: build the TAB part

def bake_pitch_shift(part_el, semitones, instr):
    """Rewrite every pitch shifted by `semitones`, clamped into range.

    The TAB part's pitches are stored at sounding octave so that pitch,
    string, fret, and staff tuning are mutually consistent -- otherwise
    MuseScore discards our fingerings on import and recomputes its own.
    Spelling is cosmetic on a TAB staff, so respelling prefers sharps.
    """
    clamped = 0
    for note in part_el.iter("note"):
        p = note.find("pitch")
        if p is None:
            continue
        original = midi_of(p)
        target = original + semitones
        adj = target
        while adj < instr.lo:
            adj += 12
        while adj > instr.hi:
            adj -= 12
        if adj != target:
            clamped += 1
        delta = adj - original
        if delta == 0:
            continue
        if delta % 12 == 0:
            oct_el = p.find("octave")
            oct_el.text = str(int(oct_el.text) + delta // 12)
        else:
            for c in list(p):
                p.remove(c)
            new_p = spelled_pitch_el(adj)
            for c in list(new_p):
                p.append(c)
            acc = note.find("accidental")
            if acc is not None:
                note.remove(acc)
    if clamped:
        print(f"  note: {clamped} note(s) octave-shifted individually to fit range")

def strip_from_copy(part_el):
    """Remove elements that don't belong on a TAB staff."""
    for measure in part_el.findall("measure"):
        for tag in ("harmony", "direction", "print", "figured-bass", "sound"):
            for el in measure.findall(tag):
                measure.remove(el)
        for note in measure.findall("note"):
            for lyr in note.findall("lyric"):
                note.remove(lyr)
    # remove every clef and transpose; the octave shift is baked into the
    # pitches and the TAB clef is added to measure 1 afterwards
    for attrs in part_el.findall("measure/attributes"):
        for tag in ("clef", "transpose"):
            for el in attrs.findall(tag):
                attrs.remove(el)

def strip_technicals(part_el):
    """Remove string/fret marks (used to clean the notation staff)."""
    for note in part_el.iter("note"):
        nots = note.find("notations")
        if nots is None:
            continue
        tech = nots.find("technical")
        if tech is None:
            continue
        for t in list(tech):
            if t.tag in ("string", "fret"):
                tech.remove(t)
        if len(tech) == 0:
            nots.remove(tech)
        if len(nots) == 0:
            note.remove(nots)

def keep_single_staff(part_el, staff):
    """Reduce a multi-staff part (piano) to one staff, preserving timing."""
    staves_el = part_el.find("measure/attributes/staves")
    if staves_el is None or int(staves_el.text) <= 1:
        return False
    for measure in part_el.findall("measure"):
        for note in measure.findall("note"):
            st = note.findtext("staff")
            if st is not None and int(st) != staff:
                if note.find("chord") is not None or note.find("grace") is not None:
                    measure.remove(note)
                else:
                    # keep timing: replace with an invisible rest
                    for child in list(note):
                        if child.tag in ("pitch", "tie", "accidental", "stem",
                                        "notehead", "beam", "notations", "dot"):
                            note.remove(child)
                    if note.find("rest") is None:
                        note.insert(0, ET.Element("rest"))
                    note.set("print-object", "no")
        for attrs in measure.findall("attributes"):
            st = attrs.find("staves")
            if st is not None:
                st.text = "1"
        for note in measure.findall("note"):
            st = note.find("staff")
            if st is not None:
                note.remove(st)
        for d in measure.findall("direction"):
            measure.remove(d)
    return True

def rebuild_attributes(attrs, extra):
    """Re-order attribute children canonically after inserting new ones."""
    children = list(attrs) + extra
    for c in list(attrs):
        attrs.remove(c)
    rank = {t: i for i, t in enumerate(model.CANONICAL_ATTR_ORDER)}
    children.sort(key=lambda c: rank.get(c.tag, 50))
    for c in children:
        attrs.append(c)

def make_tab_attributes(instr):
    clef = ET.Element("clef")
    ET.SubElement(clef, "sign").text = "TAB"
    if len(instr.tuning) == 6:
        ET.SubElement(clef, "line").text = "5"

    details = ET.Element("staff-details")
    ET.SubElement(details, "staff-lines").text = str(len(instr.tuning))
    # staff-tuning line 1 = bottom line = highest string number
    for line, midi in enumerate(reversed(instr.tuning), start=1):
        st = ET.SubElement(details, "staff-tuning", {"line": str(line)})
        octave, semi = divmod(midi, 12)
        step, alter = model.SHARP_SPELLING[semi]
        ET.SubElement(st, "tuning-step").text = step
        if alter:
            ET.SubElement(st, "tuning-alter").text = str(alter)
        ET.SubElement(st, "tuning-octave").text = str(octave - 1)

    return [clef, details]

def inject_technicals(frames, assignments):
    for frame, assign in zip(frames, assignments):
        for evt, sf in zip(frame, assign):
            if sf is None:
                continue
            string, fret = sf
            for el in evt.els:
                nots = el.find("notations")
                if nots is None:
                    nots = ET.SubElement(el, "notations")
                tech = nots.find("technical")
                if tech is None:
                    tech = ET.SubElement(nots, "technical")
                for t in list(tech):
                    if t.tag in ("string", "fret"):
                        tech.remove(t)
                ET.SubElement(tech, "string").text = str(string)
                ET.SubElement(tech, "fret").text = str(fret)

def add_tab_part(tree, src_part_id, staff, instr):
    root = tree.getroot()
    if root.tag != "score-partwise":
        raise TabError(f"unsupported MusicXML root <{root.tag}> "
                 "(only score-partwise is handled)")

    part_list = root.find("part-list")
    parts = root.findall("part")
    src_part = None
    for p in parts:
        if p.get("id") == src_part_id:
            src_part = p
    if src_part is None:
        raise TabError(f"part {src_part_id} not found")

    tab_part = copy.deepcopy(src_part)
    tab_id = src_part_id + "TAB"
    tab_part.set("id", tab_id)

    reduced = keep_single_staff(tab_part, staff)
    if reduced:
        print(f"  note: multi-staff part reduced to staff {staff} for the TAB")

    pre_frames = collect_frames(tab_part)
    if not pre_frames:
        raise TabError("no pitched notes found in the selected part/staff")

    # if every note already carries string+fret (typed TAB input), keep the
    # player's fingerings; otherwise optimize our own
    forced = []
    all_forced = True
    for frame in pre_frames:
        row = []
        for evt in frame:
            sf = existing_technical(evt.els[0])
            row.append(sf)
            if sf is None:
                all_forced = False
        forced.append(row)

    transpose = pick_transpose(tab_part, instr)
    if all_forced:
        shift = 0
        print("  keeping the fingerings from the TAB input")
    else:
        shift = best_octave_shift(pre_frames, transpose, instr)
        if shift:
            print(f"  note: shifted piece by {shift:+d} semitones to fit range")

    strip_from_copy(tab_part)
    bake_pitch_shift(tab_part, transpose + shift, instr)

    frames = collect_frames(tab_part)
    if all_forced:
        assignments = forced
    else:
        assignments = assign_frets(frames, instr)
    inject_technicals(frames, assignments)

    first_measure = tab_part.find("measure")
    attrs = first_measure.find("attributes")
    if attrs is None:
        attrs = ET.Element("attributes")
        first_measure.insert(0, attrs)
    rebuild_attributes(attrs, make_tab_attributes(instr))

    # part-list entry + bracket grouping the pair
    src_sp = None
    for sp in part_list.findall("score-part"):
        if sp.get("id") == src_part_id:
            src_sp = sp
    tab_sp = ET.Element("score-part", {"id": tab_id})
    ET.SubElement(tab_sp, "part-name")
    list_children = list(part_list)
    idx = list_children.index(src_sp)
    grp_start = ET.Element("part-group", {"type": "start", "number": "99"})
    ET.SubElement(grp_start, "group-symbol").text = "bracket"
    ET.SubElement(grp_start, "group-barline").text = "yes"
    grp_stop = ET.Element("part-group", {"type": "stop", "number": "99"})
    part_list.insert(idx, grp_start)
    part_list.insert(idx + 2, tab_sp)
    part_list.insert(idx + 3, grp_stop)

    # insert the tab <part> right after its source part
    root_children = list(root)
    pos = root_children.index(src_part)
    root.insert(pos + 1, tab_part)

    used = [sf[1] for a in assignments for sf in a if sf]
    if used:
        print(f"  frets used: {min(used)}-{max(used)} across {len(used)} notes")
    return tree

DIRECTION_VOCAB = re.compile(
    r"^(rit|rall|accel|cresc|dim|decresc|a tempo|tempo|swing|solo|fine|"
    r"d\.?\s?c\.?|d\.?\s?s\.?|coda|segno|pedal|ped|con |molto|poco|legato|"
    r"staccato|dolce|espress|arr|music by|words by|allegro|allegretto|"
    r"andante|andantino|moderato|adagio|largo|lento|vivace|presto|"
    r"let ring|palm mute|verse|chorus|intro|outro|bridge)", re.IGNORECASE)

# Tempo words Audiveris often repeats as a credit already in the title.
_TEMPO_CREDIT = re.compile(
    r"^(largo|lento|adagio|andante|andantino|moderato|allegretto|"
    r"allegro|vivace|presto|prestissimo|grave|maestoso|sostenuto|"
    r"cantabile|animato|con moto|a tempo)\s*[.!]?\s*$",
    re.IGNORECASE)

# Page furniture OCR often files as lyrics: Mutopia footers, licences, URLs.
# `.` in the middle of a token is the OCR splitting "Creative Commons".
FOOTER_NOISE = re.compile(
    r"mutopia|creativecommons|creative.{0,20}commons|lilypond|linpond|"
    r"typeset|copyright|\blicen[cs]ed?\b|all rights reserved|share.?ali|"
    r"free to(?:\s+(?:distribute|download|modify))|\bdistribute\b|by-sa|by-nc|"
    r"imslp|public domain|engraved by|www\.|https?://|"
    r"\battribution\b|\bunported\b|licensedunder|sheet.{0,4}music|"
    r"for.{0,12}details|\bdownload\b|\bfreedom to\b|"
    r"^reference:?$|^\s*perform\.?\s*$|"
    r"larsen|\bbommel\b|"
    r"/[O0-9]\d/\d{2}|"
    r"see original|correct errors|first line past|kb\.dk|rischel",
    re.IGNORECASE)

# Tokens that are licence/typesetter junk rather than sung words. Used to
# flood from a FOOTER_NOISE seed along neighbouring measures so "using" /
# "modify, and" drop with the footer they were split from. Stopwords alone
# are not enough to *start* a drop -- only to continue one.
_FOOTER_WORDS = {
    "mutopia", "project", "typeset", "using", "lilypond", "linpond",
    "creative", "commons", "attribution", "unported", "sharealike",
    "sharealiike", "shareaiike", "alike",
    "licensed", "licensedunder", "license", "licence", "copyright",
    "distribute", "modify", "download", "freedom", "details", "see",
    "under", "international", "larsen", "bommel", "glen", "rhen",
    "stephen", "louie", "van", "sheet", "music", "perform", "reserved",
    "rights", "public", "domain", "engraved", "reference", "free",
    "http", "https", "www", "org", "com", "pdf", "xml",
    "nc", "nd", "sa",
}
_STOPWORDS = {
    "a", "an", "the", "to", "by", "and", "of", "or", "in", "on", "at",
    "as", "is", "are", "for", "with", "from", "this", "that",
}
_DYNAMICS = {
    "p", "f", "mf", "mp", "ff", "pp", "ppp", "fff", "sf", "sfz", "fp",
    "cresc", "dim", "rit", "rall",
}

def rescue_lyric_directions(part_el):
    """Reclassify OCR text that is really lyrics.

    In grand-staff scores Audiveris only accepts text below the whole part
    as lyrics, so lyric lines between piano staves come out as <direction>
    words. Anything that reads like a lyric line (several words, mostly
    lowercase, not musical vocabulary) is re-attached to the melody note at
    its position as a lyric phrase.
    """
    moved = 0
    for measure in part_el.findall("measure"):
        children = list(measure)
        for d in [c for c in children if c.tag == "direction"]:
            wtexts = [w.text or "" for w in d.findall("direction-type/words")]
            text = " ".join(t.strip() for t in wtexts if t.strip())
            if FOOTER_NOISE.search(text):
                measure.remove(d)
                continue
            if len(text) < 8 or len(text.split()) < 2:
                continue
            letters = [ch for ch in text if ch.isalpha()]
            if not letters:
                continue
            if sum(ch.islower() for ch in letters) / len(letters) < 0.6:
                continue
            if DIRECTION_VOCAB.match(text.strip()):
                continue
            target = None
            for c in children[children.index(d) + 1:]:
                if (c.tag == "note" and c.find("pitch") is not None
                        and (c.findtext("staff") or "1") == "1"
                        and c.find("chord") is None):
                    target = c
                    break
            if target is None:
                for c in children:
                    if (c.tag == "note" and c.find("pitch") is not None
                            and (c.findtext("staff") or "1") == "1"):
                        target = c
                        break
            if target is None or target.find("lyric") is not None:
                continue
            lyr = ET.SubElement(target, "lyric")
            ET.SubElement(lyr, "syllabic").text = "single"
            ET.SubElement(lyr, "text").text = text
            measure.remove(d)
            moved += 1
    if moved:
        print(f"  rescued {moved} lyric line(s) the OCR had filed as directions")


def _lyric_texts(measure):
    """[(note, lyric_el, text)] for every lyric in the measure."""
    out = []
    for note in measure.findall("note"):
        for lyr in list(note.findall("lyric")):
            text = " ".join((t.text or "") for t in lyr.findall("text")).strip()
            if text:
                out.append((note, lyr, text))
    return out


def _is_dynamic_lyric(text):
    words = re.findall(r"[A-Za-z]+", text)
    return bool(words) and all(w.lower() in _DYNAMICS for w in words)


def _is_symbol_junk(text):
    """Copyright marks / replacement chars with no real letters or digits."""
    return bool(text) and not re.search(r"[A-Za-z0-9]", text)


def _is_floodable_lyric(text):
    """True when this measure's lyrics look like licence fragments, not song.

    Short tokens, typesetter names, dates, and stopwords are floodable so
    they drop with a neighbouring footer seed. A real sung word blocks the
    flood, so a lyric line next to a Mutopia footer is kept.
    """
    if not (text or "").strip():
        return False
    if _is_dynamic_lyric(text):
        return False
    if re.search(r"/[O0-9]\d/\d{2}", text):
        return True
    toks = re.findall(r"[A-Za-z]+|\d+", text)
    if not toks:
        return True
    for t in toks:
        tl = t.lower()
        if tl in _DYNAMICS or tl in _FOOTER_WORDS or tl in _STOPWORDS:
            continue
        if tl.isdigit():
            continue
        if len(tl) <= 2:
            continue
        return False
    return True


def scrub_page_furniture(part_el):
    """Drop OCR footer/copyright text, whether it landed as lyrics or directions.

    Mutopia/LilyPond PDFs print a licence line under the music; Audiveris
    files each word as its own lyric, often across many measures. We seed
    from a keyword hit, then flood through neighbouring fragment-like
    measures (one empty bar is allowed). Sung words stop the flood.
    """
    n = 0
    for measure in part_el.findall("measure"):
        for d in list(measure.findall("direction")):
            wtexts = [w.text or "" for w in d.findall("direction-type/words")]
            text = " ".join(t.strip() for t in wtexts if t.strip())
            if text and FOOTER_NOISE.search(text):
                measure.remove(d)
                n += 1
    measures = part_el.findall("measure")
    per = [_lyric_texts(m) for m in measures]
    joined = [" ".join(t for _n, _l, t in items) for items in per]
    count = len(measures)
    drop = [False] * count
    for i, text in enumerate(joined):
        if not text:
            continue
        if _is_symbol_junk(text) or FOOTER_NOISE.search(text):
            drop[i] = True
            continue
        window = " ".join(joined[max(0, i - 1):i + 2])
        if _is_floodable_lyric(text) and FOOTER_NOISE.search(window):
            drop[i] = True
    changed = True
    while changed:
        changed = False
        for i, text in enumerate(joined):
            if drop[i] or not text or not _is_floodable_lyric(text):
                continue
            for j in (i - 1, i + 1, i - 2, i + 2):
                if not (0 <= j < count and drop[j]):
                    continue
                if abs(j - i) == 2:
                    mid = (i + j) // 2
                    if joined[mid] and not drop[mid]:
                        continue
                drop[i] = True
                changed = True
                break
    for i, items in enumerate(per):
        if not drop[i]:
            continue
        for note, lyr, _t in items:
            note.remove(lyr)
            n += 1
    if n:
        print(f"  dropped {n} footer/copyright marking(s) the OCR invented")


def _pluck_from_lyric(text):
    """Right-hand p/i/m that OCR filed as a lyric, or None.

    Capital P (optionally with stray punctuation) is pulgar -- a lone
    piano 'p' is a dynamic and is left alone. i/m are only converted on
    parts that have no sung words, so 'in' / 'me' syllables stay lyrics.
    """
    t = (text or "").strip()
    if re.fullmatch(r"P[\s.'`]*", t):
        return "p"
    if t in ("i", "m"):
        return t
    return None


def _part_has_sung_lyrics(part_el):
    for measure in part_el.findall("measure"):
        for _n, _l, text in _lyric_texts(measure):
            words = re.findall(r"[A-Za-z]{3,}", text)
            if any(w.lower() not in _FOOTER_WORDS
                   and w.lower() not in _DYNAMICS
                   and w.lower() not in _STOPWORDS
                   for w in words):
                return True
    return False


def _set_pluck(note, value):
    notations = note.find("notations")
    if notations is None:
        notations = ET.SubElement(note, "notations")
    tech = notations.find("technical")
    if tech is None:
        tech = ET.SubElement(notations, "technical")
    if tech.find("pluck") is None:
        ET.SubElement(tech, "pluck").text = value


def rescue_fingering_lyrics(part_el):
    """Turn classical-guitar p/i/m OCR lyrics into MusicXML pluck marks."""
    sung = _part_has_sung_lyrics(part_el)
    n = 0
    for measure in part_el.findall("measure"):
        for note, lyr, text in _lyric_texts(measure):
            pluck = _pluck_from_lyric(text)
            if pluck is None:
                continue
            if pluck != "p" and sung:
                continue
            _set_pluck(note, pluck)
            note.remove(lyr)
            n += 1
    if n:
        print(f"  moved {n} right-hand fingering(s) off the lyric line")


_INSTRUMENT_CREDIT = re.compile(
    r"^(guitar|voice|vocals?|piano|ukulele|uke|bass|violin|viola|cello|"
    r"flute|mandolin|banjo)\s*$",
    re.IGNORECASE)


def _is_junk_credit(words):
    """Measure numbers / fingering / instrument-name OCR dumped into <credit>."""
    text = (words or "").strip()
    if not text:
        return False
    if _INSTRUMENT_CREDIT.match(text):
        return True
    if not re.search(r"[A-Za-z0-9]", text):
        return True
    return bool(re.fullmatch(r"[\d\s©\u00a9\u00ae\ufffd().,\-]+", text))


def _title_texts(root):
    out = []
    for t in (root.findtext("movement-title"),
              root.findtext("work/work-title")):
        if t and t.strip():
            out.append(t.strip())
    return out


def scrub_score_furniture(tree):
    """Drop licence lines and numeric junk from score-level credits."""
    root = tree.getroot()
    n = 0
    titles = _title_texts(root)
    for c in list(root.findall("credit")):
        words = " ".join((w.text or "").strip()
                         for w in c.iter("credit-words")).strip()
        if not words:
            continue
        dup_tempo = (_TEMPO_CREDIT.match(words)
                     and any(re.search(r"(?i)(?<!\w)" + re.escape(words)
                                       + r"(?!\w)", t)
                             for t in titles))
        if FOOTER_NOISE.search(words) or _is_junk_credit(words) or dup_tempo:
            root.remove(c)
            n += 1
    ident = root.find("identification")
    if ident is not None:
        for cr in list(ident.findall("creator")):
            text = (cr.text or "").strip()
            if text and FOOTER_NOISE.search(text):
                ident.remove(cr)
                n += 1
        for rights in list(ident.findall("rights")):
            text = (rights.text or "").strip()
            if text and FOOTER_NOISE.search(text):
                ident.remove(rights)
                n += 1
    if n:
        print(f"  dropped {n} footer/copyright credit(s)")


def strip_staff_labels(tree, part_id=None):
    """Don't print a name down the left of the tabbed instrument.

    Other parts (Voice vs Guitar on a song) keep their labels. Only the
    notation staff we are tabbing, and the TAB staff we add, go nameless
    so 'Voice' / 'Guitar' is not repeated on every system.
    """
    part_list = tree.getroot().find("part-list")
    if part_list is None:
        return
    for sp in part_list.findall("score-part"):
        sid = sp.get("id") or ""
        if part_id and sid != part_id and not sid.endswith("TAB"):
            continue
        for tag in ("part-name", "part-abbreviation"):
            el = sp.find(tag)
            if el is not None:
                el.text = None
                for child in list(el):
                    el.remove(child)

def strip_layout_hints(tree):
    """Remove imported page/system breaks and spacing hints.

    Scanned and downloaded MusicXML carries the ORIGINAL page layout, which
    was computed for single-staff systems. Our systems are roughly twice as
    tall once the TAB staff is added, so inherited breaks leave big blank
    stretches; clearing them lets MuseScore reflow the score properly.
    """
    n = 0
    for part in tree.getroot().findall("part"):
        for measure in part.findall("measure"):
            for pr in measure.findall("print"):
                measure.remove(pr)
                n += 1
    if n:
        print(f"  cleared {n} imported layout hints so pages reflow cleanly")

def remove_existing_tab(tree):
    """Drop a previously generated TAB part so re-runs regenerate it."""
    root = tree.getroot()
    removed = False
    for part in list(root.findall("part")):
        if part.get("id", "").endswith("TAB"):
            root.remove(part)
            removed = True
    part_list = root.find("part-list")
    if part_list is not None:
        for sp in list(part_list.findall("score-part")):
            if sp.get("id", "").endswith("TAB"):
                part_list.remove(sp)
        for pg in list(part_list.findall("part-group")):
            if pg.get("number") == "99":
                part_list.remove(pg)
    if removed:
        print("  note: replaced the TAB staff from a previous run")

def choose_part(tree, requested):
    """Pick the part to tab: --part override, else the one with most notes."""
    root = tree.getroot()
    parts = root.findall("part")
    if requested:
        for p in parts:
            if p.get("id") == requested:
                return requested
        raise TabError(f"part '{requested}' not in score "
                 f"(have: {', '.join(p.get('id') for p in parts)})")
    best_id, best_notes = None, -1
    for p in parts:
        n = sum(1 for note in p.iter("note") if note.find("pitch") is not None)
        if n > best_notes:
            best_id, best_notes = p.get("id"), n
    if len(parts) > 1:
        print(f"  note: score has {len(parts)} parts; tabbing '{best_id}' "
              f"({best_notes} notes). Use --part to override.")
    return best_id

def add_tab_staff(tree, instr, staff=1, part=None, *, from_omr=False,
                  ascii_mode=False):
    """Add a TAB staff under the chosen notation part. Mutates `tree`.

    from_omr: re-attach lyric lines the scanner filed as directions.
    ascii_mode: the fingerings came from typed/engraved TAB, so keep them
    and strip the helper string/fret marks off the notation staff.
    """
    if not ascii_mode:
        strip_layout_hints(tree)
    remove_existing_tab(tree)
    part_id = choose_part(tree, part)
    strip_staff_labels(tree, part_id)
    if from_omr:
        scrub_score_furniture(tree)
        for p in tree.getroot().findall("part"):
            scrub_page_furniture(p)
            rescue_lyric_directions(p)
            scrub_page_furniture(p)
            rescue_fingering_lyrics(p)
    add_tab_part(tree, part_id, staff, instr)
    if ascii_mode:
        for p in tree.getroot().findall("part"):
            if p.get("id") == part_id:
                strip_technicals(p)
    return tree
