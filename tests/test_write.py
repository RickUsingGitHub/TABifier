"""MusicXML writer tests. No MuseScore or Audiveris needed."""
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tab_model import INSTRUMENTS, TabError
from tab_read import parse_ascii_tab
from tab_write import (
    add_tab_staff, chart_to_tree, events_to_tree, rescue_lyric_directions,
    rescue_fingering_lyrics, scrub_page_furniture, scrub_score_furniture,
    voice_chord, _lyric_texts,
)
from tab_read import parse_chord_token

HERE = Path(__file__).resolve().parent
GUITAR = INSTRUMENTS["guitar"]
UKE = INSTRUMENTS["ukulele"]
BASS = INSTRUMENTS["bass"]


def _read(name):
    return (HERE / name).read_text(encoding="utf-8")


def _tab_part(tree):
    tabs = [p for p in tree.getroot().findall("part")
            if p.get("id", "").endswith("TAB")]
    return tabs[0] if tabs else None


def _frets(part):
    out = []
    for note in part.iter("note"):
        tech = note.find("notations/technical")
        if tech is None:
            continue
        s, f = tech.findtext("string"), tech.findtext("fret")
        if s is not None and f is not None:
            out.append((int(s), int(f)))
    return out


class EventsToTreeTests(unittest.TestCase):
    def test_riff_keeps_typed_fingerings(self):
        events = parse_ascii_tab(_read("guitar_riff.txt"), GUITAR)
        tree = events_to_tree(events, GUITAR, "Riff", 4, 4, "eighth")
        add_tab_staff(tree, GUITAR, ascii_mode=True)
        tab = _tab_part(tree)
        self.assertIsNotNone(tab)
        frets = _frets(tab)
        self.assertTrue(frets)
        self.assertIn((5, 3), frets)   # A-string 3
        self.assertIn((4, 2), frets)   # D-string 2
        self.assertIn((2, 0), frets)   # open B
        self.assertIn((2, 1), frets)
        # notation staff must not still carry string/fret helper marks
        notation = next(p for p in tree.getroot().findall("part")
                        if p.get("id") == "P1")
        self.assertEqual(_frets(notation), [])

    def test_hammer_written_as_notations(self):
        events = parse_ascii_tab(_read("guitar_riff.txt"), GUITAR)
        tree = events_to_tree(events, GUITAR, "Riff", 4, 4, "eighth")
        xml = ET.tostring(tree.getroot(), encoding="unicode")
        self.assertIn("hammer-on", xml)


