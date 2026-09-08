"""Parser tests against the fixtures in this folder. No MuseScore needed."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tab_model import INSTRUMENTS, TabError, parse_tuning
from tab_read import (
    guess_title, parse_ascii_tab, parse_chord_chart, parse_chord_token,
    pdf_text_mode, sniff_text_mode,
)

HERE = Path(__file__).resolve().parent
GUITAR = INSTRUMENTS["guitar"]
UKE = INSTRUMENTS["ukulele"]
BASS = INSTRUMENTS["bass"]


def _read(name):
    return (HERE / name).read_text(encoding="utf-8")


def _notes(events):
    """Flat (string, fret, frozenset marks) in time order."""
    out = []
    for _dur, notes in events:
        for entry in notes:
            marks = frozenset(entry[2]) if len(entry) > 2 else frozenset()
            out.append((entry[0], entry[1], marks))
    return out


class ChordTokenTests(unittest.TestCase):
    def test_plain_and_slash(self):
        c = parse_chord_token("Am7")
        self.assertIsNotNone(c)
        self.assertEqual(c.root_step, "A")
        self.assertEqual(c.kind, "minor-seventh")
        slash = parse_chord_token("C/B")
        self.assertIsNotNone(slash)
        self.assertEqual(slash.bass_step, "B")

    def test_words_are_not_chords(self):
        for word in ("Bass", "Add", "Capo", "Chords", "Intro", "The"):
            self.assertIsNone(parse_chord_token(word), word)


class SniffAndTitleTests(unittest.TestCase):
    def test_ug_paste_is_tab(self):
        text = _read("ug_style_paste.txt")
        self.assertEqual(sniff_text_mode(text), "tab")
        self.assertIn("Song Test", guess_title(text))

    def test_chart_with_embedded_riff_is_chart(self):
        text = _read("chart_test.txt")
        self.assertEqual(sniff_text_mode(text), "chart")
        self.assertEqual(pdf_text_mode(text), "chart")
        self.assertEqual(guess_title(text), "So Fake A Song")

    def test_pdf_text_mode_ignores_score_leftovers(self):
        self.assertIsNone(pdf_text_mode(
            "Guitar-Skole no. 2 Andante Fernando Sor typeset using LilyPond"))

    def test_happy_birthday_title(self):
        text = _read("Happy Birthday Tab by Misc Traditional.txt")
        self.assertEqual(sniff_text_mode(text), "tab")
        self.assertTrue(guess_title(text).lower().startswith("happy birthday"))


class AsciiTabTests(unittest.TestCase):
    def test_guitar_riff_hammer(self):
        events = parse_ascii_tab(_read("guitar_riff.txt"), GUITAR)
        notes = _notes(events)
        self.assertGreaterEqual(len(notes), 6)
        hammers = [n for n in notes if "hammer_start" in n[2]]
        stops = [n for n in notes if "hammer_stop" in n[2]]
        self.assertEqual(len(hammers), 1)
        self.assertEqual(len(stops), 1)
        self.assertEqual(hammers[0][1], 0)
        self.assertEqual(stops[0][1], 1)

    def test_ug_clutter_and_techniques(self):
        events = parse_ascii_tab(_read("ug_style_paste.txt"), GUITAR)
        notes = _notes(events)
        frets = [(s, f) for s, f, _m in notes]
        self.assertIn((2, 1), frets)          # B string 1h3
        self.assertIn((2, 3), frets)
        self.assertIn((1, 12), frets)         # 12/14 slide
        self.assertIn((1, 14), frets)
        self.assertIn((2, 13), frets)         # 13b15 is one picked note
        # the 15 is the bend target, not a second pick
        self.assertNotIn((2, 15), frets)
        marks = {m for _s, _f, ms in notes for m in ms}
        self.assertTrue(any(m.startswith("bend:") for m in marks))
        self.assertIn("hammer_start", marks)
        self.assertIn("slide_start", marks)
        self.assertIn("vibrato", marks)

    def test_ghost_note_skipped(self):
        events = parse_ascii_tab(
            _read("Happy Birthday Tab by Misc Traditional.txt"), GUITAR)
        notes = _notes(events)
        # D string has two real 4s and one optional "(4)" -- only the real ones
        d_fours = [n for n in notes if n[0] == 4 and n[1] == 4]
        self.assertEqual(len(d_fours), 2)

    def test_bend_release_and_pinch(self):
        events = parse_ascii_tab(_read("bend_release.txt"),
                                 GUITAR)
        notes = _notes(events)
        marks = {m for _s, _f, ms in notes for m in ms}
        self.assertTrue(any(m.startswith("bend:") for m in marks))
        self.assertTrue(any(m.startswith("release:") for m in marks))
        self.assertIn("pinch", marks)
        self.assertIn("pull_start", marks)
        self.assertIn("slide_start", marks)

    def test_ukulele_four_strings(self):
        events = parse_ascii_tab(_read("uke_song.txt"), UKE)
        notes = _notes(events)
        self.assertTrue(notes)
        self.assertTrue(all(1 <= s <= 4 for s, _f, _m in notes))

    def test_bass_walk(self):
        events = parse_ascii_tab(_read("bass_walk.txt"), BASS)
        notes = _notes(events)
        self.assertGreaterEqual(len(notes), 5)
        self.assertTrue(all(1 <= s <= 4 for s, _f, _m in notes))

    def test_wrong_instrument_raises(self):
        with self.assertRaises(TabError) as ctx:
            parse_ascii_tab(_read("guitar_riff.txt"), UKE)
        self.assertIn("4", str(ctx.exception))
        self.assertIn("6", str(ctx.exception))

    def test_bass_tab_on_guitar_pads_two_blank_strings(self):
        events = parse_ascii_tab(_read("bass_walk.txt"), GUITAR)
        notes = _notes(events)
        self.assertTrue(notes)
        self.assertTrue(all(3 <= s <= 6 for s, _f, _m in notes))
        self.assertTrue(any(s == 5 for s, _f, _m in notes))  # bass A -> guitar A
        self.assertFalse(any(s <= 2 for s, _f, _m in notes))

    def test_empty_raises(self):
        with self.assertRaises(TabError):
            parse_ascii_tab("just some words\nand no tab", GUITAR)


class ChordChartTests(unittest.TestCase):
    def test_chart_test_fixture(self):
        items = parse_chord_chart(_read("chart_test.txt"))
        displays = [c.display for _sec, c, _lyr in items]
        self.assertIn("C", displays)
        self.assertIn("C/B", displays)
        self.assertIn("Am7", displays)
        self.assertIn("G7", displays)
        sections = [sec for sec, _c, _lyr in items if sec]
        self.assertIn("Intro", sections)
        self.assertIn("Chorus", sections)
        lyrics = [lyr for _s, _c, lyr in items if lyr]
        self.assertTrue(any("Made up words" in (lyr or "") for lyr in lyrics))
        # the riff in the chorus is skipped, not parsed as chords
        self.assertEqual(len(items), 18)


class TuningTests(unittest.TestCase):
    def test_guitar_tuning_low_string_first(self):
        midi = parse_tuning("E2 A2 D3 G3 B3 E4")
        self.assertEqual(midi, GUITAR.tuning)


class EngineApiTests(unittest.TestCase):
    def test_first_run_deps_are_musescore_only(self):
        import sheet2tab
        core = sheet2tab.check_dependencies()
        self.assertEqual(list(core), ["MuseScore (turns music into PDFs)"])
        omr = sheet2tab.check_dependencies(omr=True)
        self.assertEqual(len(omr), 3)
        self.assertTrue(any("Audiveris" in k for k in omr))

    def test_gui_reexports(self):
        import sheet2tab as engine
        for name in ("INSTRUMENTS", "SETTINGS_PATH", "SHARP_SPELLING",
                     "instrument_from_custom", "load_settings",
                     "apply_settings", "sniff_text_mode", "guess_title",
                     "check_dependencies", "TabError"):
            self.assertTrue(hasattr(engine, name), name)


def _have_pdfplumber():
    try:
        import pdfplumber  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_have_pdfplumber(), "pdfplumber not installed")
class EngravedPdfTests(unittest.TestCase):
    def test_musescore_tab_no_false_vibrato_or_eaten_rest(self):
        from tab_read import Barline, pdf_tab_events
        path = HERE / "external" / "musescore_engraved_tab.pdf"
        if not path.exists():
            path = HERE / "out" / "guitar_riff_TAB.pdf"
        self.assertTrue(path.exists(), path)
        result = pdf_tab_events(path, GUITAR)
        self.assertIsNotNone(result)
        events, absolute, _meta = result
        self.assertTrue(absolute)
        marks = [m for _u, notes in events
                 for n in notes
                 for m in (n[2] if len(n) > 2 else ())]
        self.assertNotIn("vibrato", marks)
        # bar 2 is an eighth 0 plus a rest, not a whole note
        bars = []
        cur = []
        for u, notes in events:
            if isinstance(notes, Barline):
                bars.append(cur)
                cur = []
            else:
                cur.append((u, notes))
        self.assertGreaterEqual(len(bars), 2)
        b2 = bars[1]
        self.assertTrue(any(u < 16 and notes for u, notes in b2))
        self.assertTrue(any(not notes for u, notes in b2))

    def test_chart_pdf_routes_as_chart(self):
        from tab_read import extract_pdf_text, pdf_text_mode
        path = HERE / "external" / "ug_style_chord_chart.pdf"
        if not path.exists():
            self.skipTest("external chord-chart PDF not present")
        text = extract_pdf_text(path)
        self.assertTrue(text)
        self.assertEqual(pdf_text_mode(text), "chart")


def _have_numpy_pil():
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_have_numpy_pil(), "numpy/PIL not installed")
class RasterTabTests(unittest.TestCase):
    def _draw_bass_tab(self, path):
        from PIL import Image, ImageDraw, ImageFont
        im = Image.new("L", (700, 220), 225)
        d = ImageDraw.Draw(im)
        y0, g = 80, 18
        for i in range(4):
            y = y0 + i * g
            d.line((40, y, 660, y), fill=30, width=1)
        for x in (40, 200, 360, 520, 660):
            d.line((x, y0, x, y0 + 3 * g), fill=30, width=2)
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 14)
        except OSError:
            font = ImageFont.load_default()
        # E-string (bottom) open zeros in bar 1
        d.text((70, y0 + 3 * g - 8), "0", fill=0, font=font)
        d.text((110, y0 + 3 * g - 8), "0", fill=0, font=font)
        d.text((150, y0 + 3 * g - 8), "3", fill=0, font=font)
        d.text((230, y0 + 2 * g - 8), "2", fill=0, font=font)
        d.text((270, y0 + 2 * g - 8), "2", fill=0, font=font)
        d.text((390, y0 - 8), "0", fill=0, font=font)
        d.text((430, y0 - 8), "0", fill=0, font=font)
        d.text((550, y0 + 3 * g - 8), "1", fill=0, font=font)
        d.text((590, y0 + 3 * g - 8), "1", fill=0, font=font)
        im.save(path)

    def test_synthetic_four_string_tab(self):
        from tab_read import Barline, raster_tab_events
        path = HERE / "out" / "_synth_bass_tab.png"
        path.parent.mkdir(exist_ok=True)
        self._draw_bass_tab(path)
        result = raster_tab_events(path, BASS)
        self.assertIsNotNone(result)
        events, _abs, _meta = result
        notes = [(s, f) for _u, nn in events
                 if isinstance(nn, list) and type(nn) is list
                 for s, f, *_r in nn]
        self.assertGreaterEqual(len(notes), 6)
        self.assertTrue(all(1 <= s <= 4 for s, _f in notes))
        self.assertTrue(any(f == 0 for _s, f in notes))
        self.assertTrue(any(f == 3 for _s, f in notes))
        bars = sum(1 for _u, nn in events if isinstance(nn, Barline))
        self.assertGreaterEqual(bars, 2)

    def test_four_string_image_on_guitar_pads_top_strings(self):
        from tab_read import raster_tab_events
        path = HERE / "out" / "_synth_bass_tab.png"
        if not path.exists():
            self._draw_bass_tab(path)
        result = raster_tab_events(path, GUITAR)
        self.assertIsNotNone(result)
        events, _abs, _meta = result
        notes = [(s, f) for _u, nn in events
                 if isinstance(nn, list) and type(nn) is list
                 for s, f, *_r in nn]
        self.assertTrue(notes)
        self.assertTrue(all(3 <= s <= 6 for s, _f in notes))
        self.assertFalse(any(s <= 2 for s, _f in notes))

    def test_classify_rendered_digits(self):
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
        from tab_read import classify_tab_digit
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\times.ttf", 18)
        except OSError:
            self.skipTest("Times font not installed")
        for d in range(10):
            im = Image.new("L", (40, 36), 0)
            ImageDraw.Draw(im).text((4, 0), str(d), fill=255, font=font)
            got = classify_tab_digit(np.asarray(im) > 128)
            self.assertEqual(got, d, f"rendered {d} read as {got}")

    def test_zero_on_staff_line_stays_zero(self):
        """Anti-aliased 0s sitting on a staff line used to split into 1 1."""
        from PIL import Image, ImageDraw, ImageFont
        from tab_read import raster_tab_events
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\times.ttf", 16)
        except OSError:
            self.skipTest("Times font not installed")
        path = HERE / "out" / "_synth_zeros_tab.png"
        path.parent.mkdir(exist_ok=True)
        im = Image.new("L", (520, 200), 225)
        d = ImageDraw.Draw(im)
        y0, g = 70, 20
        for i in range(4):
            d.line((40, y0 + i * g, 490, y0 + i * g), fill=40, width=1)
        for x in (40, 160, 280, 400, 490):
            d.line((x, y0, x, y0 + 3 * g), fill=40, width=2)
        for x in (70, 110, 190, 230, 310, 350, 430, 470):
            d.text((x, y0 - 9), "0", fill=20, font=font)
        im.save(path)
        result = raster_tab_events(path, BASS)
        self.assertIsNotNone(result)
        notes = [(s, f) for _u, nn in result[0]
                 if isinstance(nn, list)
                 for s, f, *_r in nn]
        zeros = [n for n in notes if n[1] == 0]
        ones = [n for n in notes if n[1] == 1]
        self.assertGreaterEqual(len(zeros), 6)
        self.assertGreaterEqual(len(zeros), len(ones))

    def test_tab_clef_is_not_notes(self):
        from PIL import Image, ImageDraw, ImageFont
        from tab_read import raster_tab_events
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\times.ttf", 16)
            big = ImageFont.truetype(r"C:\Windows\Fonts\timesbd.ttf", 18)
        except OSError:
            self.skipTest("Times font not installed")
        path = HERE / "out" / "_synth_tab_clef.png"
        im = Image.new("L", (560, 210), 225)
        d = ImageDraw.Draw(im)
        y0, g = 70, 22
        for i in range(4):
            d.line((30, y0 + i * g, 530, y0 + i * g), fill=40, width=1)
        d.text((36, y0 - 8), "T", fill=10, font=big)
        d.text((36, y0 + g - 8), "A", fill=10, font=big)
        d.text((36, y0 + 2 * g - 8), "B", fill=10, font=big)
        d.line((78, y0, 78, y0 + 3 * g), fill=30, width=2)
        for x in (100, 220, 340):
            d.text((x, y0 + 2 * g - 9), "3", fill=10, font=font)
            d.text((x + 40, y0 - 9), "0", fill=10, font=font)
            d.text((x + 70, y0 - 9), "0", fill=10, font=font)
            d.text((x + 110, y0 + g - 9), "4", fill=10, font=font)
        d.line((530, y0, 530, y0 + 3 * g), fill=30, width=2)
        im.save(path)
        result = raster_tab_events(path, BASS)
        self.assertIsNotNone(result)
        notes = [(s, f) for _u, nn in result[0]
                 if isinstance(nn, list)
                 for s, f, *_r in nn]
        self.assertTrue(any(f == 0 for _s, f in notes))
        self.assertTrue(any(f == 3 for _s, f in notes))
        self.assertTrue(any(f == 4 for _s, f in notes))
        # the stacked TAB letters must not become a 4-string opening chord
        events = [nn for _u, nn in result[0] if isinstance(nn, list) and nn]
        if events:
            self.assertLess(len(events[0]), 4)

    def test_tank_pages_read_the_riff(self):
        from tab_read import engraved_tab_pages
        folder = HERE / "external" / "tank"
        pages = [folder / f"Tank p{i}.jpg" for i in (1, 2, 3)]
        if not all(p.exists() for p in pages):
            self.skipTest("Tank page screenshots not present")
        result = engraved_tab_pages(pages, GUITAR)
        self.assertIsNotNone(result)
        events, _abs, _meta = result
        notes = [(s, f) for _u, nn in events
                 if isinstance(nn, list)
                 for s, f, *_r in nn]
        self.assertGreaterEqual(len(notes), 400)
        zeros = sum(1 for _s, f in notes if f == 0)
        elevens = sum(1 for _s, f in notes if f == 11)
        self.assertGreaterEqual(zeros, 80)
        self.assertEqual(elevens, 0)
        # bass riff on guitar strings 3-6: A3, G0, G0, D4, D3, D1, D1, A1
        riff = [(5, 3), (3, 0), (3, 0), (4, 4), (4, 3), (4, 1), (4, 1), (5, 1)]
        hits = sum(1 for i in range(len(notes) - 7)
                   if notes[i:i + 8] == riff)
        self.assertGreaterEqual(hits, 8)

    def test_svg_is_a_page_image(self):
        from tab_model import IMAGE_EXTS
        from svg_page import svg_to_pil
        from tab_read import raster_tab_events
        self.assertIn(".svg", IMAGE_EXTS)
        path = HERE / "external" / "musescore_riff.svg"
        if not path.exists():
            path = HERE / "out" / "_riff-1.svg"
        if not path.exists():
            self.skipTest("MuseScore riff SVG not present")
        im = svg_to_pil(path)
        self.assertGreaterEqual(min(im.size), 400)
        result = raster_tab_events(path, GUITAR)
        self.assertIsNotNone(result)
        notes = [(s, f) for _u, nn in result[0]
                 if isinstance(nn, list)
                 for s, f, *_r in nn]
        self.assertGreaterEqual(len(notes), 5)
        frets = {f for _s, f in notes}
        self.assertTrue(frets & {0, 1, 2, 3})

    def test_simple_svg_staff_renders(self):
        from svg_page import svg_to_pil
        path = HERE / "out" / "_synth_staff.svg"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 120"
     width="120mm" height="36mm">
  <polyline fill="none" stroke="#000" stroke-width="1"
            points="20,20 380,20"/>
  <polyline fill="none" stroke="#000" stroke-width="1"
            points="20,40 380,40"/>
  <polyline fill="none" stroke="#000" stroke-width="1"
            points="20,60 380,60"/>
  <polyline fill="none" stroke="#000" stroke-width="1"
            points="20,80 380,80"/>
  <line x1="20" y1="20" x2="20" y2="80" stroke="#000" stroke-width="2"/>
  <line x1="380" y1="20" x2="380" y2="80" stroke="#000" stroke-width="2"/>
</svg>
""",
            encoding="utf-8")
        im = svg_to_pil(path, target=800)
        self.assertGreater(im.size[0], 200)
        # four staff lines should leave a strip of dark pixels
        pix = im.convert("L").tobytes()
        self.assertTrue(any(b < 40 for b in pix))

    def test_svg_arc_then_moveto_does_not_crash(self):
        """An A command's 7 numbers must not swallow the next command letter."""
        from svg_page import svg_to_pil
        path = HERE / "out" / "_synth_arc.svg"
        path.write_text(
            """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80">
  <path fill="#000" d="M10,40 A20,20 0 0 1 50,40A20,20 0 0 1 90,40"/>
  <path fill="none" stroke="#000" stroke-width="2"
        d="M10,10 A15,15 0 0 1 40,10 L90,10"/>
</svg>
""",
            encoding="utf-8")
        im = svg_to_pil(path, target=400)
        self.assertGreaterEqual(min(im.size), 50)
