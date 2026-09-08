#!/usr/bin/env python3
"""
sheet2tab -- add a TAB staff to sheet music, or turn typed TAB into a score.

Pipeline: tab_read (ASCII / chart / engraved PDF) or Audiveris (scans) ->
tab_write (MusicXML with a TAB staff) -> MuseScore (engraving).

This file is the CLI, helper-app setup, OMR glue, and MuseScore render.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from tab_model import (
    IMAGE_EXTS, TAB_TEXT_EXTS, XML_EXTS, TabError,
    apply_settings, load_settings, with_capo,
)
from tab_read import (
    engraved_tab_pages, extract_pdf_text, open_page_image, parse_ascii_tab,
    pdf_tab_events, pdf_text_mode, sniff_text_mode,
)
from tab_write import add_tab_staff, chart_to_tree, events_to_tree

# Re-exports so `import sheet2tab as engine` (the GUI) keeps working.
from tab_model import (  # noqa: F401
    INSTRUMENTS, SETTINGS_PATH, SHARP_SPELLING, Instrument,
    instrument_from_custom,
)
from tab_read import guess_title  # noqa: F401

AUDIVERIS_CANDIDATES = [
    Path(r"C:\Program Files\Audiveris\Audiveris.exe"),
    Path(r"C:\Program Files\Audiveris\bin\Audiveris.exe"),
]

MUSESCORE_CANDIDATES = [
    Path(r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe"),
    Path(r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe"),
]

def find_exe(candidates, name):
    for p in candidates:
        if p.exists():
            return p
    found = shutil.which(name)
    if found:
        return Path(found)
    return None

# ------------------------------------------------------- helper app setup

TESSDATA_URL = ("https://raw.githubusercontent.com/tesseract-ocr/"
                "tessdata/main/eng.traineddata")

def tessdata_file():
    return (Path(os.environ.get("APPDATA", str(Path.home())))
            / "AudiverisLtd" / "audiveris" / "config" / "tessdata"
            / "eng.traineddata")

def check_dependencies(omr=False):
    """What TABifier needs besides itself; True = present.

    MuseScore is required to print anything. Audiveris and the OCR data are
    only needed when reading a scan of sheet music -- typed TAB and chord
    charts do not use them.
    """
    deps = {
        "MuseScore (turns music into PDFs)":
            find_exe(MUSESCORE_CANDIDATES, "MuseScore4") is not None,
    }
    if omr:
        deps["Audiveris (reads scanned sheet music)"] = (
            find_exe(AUDIVERIS_CANDIDATES, "Audiveris") is not None)
        deps["OCR language data (reads lyrics and text)"] = (
            tessdata_file().exists())
    return deps

def _winget_install(pkg_id, name):
    print(f"Installing {name} with winget - a Windows permission prompt "
          "may appear, please allow it...", flush=True)
    try:
        proc = subprocess.run(
            ["winget", "install", "--id", pkg_id, "--accept-package-agreements",
             "--accept-source-agreements", "--silent", "--disable-interactivity"],
            capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        print(f"  winget is not available on this PC; install {name} manually "
              "(links in the README)")
        return False
    except subprocess.TimeoutExpired:
        print(f"  {name} installation timed out")
        return False
    if proc.returncode == 0:
        print(f"  {name} installed.")
        return True
    tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-3:])
    print(f"  {name} install failed (exit {proc.returncode}):\n{tail}")
    return False

def install_dependencies(omr=False):
    """Install whatever check_dependencies reports missing. Returns True
    when everything is present afterwards.

    By default only MuseScore is installed. Pass omr=True (or --setup --omr
    on the command line) for the scan reader as well.
    """
    deps = check_dependencies(omr=omr)
    if all(deps.values()):
        print("All helper apps are already installed.")
        return True
    if not deps["MuseScore (turns music into PDFs)"]:
        _winget_install("Musescore.Musescore", "MuseScore")
    if omr:
        omr_deps = check_dependencies(omr=True)
        if not omr_deps["Audiveris (reads scanned sheet music)"]:
            _winget_install("audiveris.org.Audiveris", "Audiveris")
        if (find_exe(AUDIVERIS_CANDIDATES, "Audiveris") is not None
                and not tessdata_file().exists()):
            print("Downloading OCR language data (about 23 MB)...", flush=True)
            target = tessdata_file()
            part = target.with_suffix(".part")
            try:
                import urllib.request
                target.parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(TESSDATA_URL, part)
                part.replace(target)
                print("  OCR language data installed.")
            except OSError as e:
                print(f"  could not download OCR data: {e}")
                if part.exists():
                    part.unlink()
    deps = check_dependencies(omr=omr)
    for name, ok in deps.items():
        print(("  OK      " if ok else "  MISSING ") + name)
    return all(deps.values())

# ---------------------------------------------------------------- stage 1: OMR

def rasterize_pdf(path: Path, workdir: Path, dpi=300):
    """Render each PDF page to a PNG. Empty list if the renderer is missing."""
    pngs = []
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        scale = dpi / 72.0
        for i, page in enumerate(doc):
            pil = page.render(scale=scale).to_pil()
            out = workdir / f"raster_{path.stem}_{i + 1}.png"
            pil.save(out)
            pngs.append(out)
        return pngs
    except Exception:
        pass
    try:
        import pdfplumber
    except ImportError:
        return []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                im = page.to_image(resolution=dpi).original
                out = workdir / f"raster_{path.stem}_{i + 1}.png"
                im.save(out)
                pngs.append(out)
    except Exception:
        return []
    return pngs


def run_audiveris(input_path: Path, workdir: Path, *, raster_retry=True) -> Path:
    """Run Audiveris on a PDF/image, return path to the exported .mxl."""
    exe = find_exe(AUDIVERIS_CANDIDATES, "Audiveris")
    if exe is None:
        raise TabError(
            "reading a scan needs Audiveris (the free sheet-music reader). "
            "TABifier will offer to install it, or run: "
            "python sheet2tab.py --setup --omr")
    outdir = workdir / "omr"
    outdir.mkdir(parents=True, exist_ok=True)
    if not tessdata_file().exists():
        print("  note: OCR language data is missing, so lyrics and text will "
              "be skipped - run TABifier setup to fix this")
    cmd = [str(exe), "-batch", "-export", "-output", str(outdir), str(input_path)]
    print("  running Audiveris (this takes ~30-90s per page)...")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    mxls = sorted(outdir.rglob("*.mxl"))
    if not mxls and raster_retry and input_path.suffix.lower() == ".pdf":
        print("  Audiveris exported nothing; rasterising the PDF and retrying...")
        pngs = rasterize_pdf(input_path, workdir)
        if pngs:
            retry_dir = workdir / "omr_raster"
            retry_dir.mkdir(parents=True, exist_ok=True)
            if len(pngs) == 1:
                source = preprocess_image(pngs[0], retry_dir)
            else:
                source = combine_images_to_pdf(pngs, retry_dir)
            return run_audiveris(source, retry_dir, raster_retry=False)
    if not mxls:
        tail = "\n".join((proc.stdout or "").splitlines()[-15:])
        raise TabError(
            f"Audiveris produced no MusicXML.\nExit code {proc.returncode}."
            f"\nLast output:\n{tail}")
    if len(mxls) > 1:
        print(f"  note: Audiveris found {len(mxls)} movements; using {mxls[0].name}")
    return mxls[0]

def preprocess_image(path: Path, workdir: Path, index: int = 0):
    """Flatten transparency and upscale small scans for better OCR.

    Screenshots and app exports are often ~150 dpi with an alpha channel;
    Tesseract wants ~300 dpi on white. Returns the original path if Pillow
    is unavailable or the image needs no work.
    """
    try:
        from PIL import Image
    except ImportError:
        return path
    im = open_page_image(path)
    changed = path.suffix.lower() == ".svg"
    # SVG must become a PNG so Audiveris and the rest see pixels, not XML.
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
        changed = True
    elif im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
        changed = True
    if max(im.size) < 2400:
        im = im.resize((im.size[0] * 2, im.size[1] * 2), Image.LANCZOS)
        print(f"  upscaled {path.name} 2x for sharper text recognition")
        changed = True
    if not changed:
        return path
    out = workdir / f"prep_{index}_{path.stem}.png"
    im.save(out)
    return out

def _images_for_omr(paths, workdir: Path) -> Path:
    """Stack page images as a TIFF. JPEG-in-PDF is what made Audiveris
    die with 'Error in export' on screenshot TAB pages."""
    try:
        from PIL import Image
    except ImportError:
        return combine_images_to_pdf(paths, workdir)
    pages = []
    for p in paths:
        im = open_page_image(p)
        if im.mode != "RGB":
            im = im.convert("RGB")
        if max(im.size) < 2400:
            im = im.resize((im.size[0] * 2, im.size[1] * 2), Image.LANCZOS)
        pages.append(im)
    out = workdir / "combined.tif"
    pages[0].save(out, save_all=True, append_images=pages[1:],
                  format="TIFF", compression="tiff_deflate")
    print(f"  combined {len(pages)} page images into one TIFF")
    return out


def combine_images_to_pdf(paths, workdir: Path) -> Path:
    """Stack several page images into one PDF so Audiveris reads them as a
    single piece, in the given order."""
    try:
        from PIL import Image
    except ImportError:
        raise TabError("combining multiple images needs the Pillow library.\n"
                       "Install it with:  pip install pillow")
    Image.init()  # register every codec; the PDF writer embeds pages as JPEG
    pages = []
    upscaled = 0
    for p in paths:
        im = open_page_image(p)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if max(im.size) < 2400:
            im = im.resize((im.size[0] * 2, im.size[1] * 2), Image.LANCZOS)
            upscaled += 1
        pages.append(im)
    if upscaled:
        print(f"  upscaled {upscaled} page(s) 2x for sharper text recognition")
    out = workdir / "combined.pdf"
    pages[0].save(out, save_all=True, append_images=pages[1:], resolution=300.0)
    print(f"  combined {len(pages)} page images into one PDF")
    return out

def extract_musicxml(mxl_path: Path, workdir: Path) -> Path:
    """Unpack a compressed .mxl into a plain .xml file."""
    with zipfile.ZipFile(mxl_path) as z:
        root_name = None
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rf = container.find(".//rootfile")
            if rf is not None:
                root_name = rf.get("full-path")
        except KeyError:
            pass
        if root_name is None:
            for n in z.namelist():
                if n.lower().endswith((".xml", ".musicxml")) and not n.startswith("META-INF"):
                    root_name = n
                    break
        if root_name is None:
            raise TabError(f"no MusicXML found inside {mxl_path.name}")
        out = workdir / "score.xml"
        out.write_bytes(z.read(root_name))
        return out

# ------------------------------------------------------- stage 5: rendering

# Injected into the .mscx <Style> block for --compact output. The MuseScore 4
# CLI silently ignores its -S style flag, so the style is baked into the
# score file instead. Spatium shrinks the staves; the rest tightens gaps.
COMPACT_STYLE = (
    "<Spatium>1.4</Spatium>"
    "<staffDistance>5</staffDistance>"
    "<akkoladeDistance>5</akkoladeDistance>"
    "<minSystemDistance>6</minSystemDistance>"
    "<maxSystemDistance>10</maxSystemDistance>"
    "<pageOddTopMargin>0.35</pageOddTopMargin>"
    "<pageOddBottomMargin>0.35</pageOddBottomMargin>"
    "<pageEvenTopMargin>0.35</pageEvenTopMargin>"
    "<pageEvenBottomMargin>0.35</pageEvenBottomMargin>"
    "<lyricsMinBottomDistance>1.5</lyricsMinBottomDistance>"
)

# MusicXML has no vibrato element at all: its only wavy line lives under
# <ornaments>, which readers import as a TRILL (drawn "tr~~~", telling the
# player to alternate two notes -- wrong). MuseScore does have a real
# guitar vibrato, but only in its own format, so the squiggles are swapped
# in here, after conversion.
VIBRATO_ELEMENT = "<Vibrato><subtype>guitarVibrato</subtype></Vibrato>"

# MusicXML's <hammer-on>/<pull-off> are dropped outright on import, leaving
# a bare slur with no letter. MuseScore's own HammerOnPullOff spanner draws
# the H/P (picking the letter from the pitch direction), so the slurs we
# created from hammer/pull arcs get promoted to it here.
HOPO_ELEMENT = "<HammerOnPullOff></HammerOnPullOff>"

# Source tabs draw a hammer-on/pull-off as a small mark tucked between the
# two fret numbers, while a tie sweeps the whole gap -- that difference is
# how a reader tells them apart. MuseScore draws every slur full width, so
# the endpoints are pulled inwards (spatium units: start, two bezier
# handles, end) to get the short tucked arc back. Ties are a separate
# element and keep their full sweep.
SLUR_TUCK = ('<SlurSegment no="0"><o1 x="0.6" y="0"/><o2 x="0.35" y="0"/>'
             '<o3 x="-0.35" y="0"/><o4 x="-0.6" y="0"/></SlurSegment>')

# Keeping the source's bars-per-line needs the room to do it: at the
# default staff size a busy line overflows and MuseScore wraps it anyway,
# which breaks the very layout we are copying. A slightly smaller staff
# plus narrower page margins buys back the width.
LAYOUT_FIT_STYLE = (
    "<Spatium>1.6</Spatium>"
    "<pagePrintableWidth>7.8763</pagePrintableWidth>"
    "<pageOddLeftMargin>0.19685</pageOddLeftMargin>"
    "<pageEvenLeftMargin>0.19685</pageEvenLeftMargin>"
)

def postprocess_mscx(tabbed_xml: Path, workdir: Path, compact=False,
                     vibrato=False, hopo=False, asfound=False,
                     fit_lines=False) -> Path:
    """Convert to MuseScore's own format and apply the finishing touches
    that MusicXML cannot express. Returns the file to render from."""
    mscx = workdir / "styled.mscx"
    musescore_convert(tabbed_xml, mscx)
    text = mscx.read_text(encoding="utf-8")
    changed = False
    if fit_lines and not compact:
        if "<Style>" in text:
            text = text.replace("<Style>", "<Style>" + LAYOUT_FIT_STYLE, 1)
        else:
            text = text.replace(
                "<Score>", "<Score><Style>" + LAYOUT_FIT_STYLE + "</Style>", 1)
        print("  sized to keep the original's bars on each line")
        changed = True
    if compact:
        if "<Style>" in text:
            text = text.replace("<Style>", "<Style>" + COMPACT_STYLE, 1)
            changed = True
        elif "<Score>" in text:
            text = text.replace("<Score>",
                                "<Score><Style>" + COMPACT_STYLE + "</Style>",
                                1)
            changed = True
        else:
            print("  note: could not apply compact style, using normal layout")
    if vibrato and '<Spanner type="Trill">' in text:
        text = text.replace('<Spanner type="Trill">',
                            '<Spanner type="Vibrato">')
        text = re.sub(r"<Trill>.*?</Trill>", VIBRATO_ELEMENT, text,
                      flags=re.DOTALL)
        print("  vibrato drawn as squiggly lines")
        changed = True
    if hopo and '<Spanner type="Slur">' in text:
        text = text.replace('<Spanner type="Slur">',
                            '<Spanner type="HammerOnPullOff">')
        text = re.sub(r"<Slur>.*?</Slur>", HOPO_ELEMENT, text, flags=re.DOTALL)
        print("  hammer-ons and pull-offs marked H / P")
        changed = True
    if asfound:
        # Match how the source lays a tab out: note-value stems and beams
        # above the staff, vibrato squiggles and hammer-on/pull-off curves
        # below it. MuseScore's defaults are the other way round.
        notes = []
        if "<stemsDown>1</stemsDown>" in text:
            text = text.replace("<stemsDown>1</stemsDown>",
                                "<stemsDown>0</stemsDown>")
            notes.append("note values above the staff")
        if "<subtype>guitarVibrato</subtype>" in text:
            text = text.replace(
                "<subtype>guitarVibrato</subtype>",
                "<subtype>guitarVibrato</subtype>"
                "<placement>below</placement>")
            notes.append("vibrato below")
        if "<Slur>" in text:
            text = text.replace("<Slur>", "<Slur><up>2</up>" + SLUR_TUCK)
            notes.append("hammer-on marks tucked between the frets")
        if notes:
            print("  laid out as the original: " + ", ".join(notes))
            changed = True
    if not changed:
        return tabbed_xml
    mscx.write_text(text, encoding="utf-8")
    return mscx

def musescore_convert(src: Path, dst: Path):
    exe = find_exe(MUSESCORE_CANDIDATES, "MuseScore4")
    if exe is None:
        raise TabError("MuseScore not found. Install it from musescore.org "
                       "or: winget install Musescore.Musescore")
    proc = subprocess.run([str(exe), "-f", "-o", str(dst), str(src)],
                          capture_output=True, text=True, timeout=300)
    if not dst.exists() and not list(dst.parent.glob(dst.stem + "-*" + dst.suffix)):
        tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-10:])
        raise TabError(f"MuseScore failed to write {dst.name} "
                       f"(exit {proc.returncode}).\n{tail}")

# ---------------------------------------------------------------------- main

def parse_timesig(text):
    try:
        beats, beat_type = text.split("/")
        beats, beat_type = int(beats), int(beat_type)
        if beat_type not in (2, 4, 8) or not 1 <= beats <= 16:
            raise ValueError
        return beats, beat_type
    except ValueError:
        raise TabError(f"cannot read time signature '{text}' (try 4/4 or 3/4)")

def main():
    settings = load_settings()
    instruments = apply_settings(settings)
    default_instr = settings.get("default_instrument", "guitar")
    if default_instr not in instruments:
        default_instr = "guitar"

    ap = argparse.ArgumentParser(description="Add a TAB staff to sheet music, "
                                             "or turn typed TAB into a score.")
    ap.add_argument("input", nargs="*",
                    help="PDF/image scan, MusicXML, or ASCII TAB .txt; several "
                         "image files are combined as pages, in the order given")
    ap.add_argument("--setup", action="store_true",
                    help="install MuseScore (needed to print) and exit")
    ap.add_argument("--omr", action="store_true",
                    help="with --setup, also install Audiveris and OCR data "
                         "for reading scanned sheet music")
    ap.add_argument("--instrument", default=default_instr,
                    choices=sorted(instruments),
                    help=f"instrument (default {default_instr})")
    ap.add_argument("--capo", type=int, default=0,
                    help="capo fret; TAB numbers come out capo-relative")
    ap.add_argument("--part", help="part id to tab (default: busiest part)")
    ap.add_argument("--staff", type=int, default=1,
                    help="staff to tab in a multi-staff part (default 1)")
    ap.add_argument("--out", help="output folder (default: next to input)")
    ap.add_argument("--title", help="title for typed-TAB input (default: file name)")
    ap.add_argument("--time", default="4/4", help="time signature for typed-TAB "
                                                  "input (default 4/4)")
    ap.add_argument("--base", default="eighth",
                    choices=["sixteenth", "eighth", "quarter"],
                    help="smallest note value in typed-TAB input (default eighth)")
    ap.add_argument("--mode", default="auto",
                    choices=["auto", "tab", "chart", "omr"],
                    help="how to read .txt/.pdf input: ASCII TAB, a chord "
                         "chart with lyrics, or scanned notation via OMR "
                         "(default: detect)")
    ap.add_argument("--tab-style", default=None,
                    choices=["asfound", "asfound-hp", "standard"],
                    help="asfound (default): the source's layout, hammer-ons "
                         "and pull-offs as plain curves. asfound-hp: same "
                         "layout but with H / P letters so they cannot be "
                         "confused with ties. standard: H / P letters with "
                         "the engraver's usual layout")
    ap.add_argument("--compact", action="store_true",
                    help="tighter layout: smaller staves and margins, "
                         "fewer pages")
    ap.add_argument("--png", action="store_true", help="also render a PNG preview")
    args = ap.parse_args()

    if args.setup:
        sys.exit(0 if install_dependencies(omr=args.omr) else 1)
    if not args.input:
        ap.error("an input file is required (or use --setup)")

    try:
        _run_conversion(args, settings, instruments)
    except TabError as e:
        sys.exit(f"ERROR: {e}")

def _run_conversion(args, settings, instruments):
    instr = with_capo(instruments[args.instrument], args.capo)
    inputs = [Path(a).expanduser().resolve() for a in args.input]
    for p in inputs:
        if not p.exists():
            raise TabError(f"{p} does not exist")
    multi = len(inputs) > 1
    if multi:
        raster = IMAGE_EXTS - {".pdf"}
        bad = [p.name for p in inputs if p.suffix.lower() not in raster]
        if bad:
            raise TabError("when giving several files they must all be page "
                           f"images (PNG/JPG/TIFF/BMP/SVG); not: {', '.join(bad)}")
    inp = inputs[0]
    outdir = Path(args.out).resolve() if args.out else inp.parent
    outdir.mkdir(parents=True, exist_ok=True)
    if multi:
        # "page-1", "scan 2" etc. -> shared stem without the page number
        stem = re.sub(r"[-_ ]*\d+$", "", inp.stem) or inp.stem
    else:
        stem = inp.stem
    if stem.endswith("_TAB"):
        stem = stem[:-4]
    if args.title:
        safe = re.sub(r'[\\/:*?"<>|]', "", args.title).strip()
        stem = safe or stem

    with tempfile.TemporaryDirectory(prefix="sheet2tab_",
                                     ignore_cleanup_errors=True) as td:
        workdir = Path(td)
        ext = inp.suffix.lower()
        ascii_mode = False
        from_engraved_tab = False
        tree = None
        beats, beat_type = parse_timesig(args.time)

        if ext in IMAGE_EXTS and args.mode != "omr":
            if args.mode in ("auto", "tab"):
                pages = inputs if multi else [inp]
                result = engraved_tab_pages(pages, instr, beats, beat_type)
                if result:
                    events, absolute, bar_meta = result
                    from_engraved_tab = True
                    notes = sum(len(nn) for _, nn in events
                                if isinstance(nn, list))
                    print(f"[1/4] Found engraved TAB in "
                          f"{', '.join(p.name for p in pages)} "
                          f"({notes} notes"
                          + (", printed rhythm" if absolute else "")
                          + ") - reading it directly")
                    ascii_mode = True
                    base = "sixteenth" if absolute else args.base
                    tree = events_to_tree(events, instr,
                                          args.title or stem,
                                          beats, beat_type, base,
                                          bar_meta,
                                          system_breaks=not args.compact)
            if tree is None and ext == ".pdf":
                text = extract_pdf_text(inp)
                if text:
                    tmode = None
                    if args.mode in ("tab", "chart"):
                        tmode = args.mode
                    else:
                        tmode = pdf_text_mode(text)
                    if tmode == "tab":
                        print(f"[1/4] Found TAB text inside the PDF")
                        ascii_mode = True
                        events = parse_ascii_tab(text, instr)
                        print(f"  {len(events)} events "
                              f"({sum(len(x) for _, x in events)} notes)")
                        tree = events_to_tree(events, instr,
                                              args.title or stem,
                                              beats, beat_type, args.base)
                    elif tmode == "chart":
                        print(f"[1/4] Found a chord chart inside the PDF")
                        ascii_mode = True
                        tree = chart_to_tree(text, instr, args.title or stem)
                elif args.mode in ("tab", "chart"):
                    raise TabError("this PDF has no readable text for "
                                   f"--mode {args.mode}; try the default mode")
        if tree is None and multi:
            print(f"[1/4] Reading {len(inputs)} pages with Audiveris: "
                  + ", ".join(p.name for p in inputs))
            source = _images_for_omr(inputs, workdir)
            mxl = run_audiveris(source, workdir)
            tree = ET.parse(extract_musicxml(mxl, workdir))
        elif tree is None and ext in IMAGE_EXTS:
            print(f"[1/4] Reading the score with Audiveris: {inp.name}")
            source = inp if ext == ".pdf" else preprocess_image(inp, workdir)
            mxl = run_audiveris(source, workdir)
            xml_path = extract_musicxml(mxl, workdir)
            tree = ET.parse(xml_path)
        elif tree is None and ext == ".mxl":
            print(f"[1/4] Unpacking MusicXML: {inp.name}")
            tree = ET.parse(extract_musicxml(inp, workdir))
        elif tree is None and ext in XML_EXTS:
            print(f"[1/4] Using MusicXML directly: {inp.name}")
            tree = ET.parse(inp)
        elif tree is None and ext in TAB_TEXT_EXTS:
            raw = inp.read_text(encoding="utf-8", errors="replace")
            mode = args.mode
            if mode == "auto":
                mode = sniff_text_mode(raw)
            ascii_mode = True
            if mode == "chart":
                print(f"[1/4] Reading chord chart: {inp.name}")
                if args.capo:
                    print("  note: capo ignored in chord-chart mode - play "
                          "the written shapes with your capo on")
                    instr = instruments[args.instrument]
                tree = chart_to_tree(raw, instr, args.title or stem)
            else:
                print(f"[1/4] Reading typed TAB: {inp.name}")
                beats, beat_type = parse_timesig(args.time)
                events = parse_ascii_tab(raw, instr)
                print(f"  found {len(events)} events "
                      f"({sum(len(n) for _, n in events)} notes)")
                tree = events_to_tree(events, instr, args.title or stem,
                                      beats, beat_type, args.base)
        elif tree is None:
            raise TabError(f"unsupported input type '{ext}'")

        print("[2/4] Working out fingerings")
        from_omr = multi or (ext in IMAGE_EXTS and not ascii_mode)
        print(f"[3/4] Adding the {instr.label} TAB staff")
        add_tab_staff(tree, instr, staff=args.staff, part=args.part,
                      from_omr=from_omr, ascii_mode=ascii_mode)
        tabbed = workdir / "tabbed.musicxml"
        tree.write(tabbed, encoding="UTF-8", xml_declaration=True)

        print("[4/4] Rendering with MuseScore"
              + (" (compact layout)" if args.compact else ""))
        # our own marks only ever come from tabs we built ourselves; a
        # scanned score's trills and phrasing slurs are left alone
        root_el = tree.getroot()
        has_vibrato = ascii_mode and root_el.find(".//wavy-line") is not None
        # "as found" keeps the source's own look: hammer-ons and pull-offs
        # stay as the curved line drawn between the frets, exactly as the
        # original prints them. "standard" adds the H / P letters too.
        tab_style = args.tab_style or settings.get("tab_style", "asfound")
        has_hopo = (ascii_mode and tab_style in ("asfound-hp", "standard")
                    and (root_el.find(".//hammer-on") is not None
                         or root_el.find(".//pull-off") is not None))
        # matching the source's layout only means something when we read an
        # existing engraved tab
        as_found = (from_engraved_tab
                    and tab_style in ("asfound", "asfound-hp"))
        fit_lines = from_engraved_tab and not args.compact
        render_src = tabbed
        if (args.compact or has_vibrato or has_hopo or as_found
                or fit_lines):
            render_src = postprocess_mscx(tabbed, workdir, args.compact,
                                          has_vibrato, has_hopo, as_found,
                                          fit_lines)
        # compact prints get their own name so both versions can live
        # side by side
        tag = "_CTAB" if args.compact else "_TAB"
        out_pdf = outdir / f"{stem}{tag}.pdf"
        out_mscz = outdir / f"{stem}{tag}.mscz"
        out_xml = outdir / f"{stem}{tag}.musicxml"
        musescore_convert(render_src, out_pdf)
        musescore_convert(render_src, out_mscz)
        shutil.copyfile(tabbed, out_xml)
        if args.png:
            musescore_convert(render_src, outdir / f"{stem}{tag}.png")

    print()
    print(f"Done!  ->  {out_pdf}")
    print(f"       ->  {out_mscz}  (open in MuseScore to fix any misread notes)")

if __name__ == "__main__":
    main()