class SheetToTabTests(unittest.TestCase):
    def test_ode_melody_gets_playable_frets(self):
        tree = ET.parse(HERE / "ode_melody.musicxml")
        add_tab_staff(tree, GUITAR)
        tab = _tab_part(tree)
        self.assertIsNotNone(tab)
        frets = _frets(tab)
        self.assertGreaterEqual(len(frets), 8)
        self.assertTrue(all(0 <= f <= 24 for _s, f in frets))
        self.assertTrue(all(1 <= s <= 6 for s, _f in frets))

    def test_lyrics_survive(self):
        tree = ET.parse(HERE / "lyric_test.musicxml")
        add_tab_staff(tree, GUITAR)
        notation = next(p for p in tree.getroot().findall("part")
                        if p.get("id") == "P1")
        texts = [el.text for el in notation.iter("text") if el.text]
        self.assertTrue(any("Sing" in t or "made" in t for t in texts))
        self.assertIsNotNone(_tab_part(tree))

    def test_lyrics_survive_omr_furniture_scrub(self):
        tree = ET.parse(HERE / "lyric_test.musicxml")
        add_tab_staff(tree, GUITAR, from_omr=True)
        notation = next(p for p in tree.getroot().findall("part")
                        if p.get("id") == "P1")
        texts = [el.text for el in notation.iter("text") if el.text]
        self.assertTrue(any("Sing" in t for t in texts))
        self.assertTrue(any("made" in t for t in texts))
        self.assertTrue(any("words" in t for t in texts))

    def test_other_part_keeps_its_name(self):
        root = ET.Element("score-partwise")
        pl = ET.SubElement(root, "part-list")
        v = ET.SubElement(pl, "score-part", {"id": "P1"})
        ET.SubElement(v, "part-name").text = "Voice"
        g = ET.SubElement(pl, "score-part", {"id": "P2"})
        ET.SubElement(g, "part-name").text = "Guitar"
        for pid, nnotes in (("P1", 2), ("P2", 8)):
            part = ET.SubElement(root, "part", {"id": pid})
            m = ET.SubElement(part, "measure", {"number": "1"})
            attrs = ET.SubElement(m, "attributes")
            ET.SubElement(attrs, "divisions").text = "1"
            for _ in range(nnotes):
                note = ET.SubElement(m, "note")
                pitch = ET.SubElement(note, "pitch")
                ET.SubElement(pitch, "step").text = "C"
                ET.SubElement(pitch, "octave").text = "4"
                ET.SubElement(note, "duration").text = "1"
                ET.SubElement(note, "type").text = "quarter"
        add_tab_staff(ET.ElementTree(root), GUITAR)
        names = {sp.get("id"): (sp.findtext("part-name") or "").strip()
                 for sp in root.find("part-list").findall("score-part")}
        self.assertEqual(names.get("P1"), "Voice")
        self.assertEqual(names.get("P2"), "")

    def test_dense_chord_is_thinned_to_six_strings(self):
        tree = ET.parse(HERE / "chord_melody.musicxml")
        add_tab_staff(tree, GUITAR)
        tab = _tab_part(tree)
        # first frame is five simultaneous notes; all must land on distinct strings
        first_measure_notes = []
        measure = tab.find("measure")
        for note in measure.findall("note"):
            if note.find("pitch") is None:
                continue
            tech = note.find("notations/technical")
            if tech is None:
                continue
            first_measure_notes.append(int(tech.findtext("string")))
            if note.find("chord") is None and first_measure_notes:
                # first attack in the measure may be followed by chord notes
                pass
        # the opening C chord (5 notes) must not assign the same string twice
        opening = []
        for note in measure.findall("note"):
            if note.find("rest") is not None:
                continue
            if note.find("chord") is None and opening:
                break
            s = note.findtext("notations/technical/string")
            if s:
                opening.append(int(s))
        self.assertEqual(len(opening), len(set(opening)))
        self.assertLessEqual(len(opening), 6)


class ChartToTreeTests(unittest.TestCase):
    def test_one_bar_per_chord(self):
        tree = chart_to_tree(_read("chart_test.txt"), GUITAR, "So Fake")
        add_tab_staff(tree, GUITAR, ascii_mode=True)
        part = next(p for p in tree.getroot().findall("part")
                    if p.get("id") == "P1")
        measures = part.findall("measure")
        self.assertEqual(len(measures), 18)
        kinds = [h.findtext("kind") for h in part.iter("harmony")]
        self.assertIn("major", kinds)
        self.assertIn("minor-seventh", kinds)
        lyrics = [t.text for t in part.iter("text") if t.text]
        self.assertTrue(any("Made up words" in (t or "") for t in lyrics))
        self.assertIsNotNone(_tab_part(tree))

    def test_empty_chart_raises(self):
        with self.assertRaises(TabError):
            chart_to_tree("hello there\nno chords at all", GUITAR, "Nope")


class VoicingTests(unittest.TestCase):
    def test_c_major_guitar_has_open_strings(self):
        chord = parse_chord_token("C")
        voiced = voice_chord(chord, GUITAR)
        self.assertGreaterEqual(len(voiced), 3)
        frets = [f for _s, f in voiced]
        self.assertIn(0, frets)

    def test_ukulele_c_is_on_four_strings(self):
        chord = parse_chord_token("C")
        voiced = voice_chord(chord, UKE)
        self.assertTrue(voiced)
        self.assertTrue(all(1 <= s <= 4 for s, _f in voiced))

    def test_bass_plays_the_root(self):
        chord = parse_chord_token("G")
        voiced = voice_chord(chord, BASS)
        self.assertEqual(len(voiced), 1)
        s, f = voiced[0]
        midi = BASS.tuning[s - 1] + f
        self.assertEqual(midi % 12, 7)   # G


