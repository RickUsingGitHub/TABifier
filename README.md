# TABifier v1.04 — sheet music ↔ TAB, for guitar, ukulele & bass

© Rick & Dolly

**Windows:** [Download TABifier.exe](https://github.com/RickUsingGitHub/TABifier/releases/latest/download/TABifier.exe) — one file, no installer. Copy it anywhere; settings live in `%APPDATA%\TABifier`.

**Mac (Apple Silicon):** [Download TABifier-macOS-arm64.zip](https://github.com/RickUsingGitHub/TABifier/releases/latest/download/TABifier-macOS-arm64.zip) — unzip, then right-click **TABifier.app** → Open the first time (macOS Gatekeeper asks because the app is not signed with an Apple developer ID). Settings live in `~/Library/Application Support/TABifier`. If macOS says the app is damaged, that is the quarantine flag from the download; in Terminal: `xattr -cr TABifier.app` and Open again.

TABifier is free. If it saves you some typing, you can [buy us a coffee](https://ko-fi.com/TABifier).

## The easy way: the app

Double-click **TABifier.exe** (Windows) or **TABifier.app** (Mac). Copy it
or make a shortcut anywhere you like — settings follow the user account,
not the folder you put the app in.

On a fresh machine, TABifier offers to install MuseScore (free) so it can
print the finished page. On Windows, say yes and allow the permission
prompt. On a Mac it uses Homebrew if you have it (`brew install --cask
musescore`), otherwise download MuseScore from musescore.org. Watch the
log; it takes a few minutes. The same can be run by hand with
`TABifier.exe --engine --setup` or, from the Python scripts,
`python sheet2tab.py --setup`. (`TAB Maker.bat` on Windows, or
`TAB Maker.command` on a Mac, runs the same app from the scripts, for
tinkering.)

The scan reader (Audiveris, plus the OCR language data that lyrics need)
is only offered when you open a photo or scan of sheet music. Typed TAB,
chord charts, MusicXML, and PDFs that already contain TAB do not need it.
Install it then, or by hand with `TABifier.exe --engine --setup --omr`.

Pick your instrument at the top
(Guitar / Ukulele / Ukulele low-G / Bass), then:

- **From sheet music** — choose a PDF, a MusicXML file (`.musicxml` /
  `.mxl`, e.g. downloaded from musescore.com — these convert perfectly, no
  scanning involved), an SVG (MuseScore / Inkscape export), or photos/scans
  of the pages. For a multi-page piece,
  select all the page images at once; they're sorted naturally
  (`page1, page2, ... page10`) and the **Pages...** button lets you reorder
  or drop pages before converting. Sideways phone photos are rotated
  upright automatically, and the original's page layout is discarded so the
  taller notation+TAB systems re-flow cleanly (expect more pages than the
  original — each line of music is now two staves).
- **From typed TAB** — type or paste ASCII TAB (the `e|--0--2--|` kind from
  the internet, or use *Insert empty TAB lines* and fill in your own), give
  it a title, and get a properly engraved Score + TAB PDF. Your fingerings
  are kept exactly as typed.
- **From chord chart** — paste a chords-over-lyrics chart (the most common
  kind of song page online). Every chord becomes a strummed bar: chord
  symbol on top, real voicing on the notation staff, the shape on the TAB
  staff, and the lyrics underneath. Section headers like `[Chorus]` become
  bold markers in the score. Voicings are found by search on whichever
  instrument is selected, so ukulele charts get ukulele shapes. A `Capo 3`
  marking in the chart means: set your capo and play the written shapes
  (the Capo box is ignored in this mode on purpose).

Every conversion produces three files:

| File | What it is |
|------|------------|
| `Song_TAB.pdf` | Notation + TAB — print this |
| `Song_TAB.mscz` | Editable version — open in MuseScore to fix anything |
| `Song_TAB.musicxml` | Drop back onto the tool to regenerate the TAB |

**Make TAB.bat** still works too: drag any PDF, scan, MusicXML, or TAB .txt
onto it (guitar assumed).

## Settings

The **Settings...** button (or editing `settings.json` next to the scripts)
controls:

- **Default folder** for typed-TAB output, and **default instrument**.
- **Custom instruments** — the *Create new...* button next to the default
  instrument opens the instrument editor: name, tuning (typed as note names,
  low string first, e.g. `D2 A2 D3 G3 B3 E4`), number of frets, whether the
  notation is written an octave up (guitar-style), and clef. Drop-D guitar
  and mandolin ship as examples; anything with 2-8 strings works. Custom
  instruments show up in the app's dropdown and on the command line.
- **Fingering magic** — how strongly the engine prefers open strings, stays
  near the nut, and avoids hand jumps, plus the widest chord stretch it may
  demand. (Applies when reading sheet music; typed TAB always keeps your
  fingerings.)

**Markings** (next to the instrument picker) decides how the playing marks
are drawn when reading a TAB that already exists:

- *As in the original* (default) — the tab is reproduced as printed:
  note values above the staff, hammer-on and pull-off curves below the
  fret numbers, vibrato squiggles below, slides as their slanted stroke,
  and rests written as whole beats rather than dotted values. Nothing is
  restyled; the treble staff is simply added above.
  Hammer-on and pull-off marks are drawn short and tucked between the two
  fret numbers, so they stay distinct from ties (which sweep the whole gap
  and always join two *identical* numbers). Section headings (VERSE,
  CHORUS and the like), repeat signs and double bars are carried across
  too, on the bars they belong to.
- *Original + H / P* — the same layout, plus the conventional H and P
  letters for anyone who prefers them spelled out.
- *Standard (H / P)* — H and P letters with the engraver's usual layout
  (note values below the staff, vibrato above).

(`--tab-style asfound|asfound-hp|standard`; the default can be changed in
Settings.)

There's also a **Capo** box next to the instrument picker: TAB numbers come
out capo-relative, exactly as a player wants them (`--capo N` on the command
line). The **Compact layout** checkbox (or `--compact`) prints smaller
staves with tighter margins — roughly a page saved for every three — and
can be made the default in Settings; compact files are named
`Song_CTAB.pdf` so they sit alongside the full-size `Song_TAB.pdf` rather
than replacing it.

When reading a tab that already exists, the full-size version keeps the
source's own line breaks, so the same bars start each line as in the
original. That needs a little room, so the staves come out slightly
smaller than MuseScore's default; the compact version re-flows instead,
fitting as many bars per line as it can.

**Dragging text in:** select a tab or chord chart in your browser and drag
it straight onto the TABifier window — it lands in the right typing box,
TAB or chord chart, whichever it reads as. (Dragging text onto the
program *icon* cannot work: Windows only ever hands a program file names,
never the text itself. Drag onto the open window, or copy and use the
**Paste** button.)

**Editing the typing boxes:** right-click for Cut, Copy, Paste, Select all
and Clear; Ctrl+A selects everything, Ctrl+Z undoes, Ctrl+Y redoes.

**Dropping files works three ways:** drop them onto the TABifier icon,
drop them onto the open window, or use Browse. Sheet music (PDF,
MusicXML, page images) fills in the file box; a `.txt` or `.tab` file is
read straight into the typing box — TAB or chord chart, whichever it
turns out to be — with the title filled in from the file name. Several
page images dropped together are sorted into page order.

Every control has hover help — rest the mouse on it for a sentence
explaining what it does — and each window (main, Settings, Custom
instruments) has a **?** button with a fuller plain-language explanation.

Staff labels are left off: the notation and TAB staves are plainly one
instrument, so nothing is printed down the left of every system.

## Tabs from the internet

Open the tab in your browser, select the whole thing (Ctrl+A works fine),
copy, and paste it into **From typed TAB**. Section headers, chord names,
"let ring" notes, `x2` marks, and other page clutter are ignored
automatically — only the TAB lines are read. If the tab says "Capo 3", set
the Capo box to 3. Technique marks (hammer-ons `h`, slides `/`, bends `b`,
vibrato `~`) are read as their plain notes for now.

Both typing tabs work the same way: a title box, the big box, and a
**Paste** button beside the save folder. If the title box is empty the
first real line of the paste is used, and it appears in the box so you
can see (and change) what was chosen — the same for TAB and for chord
charts.

Pasted web pages carry clutter, and it is skipped: the list of chord
shapes a page prints above the song, view counts, difficulty, tuning and
contributor lines. Words that merely look like chords (Bass, Add, Capo,
Chords) are no longer mistaken for them. If the title box is empty, the
first real line of the paste is used as the title.

There's deliberately no automatic downloader: tab sites license their
catalogs from music publishers and their terms don't allow automated
fetching, so the copy-paste route keeps things square. For fully legal
downloads, Ultimate Guitar's official app can export tabs for subscribers,
musescore.com offers licensed MusicXML/PDF downloads (which this tool eats
directly), and IMSLP / Mutopia have free public-domain sheet music for the
scan pipeline.

## Good to know

- **Scanning accuracy:** ~95%+ on cleanly printed music; dense chord stacks
  are the usual victims, blurry photos and handwriting won't work.
  Low-resolution images are upscaled automatically before reading, and
  lyric lines that the reader misfiles as stage directions (common in
  piano scores, where lyrics sit between the two staves) are rescued and
  re-attached to the melody as phrase lyrics. To fix a
  wrong note: open the `.mscz` and edit, or (better for several fixes) open
  the `.musicxml`, correct the *top* staff, export MusicXML over the same
  file, and run it through again — the TAB rebuilds with fingerings
  re-optimized.
- **Technique marks in typed TAB are read:** `18b20` is one picked note
  bent up a whole step (not two notes), `18b20r18` bends and releases,
  `17b` assumes a whole step, `h` and `p` become hammer-ons and pull-offs,
  `/` and `\` become slides, `~~~` vibrato, `(h)` a natural harmonic and
  `(p)` / `(ph)` a pinched one. Not yet read: `x` mutes, palm muting,
  tapping, and `>` holds (which only stretch the rhythm through spacing).
- **Brackets mean optional:** a fret written like `(4)` is a ghost or
  optional note and is left out, which is what those brackets mean in tab.
- **Set the Time signature** before converting if the song is not in 4/4 —
  a waltz is 3/4, for instance. Typed TAB carries no time signature, so
  TABifier cannot know.
- **Typed-TAB rhythm** is inferred from spacing: the tightest gap between
  numbers is one unit ("fastest note" in the app), wider gaps hold longer.
  It's an approximation — polish rhythms in MuseScore if they matter.
- **Fingerings** favor open position, minimal hand movement, and playable
  shapes. High-G ukulele's re-entrant string is handled properly (melodies
  will happily use the open G). Pieces out of range are octave-shifted to
  fit.
- **Piano scores:** the top staff is tabbed by default (`--staff 2` on the
  command line for the left hand). Rough but workable.

## Command line

```
python sheet2tab.py INPUT [--instrument guitar|ukulele|ukulele-low-g|bass]
                          [--part ID] [--staff N] [--out DIR]
                          [--title T] [--time 4/4]
                          [--base sixteenth|eighth|quarter] [--png]
                          [--setup] [--omr]
```

INPUT: `.pdf .png .jpg .tif .bmp .svg` (scanned music or a vector page;
SVG is drawn then read like a screenshot; scans go through Audiveris OMR),
`.musicxml .mxl .xml` (direct), or `.txt .tab` (ASCII TAB). Several image
files at once are combined as pages of one piece, in the order given.

PDFs get smart routing: engraved TAB (Guitar-Pro/Songsterr-style, where
fret numbers are text over drawn staff lines) is read directly by
geometry — your printed fingerings kept exactly, along with the printed
rhythm (stems and beams), ties, hammer-ons and pull-offs, slides, and
vibrato; ASCII-tab or chord-chart
text inside a PDF is parsed as such; everything else goes to the scan
reader. Force a route with `--mode tab|chart|omr`. (Python-script users:
these paths need `pip install pillow pypdf pdfplumber`; the exe has them
built in.)

## Under the hood

Python modules, stdlib only (PDF/SVG paths also want `pillow`, `pypdf`
and `pdfplumber`; the exe has them built in):

- `tab_read.py` — ASCII TAB, chord charts, engraved-TAB PDFs/images/SVG
- `svg_page.py` — draw an SVG page to a bitmap (no Cairo needed)
- `tab_write.py` — MusicXML with a TAB staff, including the fingering search
- `sheet2tab.py` — CLI, MuseScore/Audiveris setup, scan glue

[Audiveris](https://audiveris.github.io/audiveris/) reads scans → a dynamic
program over chord frames assigns strings and frets (or keeps yours from
typed TAB) → [MuseScore](https://musescore.org) engraves the result.
`tabmaker_gui.py` is a Tkinter front end that shells out to the engine.
Both helper apps are free; installed via winget on Windows, or Homebrew /
musescore.org on a Mac.

Parser and MusicXML tests (no MuseScore needed):

```
python -m unittest tests.test_parse tests.test_write
```

A Mac `.app` is built on GitHub Actions (`macos-latest`, Apple Silicon)
and attached to the latest Release as `TABifier-macOS-arm64.zip`. It is
ad-hoc signed, not notarized.