class LyricRescueTests(unittest.TestCase):
    def _part_with_direction(self, words):
        part = ET.Element("part", {"id": "P1"})
        measure = ET.SubElement(part, "measure", {"number": "1"})
        d = ET.SubElement(measure, "direction")
        dt = ET.SubElement(d, "direction-type")
        w = ET.SubElement(dt, "words")
        w.text = words
        note = ET.SubElement(measure, "note")
        pitch = ET.SubElement(note, "pitch")
        ET.SubElement(pitch, "step").text = "C"
        ET.SubElement(pitch, "octave").text = "4"
        ET.SubElement(note, "staff").text = "1"
        return part, measure, note

    def test_mutopia_footer_is_dropped_not_lyric(self):
        part, measure, note = self._part_with_direction(
            "Mutopia Project Typeset using LilyPond Creative Commons Attribution")
        rescue_lyric_directions(part)
        self.assertIsNone(measure.find("direction"))
        self.assertIsNone(note.find("lyric"))

    def test_real_lyric_line_is_rescued(self):
        part, measure, note = self._part_with_direction(
            "singing made up words for this test")
        rescue_lyric_directions(part)
        self.assertIsNone(measure.find("direction"))
        self.assertEqual(note.findtext("lyric/text"),
                         "singing made up words for this test")

    def test_footer_already_filed_as_lyric_is_dropped(self):
        part, measure, note = self._part_with_direction("keep me")
        lyr = ET.SubElement(note, "lyric")
        ET.SubElement(lyr, "syllabic").text = "single"
        ET.SubElement(lyr, "text").text = (
            "Mutopia Project Typeset using LilyPond Creative Commons")
        scrub_page_furniture(part)
        self.assertIsNone(note.find("lyric"))

    def test_cc_fragments_are_dropped(self):
        for frag in ("Attribution (Unported)", "perform.",
                     "Licensedunder the CreativeCommons",
                     "distribute, modify and"):
            part, measure, note = self._part_with_direction("x")
            lyr = ET.SubElement(note, "lyric")
            ET.SubElement(lyr, "text").text = frag
            scrub_page_furniture(part)
            self.assertIsNone(note.find("lyric"), frag)

    def _part_from_lyrics(self, measure_words):
        """One measure per entry; each entry is a list of lyric strings."""
        part = ET.Element("part", {"id": "P1"})
        for i, words in enumerate(measure_words, 1):
            measure = ET.SubElement(part, "measure", {"number": str(i)})
            note = ET.SubElement(measure, "note")
            pitch = ET.SubElement(note, "pitch")
            ET.SubElement(pitch, "step").text = "C"
            ET.SubElement(pitch, "octave").text = "4"
            for w in words:
                lyr = ET.SubElement(note, "lyric")
                ET.SubElement(lyr, "text").text = w
        return part

    def _lyric_words(self, part):
        return [t for m in part.findall("measure")
                for _n, _l, t in _lyric_texts(m)]

    def test_footer_split_across_measures_is_dropped(self):
        # Audiveris puts each Mutopia footer word on its own note/measure.
        part = self._part_from_lyrics([
            ["using"], ["under"], ["the"], ["Creative"], ["by"],
            ["Commons"], ["Louie"], ["Bommel."], ["van"],
            [],
            ["/06/08"],
        ])
        scrub_page_furniture(part)
        self.assertEqual(self._lyric_words(part), [])

    def test_modify_and_tail_and_date_drop_with_footer(self):
        part = self._part_from_lyrics([
            ["Project"],
            ["using", "Creative", "Commons"],
            ["ShareAIike", "Glen", "by"],
            ["Larsen", "International"],
            ["free", "to"],
            ["modify,", "and"],
            ["C", "D."],
        ])
        scrub_page_furniture(part)
        self.assertEqual(self._lyric_words(part), [])

    def test_real_lyrics_next_to_footer_are_kept(self):
        part = self._part_from_lyrics([
            ["singing", "made", "up", "words"],
            ["Creative", "Commons", "Attribution"],
        ])
        scrub_page_furniture(part)
        self.assertEqual(self._lyric_words(part),
                         ["singing", "made", "up", "words"])

    def test_dynamics_kept_when_footer_is_elsewhere(self):
        part = self._part_from_lyrics([
            ["mf"],
            [], [], [],
            ["p"],
            [],
            ["Creative", "using", "Commons", "Project"],
            ["Attribution", "by", "Glen"],
            [], [],
            ["ff"],
        ])
        scrub_page_furniture(part)
        self.assertEqual(self._lyric_words(part), ["mf", "p", "ff"])

    def test_editorial_errata_line_is_dropped(self):
        part = self._part_from_lyrics([
            ["see", "original"],
            ["correct", "errors", "to"],
            ["first", "line", "past"],
        ])
        scrub_page_furniture(part)
        self.assertEqual(self._lyric_words(part), [])

    def test_copyright_symbol_lyric_is_dropped(self):
        part = self._part_from_lyrics([["P"], ["\ufffd"], ["mf"]])
        scrub_page_furniture(part)
        self.assertEqual(self._lyric_words(part), ["P", "mf"])

    def test_pulgar_lyric_becomes_pluck(self):
        part = self._part_from_lyrics([["P ' ."], ["mf"], ["cresc."]])
        rescue_fingering_lyrics(part)
        self.assertEqual(self._lyric_words(part), ["mf", "cresc."])
        first = part.find("measure/note")
        self.assertEqual(first.findtext("notations/technical/pluck"), "p")

    def test_im_fingerings_kept_when_part_has_lyrics(self):
        part = self._part_from_lyrics([["Sing"], ["i"], ["made"]])
        rescue_fingering_lyrics(part)
        self.assertEqual(self._lyric_words(part), ["Sing", "i", "made"])

    def test_im_fingerings_moved_on_instrumental(self):
        part = self._part_from_lyrics([["i"], ["m"], ["P"]])
        rescue_fingering_lyrics(part)
        self.assertEqual(self._lyric_words(part), [])
        plucks = [n.findtext("notations/technical/pluck")
                  for n in part.iter("note")]
        self.assertEqual(plucks, ["i", "m", "p"])

    def test_licence_credit_is_dropped(self):
        root = ET.Element("score-partwise")
        ident = ET.SubElement(root, "identification")
        cr = ET.SubElement(ident, "creator", {"type": "composer"})
        cr.text = "Fernando Sor"
        junk = ET.SubElement(ident, "creator", {"type": "lyricist"})
        junk.text = "Creative Commons Attribution 3.0"
        rights = ET.SubElement(ident, "rights")
        rights.text = "Creative Commons Attribution 3.0"
        guitar = ET.SubElement(root, "credit")
        ET.SubElement(guitar, "credit-words").text = "Guitar"
        credit = ET.SubElement(root, "credit")
        ET.SubElement(credit, "credit-type").text = "rights"
        words = ET.SubElement(credit, "credit-words")
        words.text = "Creative Commons Attribution 3.0"
        num = ET.SubElement(root, "credit")
        ET.SubElement(num, "credit-words").text = "14"
        mark = ET.SubElement(root, "credit")
        ET.SubElement(mark, "credit-words").text = "\u00ae"
        title = ET.SubElement(root, "credit")
        ET.SubElement(title, "credit-type").text = "title"
        ET.SubElement(title, "credit-words").text = "Guitar-Skole no. 2"
        tree = ET.ElementTree(root)
        scrub_score_furniture(tree)
        credits = [" ".join((w.text or "") for w in c.iter("credit-words"))
                   for c in root.findall("credit")]
        self.assertEqual(credits, ["Guitar-Skole no. 2"])
        creators = [(c.get("type"), c.text) for c in ident.findall("creator")]
        self.assertEqual(creators, [("composer", "Fernando Sor")])
        self.assertIsNone(ident.find("rights"))

    def test_tempo_credit_already_in_title_is_dropped(self):
        root = ET.Element("score-partwise")
        ET.SubElement(root, "movement-title").text = (
            "Guitar-Skole no. 15 : Allegretto")
        tempo = ET.SubElement(root, "credit")
        ET.SubElement(tempo, "credit-words").text = "Allegretto"
        title = ET.SubElement(root, "credit")
        ET.SubElement(title, "credit-words").text = (
            "Guitar-Skole no. 15 : Allegretto")
        lento = ET.SubElement(root, "credit")
        ET.SubElement(lento, "credit-words").text = "Lento"
        composer = ET.SubElement(root, "credit")
        ET.SubElement(composer, "credit-words").text = "Matteo Carcassi"
        scrub_score_furniture(ET.ElementTree(root))
        credits = [" ".join((w.text or "") for w in c.iter("credit-words"))
                   for c in root.findall("credit")]
        self.assertEqual(credits, [
            "Guitar-Skole no. 15 : Allegretto",
            "Lento",
            "Matteo Carcassi",
        ])
