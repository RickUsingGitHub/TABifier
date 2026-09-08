#!/usr/bin/env python3
"""TABifier -- turn sheet music, TAB text, or chord charts into Score + TAB.

(c) Rick & Dolly

Three ways in, one way out:
  * pick a PDF/scan/MusicXML of sheet music  -> Score + TAB PDF
  * type or paste ASCII TAB                  -> Score + TAB PDF
  * paste a chord chart with lyrics          -> Score + TAB PDF

Runs as a plain script next to sheet2tab.py, or frozen as TABifier.exe /
TABifier.app (where the app re-invokes itself with --engine for conversions
and settings live in the OS application-support folder).
"""

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

# under pythonw there is no console; give prints somewhere to go before
# importing the engine (which prints notes while loading settings)
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import sheet2tab as engine

# tkdnd, if it is available, lets selected TEXT be dragged in from a
# browser as well as files. Without it we fall back to windnd, which
# handles dropped files only.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES, DND_TEXT
    HAVE_TKDND = True
except ImportError:
    TkinterDnD = None
    DND_FILES = DND_TEXT = None
    HAVE_TKDND = False

ENGINE = Path(__file__).with_name("sheet2tab.py")

VERSION = "1.04"
APP_TITLE = "TABifier v" + VERSION

KOFI_URL = "https://ko-fi.com/TABifier"

MUSIC_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
                  ".svg", ".musicxml", ".mxl", ".xml"}
TEXT_SUFFIXES = {".txt", ".tab"}


def resource_path(name):
    """Files bundled beside the script, or inside the frozen exe."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / name if base else Path(__file__).with_name(name)


def open_os_path(path):
    path = str(path)
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _helper_install_blurb():
    if sys.platform == "darwin":
        return ("Install it now? If Homebrew is installed, TABifier will use "
                "it; otherwise download MuseScore from musescore.org. "
                "This can take a few minutes.")
    if sys.platform == "win32":
        return ("Install it now? Windows may show a permission prompt - "
                "please allow it. This can take a few minutes.")
    return "Install it now? This can take a few minutes."

MAGIC_DEFAULTS = {"prefer_open_strings": 30, "stay_low": 25,
                  "avoid_jumps": 50, "max_stretch": 4}

MAGIC_TIPS = {
    "prefer_open_strings": "Higher uses more open strings, which ring on "
                           "and are easier to play.",
    "stay_low": "Higher keeps the playing down at the low end of the neck, "
                "near the nut.",
    "avoid_jumps": "Higher keeps the hand in one place rather than moving "
                   "up and down the neck.",
}

# how hammer-ons and pull-offs are drawn: keep the source's own curved line
# between the frets, or add the H / P letters on top of it
STYLE_LABELS = ["As in the original", "Original + H / P", "Standard (H / P)"]
STYLE_KEYS = {STYLE_LABELS[0]: "asfound", STYLE_LABELS[1]: "asfound-hp",
              STYLE_LABELS[2]: "standard"}
STYLE_FROM_KEY = {v: k for k, v in STYLE_KEYS.items()}

FILETYPES = [
    ("Sheet music", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.svg *.musicxml *.mxl *.xml"),
    ("Page images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.svg"),
    ("MusicXML", "*.musicxml *.mxl *.xml"),
    ("ASCII TAB text", "*.txt *.tab"),
    ("All files", "*.*"),
]


def natural_key(path):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", Path(path).name)]


class Tooltip:
    """Hover help: a small note that appears after a short pause."""

    def __init__(self, widget, text, delay=600):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.after_id = None
        self.win = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None):
        self.cancel()
        self.after_id = self.widget.after(self.delay, self.show)

    def cancel(self):
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    def show(self):
        if self.win is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.win = tk.Toplevel(self.widget)
        self.win.wm_overrideredirect(True)
        self.win.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.win, text=self.text, justify="left",
                 background="#ffffe0", relief="solid", borderwidth=1,
                 wraplength=330, padx=6, pady=4).pack()

    def hide(self, _event=None):
        self.cancel()
        if self.win is not None:
            self.win.destroy()
            self.win = None


def tip(widget, text):
    """Attach hover help; returns the widget so it can be used inline."""
    Tooltip(widget, text)
    return widget


def _select_all(event):
    """Ctrl+A. Tk leaves this unbound, which is a nuisance when replacing a
    box full of tab."""
    widget = event.widget
    if isinstance(widget, tk.Text):
        widget.tag_add("sel", "1.0", "end-1c")
        widget.mark_set("insert", "1.0")
    else:
        widget.select_range(0, "end")
        widget.icursor("end")
    return "break"


def _undo(event):
    try:
        event.widget.edit_undo()
    except tk.TclError:
        pass                          # nothing left to undo
    return "break"


def _redo(event):
    try:
        event.widget.edit_redo()
    except tk.TclError:
        pass
    return "break"


def add_edit_menu(widget):
    """A right-click menu, so cut and paste do not depend on knowing the
    keyboard shortcuts."""
    menu = tk.Menu(widget, tearoff=0)
    menu.add_command(label="Cut",
                     command=lambda: widget.event_generate("<<Cut>>"))
    menu.add_command(label="Copy",
                     command=lambda: widget.event_generate("<<Copy>>"))
    menu.add_command(label="Paste",
                     command=lambda: widget.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="Select all",
                     command=lambda: widget.event_generate("<<SelectAll>>"))
    if isinstance(widget, tk.Text):
        menu.add_command(
            label="Clear",
            command=lambda: widget.delete("1.0", "end"))

    def popup(event):
        try:
            widget.focus_set()
        except tk.TclError:
            pass
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    widget.bind("<Button-3>", popup, add="+")


def enable_editing(container):
    """Give every typing box in a window the usual editing behaviour."""
    stack = [container]
    while stack:
        widget = stack.pop()
        stack.extend(widget.winfo_children())
        if isinstance(widget, ttk.Combobox):
            continue                  # a drop-down list, not a typing box
        if not isinstance(widget, (tk.Text, tk.Entry)):
            continue
        add_edit_menu(widget)
        for seq in ("<Control-a>", "<Control-A>"):
            widget.bind(seq, _select_all, add="+")
        if isinstance(widget, tk.Text):
            for seq in ("<Control-z>", "<Control-Z>"):
                widget.bind(seq, _undo, add="+")
            for seq in ("<Control-y>", "<Control-Y>"):
                widget.bind(seq, _redo, add="+")


def make_kofi_button(parent, top):
    """The support button. Falls back to a plain link if the artwork is
    missing, so the app never loses the way to say thank you."""
    art = resource_path("kofi_button.png")
    widget = None
    if art.exists():
        try:
            img = tk.PhotoImage(file=str(art))
            widget = ttk.Label(parent, image=img, cursor="hand2")
            widget.image = img        # keep a reference or Tk drops it
        except tk.TclError:
            widget = None
    if widget is None:
        widget = ttk.Label(parent, text="Support us on Ko-fi",
                           foreground="#2a6db5", cursor="hand2")
    widget.bind("<Button-1>", lambda _e: show_kofi(top))
    return widget


def show_kofi(parent):
    """The Ko-fi page as a link and a QR code, for scanning with a phone."""
    win = tk.Toplevel(parent)
    win.title("Buy us a coffee?")
    win.resizable(False, False)
    win.transient(parent)
    ttk.Label(win, text="TABifier is free.\nIf it has saved you some "
                        "typing, this helps keep it going.",
              justify="center").pack(padx=16, pady=(14, 8))
    qr = resource_path("kofi_qr.png")
    if qr.exists():
        try:
            img = tk.PhotoImage(file=str(qr))
            holder = ttk.Label(win, image=img, borderwidth=1, relief="solid")
            holder.image = img          # keep a reference or Tk drops it
            holder.pack(padx=16)
            ttk.Label(win, text="Scan with a phone camera",
                      foreground="#666").pack(pady=(4, 0))
        except tk.TclError:
            pass
    link = ttk.Label(win, text=KOFI_URL, foreground="#2a6db5",
                     cursor="hand2")
    link.pack(pady=(10, 4))
    link.bind("<Button-1>", lambda _e: webbrowser.open(KOFI_URL))
    row = ttk.Frame(win)
    row.pack(fill="x", padx=16, pady=(4, 14))
    tip(ttk.Button(row, text="Open in browser",
                   command=lambda: webbrowser.open(KOFI_URL)),
        "Opens the Ko-fi page in your web browser.").pack(side="left")
    ttk.Button(row, text="Close", command=win.destroy).pack(side="right")
    win.grab_set()
    return win


def show_help(parent, title, body):
    """A plain-language help window for one screen."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.geometry("580x540")
    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=10, pady=10)
    sb = ttk.Scrollbar(frame)
    sb.pack(side="right", fill="y")
    txt = tk.Text(frame, wrap="word", relief="flat", padx=10, pady=8,
                  background="#fcfcfc", yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.configure(command=txt.yview)
    txt.tag_configure("h", font=("Segoe UI", 10, "bold"), spacing1=8)
    for line in body.strip().splitlines():
        if line.startswith("# "):
            txt.insert("end", line[2:] + "\n", "h")
        else:
            txt.insert("end", line + "\n")
    txt.configure(state="disabled")
    ttk.Button(win, text="Close", command=win.destroy).pack(
        anchor="e", padx=10, pady=(0, 10))
    win.grab_set()
    return win


MAIN_HELP = f"""
TABifier v{VERSION} turns music into a page with standard notation on top and TAB
underneath, ready to print.

# Three ways in
From sheet music - choose a PDF, a MusicXML file, or photos/scans of the
pages. Pick several images at once for a multi-page piece; the Pages...
button puts them in the right order.

From typed TAB - type or paste TAB (the e|--0--2--| kind found online).
Your fingerings are kept exactly as typed.

From chord chart - paste a chords-over-lyrics page. Every chord becomes a
strummed bar with the shape on the TAB staff and the words underneath.

# Along the top
Instrument - what the TAB is written for. Anything you create in Settings
appears here too.

Capo - the fret your capo sits on. The numbers come out capo-relative,
the way a player reads them.

Compact layout - smaller staves and tighter margins, saving approximately
one page in three. Saved as Song_CTAB.pdf, so it sits beside the
full-size version rather than replacing it.

Markings - how hammer-ons and pull-offs are drawn when reading a TAB that
already exists. See the help in Settings for the three choices.

# What you get
Song_TAB.pdf - print this one.
Song_TAB.mscz - open in MuseScore to correct anything.
Song_TAB.musicxml - drop back onto TABifier to rebuild the TAB.

# If something comes out wrong
Reading printed music is not perfect. Open the .mscz in MuseScore, fix
the note, and print from there. For several fixes, correct the top staff
of the .musicxml, export it from MuseScore, and run it through TABifier
again - the TAB is rebuilt from your corrections.

# Helpers
The first time you run TABifier it offers to install MuseScore, which is
what prints the page. The scan reader (Audiveris) is only offered if you
open a photo or scan of sheet music. Typed TAB, chord charts and MusicXML
files do not need it.
"""

SETTINGS_HELP = """
# General
Default folder - where typed TAB and chord chart conversions are saved.
Sheet music conversions always land next to the file you picked.

Default instrument - the one selected when TABifier opens.

Create new... - define your own instrument: any tuning, 2 to 8 strings.

Compact layout by default - start with compact printing already ticked.

Default markings - how hammer-ons and pull-offs are drawn when reading an
existing TAB:
  As in the original - reproduced as printed. Hammer-ons are the small
  curve tucked between the fret numbers; a tie is the long curve, and it
  always joins two identical numbers.
  Original + H / P - the same, plus the H and P letters, if you would
  rather see them spelled out.
  Standard (H / P) - the letters with the engraver's usual layout.

# Fingering magic
These only apply when reading sheet music, where TABifier has to work out
the fingering itself. Typed TAB always keeps your own fingerings.

Prefer open strings - higher uses more open strings.
Stay near the nut - higher keeps the playing down at the low end of the
neck.
Avoid hand jumps - higher keeps the hand in one place rather than moving
up and down.
Max chord stretch - how many frets a chord is allowed to span. Lower
suits smaller hands.

Reset magic puts all four back to normal.
"""

INSTRUMENT_HELP = """
Define any stringed instrument with 2 to 8 strings.

# Name
What it is called in the instrument list.

# Tuning
The open strings, LOWEST string first, separated by spaces. Each is a
letter, an optional # or b, then the octave number:

  Guitar      E2 A2 D3 G3 B3 E4
  Drop D      D2 A2 D3 G3 B3 E4
  Bass        E1 A1 D2 G2
  Ukulele     G4 C4 E4 A4
  Mandolin    G3 D4 A4 E5

Octave 4 is the one containing middle C. If a whole instrument sounds an
octave too high or low, change every octave number by one.

# Frets
How far up the neck it goes. Only affects which fingerings are offered.

# Written an octave up
Tick for the guitar family, whose music is written an octave higher than
it actually sounds. Leave unticked for mandolin, violin and similar.

# Clef
Treble for most instruments, bass for low ones.

# Add / Update
Saves the instrument. Using a name that already exists updates it instead
of adding a second one.
"""


class PageOrderDialog(tk.Toplevel):
    """Reorder the selected page images before converting."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Page order")
        self.resizable(False, True)
        self.transient(app.root)
        ttk.Label(self, text="Pages are read top to bottom:").pack(
            anchor="w", padx=10, pady=(10, 2))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        self.listbox = tk.Listbox(body, height=10, width=44, exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        col = ttk.Frame(body)
        col.pack(side="left", fill="y", padx=(6, 0))
        tip(ttk.Button(col, text="Up", command=lambda: self.move(-1)),
            "Move the selected page earlier.").pack(fill="x")
        tip(ttk.Button(col, text="Down", command=lambda: self.move(1)),
            "Move the selected page later.").pack(fill="x", pady=4)
        tip(ttk.Button(col, text="Remove", command=self.remove),
            "Leave this page out of the conversion.").pack(fill="x")
        ttk.Button(self, text="Done", command=self.destroy).pack(
            anchor="e", padx=10, pady=(4, 10))
        self.refresh()
        enable_editing(self)
        self.grab_set()

    def refresh(self, keep=None):
        self.listbox.delete(0, "end")
        for i, p in enumerate(self.app.selected_files, 1):
            self.listbox.insert("end", f"{i}.  {Path(p).name}")
        if keep is not None and 0 <= keep < len(self.app.selected_files):
            self.listbox.selection_set(keep)
            self.listbox.see(keep)
        self.app.show_selection()

    def move(self, delta):
        sel = self.listbox.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + delta
        files = self.app.selected_files
        if 0 <= j < len(files):
            files[i], files[j] = files[j], files[i]
            self.refresh(keep=j)

    def remove(self):
        sel = self.listbox.curselection()
        if sel:
            del self.app.selected_files[sel[0]]
            self.refresh(keep=min(sel[0], len(self.app.selected_files) - 1))


def string_names(instr):
    """Display names for TAB template lines, string 1 (top line) first."""
    names = []
    for midi in instr.tuning:
        step, alter = engine.SHARP_SPELLING[midi % 12]
        names.append(step + ("#" if alter else ""))
    if names:
        names[0] = names[0].lower()
    return names


def template_lines(instr):
    return [f"{n:<2}|" + "-" * 40 + "|" for n in string_names(instr)]


class InstrumentsDialog(tk.Toplevel):
    """Create, edit and remove custom instruments (popup off Settings)."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.title("Custom instruments")
        self.resizable(False, False)
        self.transient(owner)
        pad = {"padx": 8, "pady": 3}

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y", **pad)
        self.listbox = tk.Listbox(left, height=8, width=22, exportselection=False)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.load_selected)
        tip(self.listbox,
            "Instruments you have made. Click one to edit it.")
        tip(ttk.Button(left, text="Remove", command=self.remove_selected),
            "Delete the selected instrument.").pack(fill="x", pady=(4, 0))

        form = ttk.Frame(body)
        form.pack(side="left", fill="both", expand=True, **pad)
        ttk.Label(form, text="Name:").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar()
        tip(ttk.Entry(form, textvariable=self.name_var, width=24),
            "What it is called in the instrument list, e.g. Drop D "
            "Guitar.").grid(row=0, column=1, columnspan=3, sticky="we", pady=2)
        ttk.Label(form, text="Tuning:").grid(row=1, column=0, sticky="w")
        self.tuning_var = tk.StringVar()
        tip(ttk.Entry(form, textvariable=self.tuning_var, width=24),
            "The open strings, lowest first, separated by spaces: a letter, "
            "an optional # or b, then the octave number. Guitar is "
            "E2 A2 D3 G3 B3 E4. Octave 4 holds middle C.").grid(
            row=1, column=1, columnspan=3, sticky="we", pady=2)
        ttk.Label(form, text="low string first, e.g.  D2 A2 D3 G3 B3 E4",
                  foreground="#666").grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Label(form, text="Frets:").grid(row=3, column=0, sticky="w")
        self.frets_var = tk.IntVar(value=19)
        tip(ttk.Spinbox(form, from_=5, to=30, textvariable=self.frets_var,
                        width=5, state="readonly"),
            "How far up the neck it goes - the highest fret TABifier may "
            "use.").grid(row=3, column=1, sticky="w", pady=2)
        self.octave_var = tk.BooleanVar(value=True)
        tip(ttk.Checkbutton(form, text="written an octave up (like guitar)",
                            variable=self.octave_var),
            "Tick for the guitar family, whose music is written an octave "
            "higher than it sounds. Leave unticked for mandolin, violin and "
            "the like.").grid(row=4, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(form, text="Clef:").grid(row=5, column=0, sticky="w")
        self.clef_var = tk.StringVar(value="treble")
        tip(ttk.Combobox(form, textvariable=self.clef_var, state="readonly",
                         width=8, values=["treble", "bass"]),
            "Treble for most instruments, bass for low ones.").grid(
            row=5, column=1, sticky="w", pady=2)
        tip(ttk.Button(form, text="Add / Update", command=self.add_update),
            "Save this instrument. Reusing an existing name updates that "
            "one.").grid(row=6, column=1, sticky="w", pady=6)
        form.columnconfigure(1, weight=1)
        foot = ttk.Frame(self)
        foot.pack(fill="x", padx=10, pady=(0, 10))
        tip(ttk.Button(foot, text="?", width=3,
                       command=lambda: show_help(self, "Custom instruments",
                                                 INSTRUMENT_HELP)),
            "How to describe an instrument, with examples.").pack(side="left")
        tip(ttk.Button(foot, text="Done", command=self.destroy),
            "Close and go back to Settings.").pack(side="right")
        self.refresh()
        self.grab_set()

    def customs(self):
        return self.owner.work["custom_instruments"]

    def refresh(self):
        self.listbox.delete(0, "end")
        for d in self.customs():
            self.listbox.insert("end", d.get("name", "?"))

    def load_selected(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        d = self.customs()[sel[0]]
        self.name_var.set(d.get("name", ""))
        self.tuning_var.set(d.get("tuning", ""))
        self.frets_var.set(int(d.get("frets", 19)))
        self.octave_var.set(bool(d.get("octave_up", True)))
        self.clef_var.set(d.get("clef", "treble"))

    def add_update(self):
        d = {"name": self.name_var.get().strip(),
             "tuning": self.tuning_var.get().strip(),
             "frets": int(self.frets_var.get()),
             "octave_up": bool(self.octave_var.get()),
             "clef": self.clef_var.get()}
        try:
            engine.instrument_from_custom(d)
        except (ValueError, KeyError, TypeError) as e:
            messagebox.showerror("TABifier", f"Cannot save this instrument:\n{e}",
                                 parent=self)
            return
        names = [c.get("name", "").strip().lower() for c in self.customs()]
        if d["name"].lower() in names:
            self.customs()[names.index(d["name"].lower())] = d
        else:
            self.customs().append(d)
        self.refresh()

    def remove_selected(self):
        sel = self.listbox.curselection()
        if sel:
            del self.customs()[sel[0]]
            self.refresh()


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("TABifier settings")
        self.resizable(False, False)
        self.transient(app.root)
        self.work = json.loads(json.dumps(app.settings))  # deep copy
        self.work.setdefault("custom_instruments", [])
        pad = {"padx": 8, "pady": 3}

        # ---- general ------------------------------------------------------
        gen = ttk.LabelFrame(self, text="General")
        gen.pack(fill="x", padx=10, pady=6)
        row = ttk.Frame(gen)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Default folder (typed TAB):").pack(side="left")
        self.dir_var = tk.StringVar(value=self.work.get(
            "default_output_dir", str(Path.home() / "Documents" / "TABs")))
        tip(ttk.Entry(row, textvariable=self.dir_var, width=38),
            "Where typed TAB and chord chart conversions are saved. Sheet "
            "music always lands next to the file you picked.").pack(
            side="left", fill="x", expand=True, padx=4)
        tip(ttk.Button(row, text="...", width=3, command=self.pick_dir),
            "Choose the default folder.").pack(side="left")
        row2 = ttk.Frame(gen)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Default instrument:").pack(side="left")
        self.def_instr_var = tk.StringVar()
        self.def_instr_combo = ttk.Combobox(row2, textvariable=self.def_instr_var,
                                            state="readonly", width=20)
        self.def_instr_combo.pack(side="left", padx=4)
        tip(self.def_instr_combo,
            "The instrument selected when TABifier opens.")
        tip(ttk.Button(row2, text="Create new...",
                       command=self.open_instruments),
            "Define your own instrument - any tuning, 2 to 8 strings. Also "
            "edits and removes ones you have made.").pack(side="left", padx=6)
        self.compact_default_var = tk.BooleanVar(
            value=bool(self.work.get("compact_default", False)))
        tip(ttk.Checkbutton(gen, text="Compact layout by default",
                            variable=self.compact_default_var),
            "Start every conversion with compact printing already ticked.").pack(
            anchor="w", padx=8, pady=(0, 4))
        row3 = ttk.Frame(gen)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="Default markings:").pack(side="left")
        self.style_default_var = tk.StringVar(
            value=STYLE_FROM_KEY.get(self.work.get("tab_style", "asfound"),
                                     STYLE_LABELS[0]))
        tip(ttk.Combobox(row3, textvariable=self.style_default_var,
                         state="readonly", width=17, values=STYLE_LABELS),
            "How hammer-ons and pull-offs are drawn when reading an existing "
            "TAB. Press ? below for what each choice looks like.").pack(
            side="left", padx=4)
        ttk.Label(row3, text="(keeps the source's own hammer-on curves)",
                  foreground="#666").pack(side="left", padx=6)

        # ---- fingering magic ---------------------------------------------
        magic = ttk.LabelFrame(self, text="Fingering magic (sheet music mode)")
        magic.pack(fill="x", padx=10, pady=6)
        self.magic_vars = {}
        rows = [("prefer_open_strings", "Prefer open strings"),
                ("stay_low", "Stay near the nut"),
                ("avoid_jumps", "Avoid hand jumps")]
        cur = {**MAGIC_DEFAULTS, **self.work.get("magic", {})}
        for key, label in rows:
            r = ttk.Frame(magic)
            r.pack(fill="x", **pad)
            ttk.Label(r, text=label + ":", width=20).pack(side="left")
            var = tk.IntVar(value=int(cur.get(key, MAGIC_DEFAULTS[key])))
            self.magic_vars[key] = var
            val = ttk.Label(r, width=4, anchor="e")
            scale = ttk.Scale(r, from_=0, to=100, variable=var,
                              command=lambda _v, v=var, l=val:
                              l.configure(text=str(int(float(v.get())))))
            scale.pack(side="left", fill="x", expand=True, padx=4)
            tip(scale, MAGIC_TIPS[key])
            val.configure(text=str(var.get()))
            val.pack(side="left")
        r = ttk.Frame(magic)
        r.pack(fill="x", **pad)
        ttk.Label(r, text="Max chord stretch (frets):", width=22).pack(side="left")
        self.stretch_var = tk.IntVar(value=int(cur.get("max_stretch", 4)))
        tip(ttk.Spinbox(r, from_=2, to=6, textvariable=self.stretch_var,
                        width=4, state="readonly"),
            "How many frets a chord may span. Lower suits smaller "
            "hands.").pack(side="left", padx=4)
        tip(ttk.Button(r, text="Reset magic", command=self.reset_magic),
            "Put all four fingering settings back to normal.").pack(
            side="right")

        # ---- buttons ------------------------------------------------------
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        tip(ttk.Button(btns, text="Cancel", command=self.destroy),
            "Close without keeping the changes.").pack(side="right")
        tip(ttk.Button(btns, text="Save", command=self.save),
            "Keep these settings and close.").pack(side="right", padx=6)
        tip(ttk.Button(btns, text="?", width=3,
                       command=lambda: show_help(self, "Settings help",
                                                 SETTINGS_HELP)),
            "What each setting does.").pack(side="left")

        self.refresh_lists()
        enable_editing(self)
        self.grab_set()

    # ------------------------------------------------------------- helpers
    def open_instruments(self):
        dlg = InstrumentsDialog(self)
        self.wait_window(dlg)
        if self.winfo_exists():
            self.refresh_lists()
            self.grab_set()

    def refresh_lists(self):
        instruments = self.current_instruments()
        labels = [i.label for i in instruments.values()]
        self.def_instr_combo.configure(values=labels)
        want_key = self.work.get("default_instrument", "guitar")
        label = next((i.label for i in instruments.values() if i.key == want_key),
                     "Guitar")
        self.def_instr_var.set(label)

    def current_instruments(self):
        merged = dict(engine.INSTRUMENTS)
        for d in self.work["custom_instruments"]:
            try:
                i = engine.instrument_from_custom(d)
                merged[i.key] = i
            except (ValueError, KeyError, TypeError):
                pass
        return merged

    def pick_dir(self):
        d = filedialog.askdirectory(parent=self, title="Default output folder",
                                    initialdir=self.dir_var.get() or str(Path.home()))
        if d:
            self.dir_var.set(d)

    def reset_magic(self):
        for key, var in self.magic_vars.items():
            var.set(MAGIC_DEFAULTS[key])
        self.stretch_var.set(MAGIC_DEFAULTS["max_stretch"])

    def save(self):
        instruments = self.current_instruments()
        label = self.def_instr_var.get()
        key = next((i.key for i in instruments.values() if i.label == label),
                   "guitar")
        self.work["default_output_dir"] = self.dir_var.get().strip()
        self.work["default_instrument"] = key
        self.work["compact_default"] = bool(self.compact_default_var.get())
        self.work["tab_style"] = STYLE_KEYS.get(self.style_default_var.get(),
                                                "asfound")
        self.work["magic"] = {k: int(v.get()) for k, v in self.magic_vars.items()}
        self.work["magic"]["max_stretch"] = int(self.stretch_var.get())
        try:
            engine.SETTINGS_PATH.write_text(
                json.dumps(self.work, indent=2), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("TABifier", f"Cannot write settings:\n{e}",
                                 parent=self)
            return
        self.app.apply_new_settings()
        self.destroy()


class TabMakerApp:
    def __init__(self, root, smoke=False):
        self.root = root
        root.title(APP_TITLE)
        root.minsize(700, 600)
        self.queue = queue.Queue()
        self.running = False
        self.last_pdf = None
        self.last_dir = None
        self.settings = {}
        self.instruments = {}
        self._scratch = None
        self._retry_after_setup = False
        self._pending_convert = None
        self._need_omr = False
        self._last_convert = None

        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Instrument:").pack(side="left")
        self.instrument = tk.StringVar()
        self.instr_combo = ttk.Combobox(top, textvariable=self.instrument,
                                        state="readonly", width=18)
        self.instr_combo.pack(side="left", padx=6)
        tip(self.instr_combo,
            "What the TAB is written for. Instruments you create in "
            "Settings appear here too.")
        ttk.Label(top, text="Capo:").pack(side="left", padx=(12, 2))
        self.capo_var = tk.IntVar(value=0)
        tip(ttk.Spinbox(top, from_=0, to=9, textvariable=self.capo_var,
                        width=3, state="readonly"),
            "The fret your capo sits on. Leave at 0 for no capo. The TAB "
            "numbers come out capo-relative, the way a player reads "
            "them.").pack(side="left")
        self.compact_var = tk.BooleanVar(value=False)
        tip(ttk.Checkbutton(top, text="Compact layout",
                            variable=self.compact_var),
            "Smaller staves and tighter margins - saves roughly a page in "
            "three. Saved as Song_CTAB.pdf so it sits beside the full-size "
            "version.").pack(side="left", padx=(12, 0))
        ttk.Label(top, text="Markings:").pack(side="left", padx=(12, 2))
        self.style_var = tk.StringVar(value=STYLE_LABELS[0])
        tip(ttk.Combobox(top, textvariable=self.style_var, state="readonly",
                         width=17, values=STYLE_LABELS),
            "How hammer-ons and pull-offs are drawn when reading a TAB "
            "that already exists: as the original prints them, or with the "
            "H and P letters added.").pack(side="left")
        tip(ttk.Button(top, text="?", width=3,
                       command=lambda: show_help(root, "TABifier help",
                                                 MAIN_HELP)),
            "What this screen does, and what the buttons mean.").pack(
            side="right", padx=(6, 0))
        tip(ttk.Button(top, text="Settings...", command=self.open_settings),
            "Default folder, default instrument, your own instruments, and "
            "how fingerings are chosen.").pack(side="right")

        nb = ttk.Notebook(root)
        self.nb = nb
        nb.pack(fill="both", expand=True, **pad)

        # ---- tab 1: from sheet music -------------------------------------
        f1 = ttk.Frame(nb)
        nb.add(f1, text="  From sheet music  ")
        ttk.Label(f1, text="Take sheet music and add a TAB staff under the "
                           "notation. Works with a PDF, a MusicXML file "
                           "(.musicxml / .mxl, e.g. from musescore.com), or "
                           "photos/scans of the pages - pick several images "
                           "at once for a multi-page piece.", wraplength=620
                  ).pack(anchor="w", **pad)
        self.selected_files = []
        row = ttk.Frame(f1)
        row.pack(fill="x", **pad)
        self.file_var = tk.StringVar()
        self.multi_label = ""
        tip(ttk.Entry(row, textvariable=self.file_var),
            "The file to convert. Choosing several page images shows them "
            "here as a list of pages.").pack(
            side="left", fill="x", expand=True)
        self.btn_pages = ttk.Button(row, text="Pages...", state="disabled",
                                    command=lambda: PageOrderDialog(self))
        self.btn_pages.pack(side="left", padx=(6, 0))
        tip(self.btn_pages,
            "Put the pages in the right order, or drop one. Only needed "
            "when you pick several images at once.")
        tip(ttk.Button(row, text="Browse...", command=self.pick_file),
            "Choose a PDF, a MusicXML file, or photos/scans of the pages. "
            "Select several images together for a multi-page piece.").pack(
            side="left", padx=6)
        ttk.Label(f1, text="The new files appear next to the file you pick.",
                  foreground="#666").pack(anchor="w", padx=8)
        self.btn_file = ttk.Button(f1, text="Make Score + TAB PDF",
                                   command=self.convert_file)
        self.btn_file.pack(anchor="e", **pad)
        tip(self.btn_file,
            "Convert now. The new files appear next to the file you "
            "picked, and progress shows in the log below.")

        # ---- tab 2: from typed TAB ---------------------------------------
        f2 = ttk.Frame(nb)
        nb.add(f2, text="  From typed TAB  ")
        ttk.Label(f2, text="Type or paste TAB (the e|--0--2--| kind). Your "
                           "fingerings are kept exactly as typed, and the "
                           "notation staff is worked out from them.",
                  wraplength=620).pack(anchor="w", **pad)
        meta = ttk.Frame(f2)
        meta.pack(fill="x", **pad)
        ttk.Label(meta, text="Title:").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar()
        tip(ttk.Entry(meta, textvariable=self.title_var, width=28),
            "Printed at the top of the music, and used for the file "
            "name.").grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(meta, text="Time:").grid(row=0, column=2, sticky="e", padx=(12, 0))
        self.time_var = tk.StringVar(value="4/4")
        tip(ttk.Entry(meta, textvariable=self.time_var, width=6),
            "Beats per bar, e.g. 4/4 for most songs, 3/4 for a waltz.").grid(
            row=0, column=3, sticky="w", padx=4)
        ttk.Label(meta, text="Fastest note:").grid(row=0, column=4, sticky="e",
                                                   padx=(12, 0))
        self.base_var = tk.StringVar(value="eighth")
        tip(ttk.Combobox(meta, textvariable=self.base_var, state="readonly",
                         width=10,
                         values=["sixteenth", "eighth", "quarter"]),
            "The shortest note in your TAB. Typed TAB carries no rhythm, so "
            "TABifier measures the spacing between numbers against "
            "this.").grid(row=0, column=5, sticky="w", padx=4)

        self.text = tk.Text(f2, height=12, font=("Consolas", 11), wrap="none",
                            undo=True)
        self.text.pack(fill="both", expand=True, padx=8, pady=4)
        tip(self.text,
            "Type or paste TAB here. Pasting a whole page from the web is "
            "fine - headings, chord names and other clutter are ignored.")

        row2 = ttk.Frame(f2)
        row2.pack(fill="x", **pad)
        tip(ttk.Button(row2, text="Insert empty TAB lines",
                       command=self.insert_template),
            "Drop in blank staff lines for the chosen instrument, ready to "
            "type your own numbers onto.").pack(side="left")
        tip(ttk.Button(row2, text="Paste",
                       command=lambda: self.paste_into(self.text)),
            "Paste whatever you last copied into the box above. Right-click "
            "the box for cut, copy and select all.").pack(
            side="left", padx=(6, 0))
        ttk.Label(row2, text="Save to:").pack(side="left", padx=(16, 2))
        self.out_var = tk.StringVar()
        tip(ttk.Entry(row2, textvariable=self.out_var, width=30),
            "Where the finished files are saved. Change the default in "
            "Settings.").pack(side="left", fill="x", expand=True)
        tip(ttk.Button(row2, text="...", width=3, command=self.pick_outdir),
            "Choose the folder to save into.").pack(side="left", padx=4)
        self.btn_text = ttk.Button(row2, text="Make Score + TAB PDF",
                                   command=self.convert_text)
        self.btn_text.pack(side="right")
        tip(self.btn_text, "Convert the typed TAB into a printable page.")

        # ---- tab 3: from chord chart -------------------------------------
        f3 = ttk.Frame(nb)
        nb.add(f3, text="  From chord chart  ")
        ttk.Label(f3, text="Paste a chord chart (chord names above the "
                           "lyrics). Every chord becomes a strummed bar with "
                           "the chord symbol, the fingering on the TAB staff, "
                           "and the lyrics underneath.", wraplength=620
                  ).pack(anchor="w", **pad)
        meta3 = ttk.Frame(f3)
        meta3.pack(fill="x", **pad)
        ttk.Label(meta3, text="Title:").pack(side="left")
        self.chart_title_var = tk.StringVar()
        tip(ttk.Entry(meta3, textvariable=self.chart_title_var, width=32),
            "Printed at the top of the music, and used for the file "
            "name.").pack(side="left", padx=4)
        self.chart_text = tk.Text(f3, height=12, font=("Consolas", 11),
                                  wrap="none", undo=True)
        self.chart_text.pack(fill="both", expand=True, padx=8, pady=4)
        tip(self.chart_text,
            "Paste the whole song page: chord names on their own lines "
            "above the words. Section headings like [Chorus] are kept.")
        row3 = ttk.Frame(f3)
        row3.pack(fill="x", **pad)
        tip(ttk.Button(row3, text="Paste",
                       command=lambda: self.paste_into(self.chart_text)),
            "Paste the chord chart you last copied into the box above. "
            "Right-click the box for cut, copy and select all.").pack(
            side="left")
        ttk.Label(row3, text="Save to:").pack(side="left", padx=(16, 2))
        tip(ttk.Entry(row3, textvariable=self.out_var, width=34),
            "Where the finished files are saved. Change the default in "
            "Settings.").pack(side="left", fill="x", expand=True)
        tip(ttk.Button(row3, text="...", width=3, command=self.pick_outdir),
            "Choose the folder to save into.").pack(side="left", padx=4)
        self.btn_chart = ttk.Button(row3, text="Make Score + TAB PDF",
                                    command=self.convert_chart)
        self.btn_chart.pack(side="right")
        tip(self.btn_chart,
            "Turn the chord chart into a printable page.")

        # ---- log + result buttons ----------------------------------------
        logf = ttk.Frame(root)
        logf.pack(fill="both", **pad)
        self.log = tk.Text(logf, height=9, state="disabled", font=("Consolas", 9),
                           background="#1e1e1e", foreground="#d4d4d4")
        self.log.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(root)
        btns.pack(fill="x", **pad)
        self.status = ttk.Label(btns, text="Ready.")
        self.status.pack(side="left")
        ttk.Label(btns, text=APP_TITLE + " © Rick & Dolly",
                  foreground="#999").pack(side="left", padx=(16, 0))
        kofi = make_kofi_button(btns, root)
        kofi.pack(side="left", padx=(10, 0))
        tip(kofi, "TABifier is free. Opens a QR code and a link, if you "
                  "would like to chip in.")
        self.btn_open_dir = ttk.Button(btns, text="Open folder", state="disabled",
                                       command=self.open_dir)
        self.btn_open_dir.pack(side="right", padx=4)
        tip(self.btn_open_dir,
            "Open the folder holding the files just made.")
        self.btn_open_pdf = ttk.Button(btns, text="Open PDF", state="disabled",
                                       command=self.open_pdf)
        self.btn_open_pdf.pack(side="right", padx=4)
        tip(self.btn_open_pdf, "Open the finished PDF to look at or print.")
        tip(self.log, "Progress messages. If a conversion fails, the reason "
                      "appears here.")

        self.apply_new_settings()
        self.enable_drops()
        enable_editing(root)
        root.after(100, self.pump)
        if not smoke:
            root.after(400, self.offer_setup)
        if smoke:
            # let a build be checked from outside: did the QR and the
            # drag-and-drop helper actually make it into the exe?
            report = os.environ.get("TABIFIER_SMOKE_REPORT")
            if report:
                have_dnd = False
                if sys.platform == "win32":
                    try:
                        import windnd            # noqa: F401
                        have_dnd = True
                    except ImportError:
                        pass
                with open(report, "w", encoding="utf-8") as fh:
                    print("qr=%s" % resource_path("kofi_qr.png").exists(),
                          file=fh)
                    print("kofi_button=%s"
                          % resource_path("kofi_button.png").exists(),
                          file=fh)
                    print("windnd=%s" % have_dnd, file=fh)
                    print("tkdnd=%s" % HAVE_TKDND, file=fh)
                    print("text_drop=%s"
                          % hasattr(self.root, "drop_target_register"),
                          file=fh)

            def open_sd():
                self._smoke_sd = SettingsDialog(self)

            def kill_toplevels():
                for w in root.winfo_children():
                    if isinstance(w, tk.Toplevel):
                        w.destroy()

            def check_tooltip():
                t = Tooltip(self.btn_file, "smoke test")
                t.show()
                t.hide()

            root.after(300, check_tooltip)
            root.after(500, lambda: show_help(root, "TABifier help",
                                              MAIN_HELP))
            root.after(700, lambda: show_help(root, "Settings help",
                                              SETTINGS_HELP))
            root.after(900, lambda: show_help(root, "Custom instruments",
                                              INSTRUMENT_HELP))
            root.after(1100, kill_toplevels)
            root.after(1300, open_sd)
            root.after(1700, lambda: self._smoke_sd.open_instruments())
            root.after(2100, kill_toplevels)
            root.after(2500, root.destroy)

    # ----------------------------------------------------------- settings
    def apply_new_settings(self):
        previous = self.instrument.get()
        self.settings = engine.load_settings()
        self.instruments = engine.apply_settings(self.settings)
        labels = [i.label for i in self.instruments.values()]
        self.instr_combo.configure(values=labels)
        want_key = self.settings.get("default_instrument", "guitar")
        default_label = next((i.label for i in self.instruments.values()
                              if i.key == want_key), "Guitar")
        if previous in labels:
            self.instrument.set(previous)
        else:
            self.instrument.set(default_label)
        self.compact_var.set(bool(self.settings.get("compact_default", False)))
        self.style_var.set(STYLE_FROM_KEY.get(
            self.settings.get("tab_style", "asfound"), STYLE_LABELS[0]))
        if not self.out_var.get().strip():
            self.out_var.set(self.settings.get(
                "default_output_dir", str(Path.home() / "Documents" / "TABs")))

    def paste_into(self, widget):
        """Put the clipboard into a typing box at the cursor."""
        try:
            data = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("TABifier",
                                "There is nothing copied to paste.")
            return
        widget.insert("insert", data)
        widget.focus_set()

    # ------------------------------------------- dropped files and text
    def enable_drops(self):
        """Let files - and text dragged from a browser - be dropped on."""
        if HAVE_TKDND and hasattr(self.root, "drop_target_register"):
            targets = [(self.root, self.on_dnd),
                       (self.text,
                        lambda e: self.on_dnd(e, self.text)),
                       (self.chart_text,
                        lambda e: self.on_dnd(e, self.chart_text))]
            ok = False
            for widget, handler in targets:
                try:
                    widget.drop_target_register(DND_FILES, DND_TEXT)
                    widget.dnd_bind("<<Drop>>", handler)
                    ok = True
                except tk.TclError:
                    pass
            if ok:
                return
        if sys.platform != "win32":
            return
        try:
            import windnd
        except ImportError:
            return
        try:
            windnd.hook_dropfiles(self.root, func=self.on_drop,
                                  force_unicode=True)
        except Exception:
            pass                      # drag and drop is a convenience only

    def on_dnd(self, event, target=None):
        """One handler for both kinds of drop: a list of files, or text."""
        data = event.data or ""
        try:
            items = list(self.root.tk.splitlist(data))
        except tk.TclError:
            items = [data]
        if items and all(Path(i).exists() for i in items if i.strip()):
            self.accept_files(items)
        else:
            self.accept_text(data, target)
        return getattr(event, "action", None)

    def accept_text(self, text, target=None):
        """Text dragged in from a browser or another program."""
        text = (text or "").strip("{}").strip()
        if not text:
            return
        if target is not None:
            target.insert("insert", text)
            target.focus_set()
            self.nb.select(1 if target is self.text else 2)
            return
        try:
            mode = engine.sniff_text_mode(text)
        except Exception:
            mode = "tab"
        box = self.chart_text if mode == "chart" else self.text
        box.insert("insert", text)
        box.focus_set()
        self.nb.select(2 if mode == "chart" else 1)
        self.status.configure(
            text="Dropped text read as a %s."
                 % ("chord chart" if mode == "chart" else "TAB"))

    def on_drop(self, items):
        paths = []
        for item in items:
            if isinstance(item, bytes):
                item = item.decode(sys.getfilesystemencoding(), "replace")
            if item:
                paths.append(item)
        if paths:
            self.root.after(0, lambda: self.accept_files(paths))

    def accept_files(self, paths):
        """Take files from a drop, or from the command line, and put them
        where they belong: music in the file box, text in the right
        typing box."""
        picked = [Path(p) for p in paths if str(p).strip()]
        picked = [p for p in picked if p.exists()]
        if not picked:
            return
        music = [p for p in picked
                 if p.suffix.lower() in MUSIC_SUFFIXES]
        texts = [p for p in picked if p.suffix.lower() in TEXT_SUFFIXES]
        if music:
            self.selected_files = sorted((str(p) for p in music),
                                         key=natural_key)
            self.show_selection()
            self.nb.select(0)
            self.status.configure(
                text="Loaded %d file%s - press Make Score + TAB PDF."
                     % (len(music), "" if len(music) == 1 else "s"))
            return
        if not texts:
            messagebox.showinfo(
                "TABifier",
                "That file type is not one TABifier reads.\n\n"
                "Sheet music: PDF, MusicXML, or page images.\n"
                "TAB or chord charts: .txt or .tab")
            return
        path = texts[0]
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            messagebox.showerror("TABifier", "Cannot read that file:\n%s" % e)
            return
        try:
            mode = engine.sniff_text_mode(body)
        except Exception:
            mode = "tab"
        if mode == "chart":
            self.chart_text.delete("1.0", "end")
            self.chart_text.insert("1.0", body)
            if not self.chart_title_var.get().strip():
                self.chart_title_var.set(path.stem)
            self.nb.select(2)
            self.status.configure(text="Chord chart loaded from %s."
                                       % path.name)
        else:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", body)
            if not self.title_var.get().strip():
                self.title_var.set(path.stem)
            self.nb.select(1)
            self.status.configure(text="TAB loaded from %s." % path.name)

    def open_settings(self):
        SettingsDialog(self)

    def offer_setup(self):
        missing = [name for name, ok in engine.check_dependencies().items()
                   if not ok]
        if not missing:
            return
        wants = messagebox.askyesno(
            "TABifier setup",
            "TABifier needs MuseScore (free) to print the finished page:\n\n  • "
            + "\n  • ".join(missing)
            + "\n\n" + _helper_install_blurb() + "\n\n"
              "The scan reader is separate - TABifier will offer it if you "
              "open a photo of sheet music.")
        if wants:
            self.run_setup(omr=False)
        else:
            self.status.configure(
                text="MuseScore missing - printing will fail until it is "
                     "installed. Restart TABifier to try again.")

    def offer_omr_setup(self, retry=False):
        missing = [name for name, ok
                   in engine.check_dependencies(omr=True).items() if not ok]
        if not missing:
            return True
        wants = messagebox.askyesno(
            "TABifier setup",
            "Reading a scan of sheet music needs these free helpers:\n\n  • "
            + "\n  • ".join(missing)
            + "\n\n" + _helper_install_blurb()
            + " Typed TAB and chord charts do not need this.")
        if wants:
            self.run_setup(omr=True, retry=retry)
            return True
        self._pending_convert = None
        self._retry_after_setup = False
        self.status.configure(
            text="Scan reader missing - paste TAB or a chord chart, or "
                 "drop a MusicXML file, without it.")
        return False

    def run_setup(self, omr=False, retry=False):
        if self.running:
            return
        self._retry_after_setup = retry
        self.log_line("")
        self.log_line("=== Installing helper apps ==="
                      + (" (including the scan reader)" if omr else ""))
        args = ["--setup", "--omr"] if omr else ["--setup"]
        self.launch_setup(args)

    def instrument_obj(self):
        label = self.instrument.get()
        for i in self.instruments.values():
            if i.label == label:
                return i
        return engine.INSTRUMENTS["guitar"]

    # ------------------------------------------------------------- helpers
    def pick_file(self):
        files = filedialog.askopenfilenames(title="Choose sheet music "
                                            "(several images = pages)",
                                            filetypes=FILETYPES)
        if not files:
            return
        self.selected_files = sorted(files, key=natural_key)
        self.show_selection()

    def show_selection(self):
        n = len(self.selected_files)
        if n > 1:
            self.multi_label = (f"{n} pages: " +
                                ", ".join(Path(p).name for p in self.selected_files))
            self.file_var.set(self.multi_label)
            self.btn_pages.configure(state="normal")
        else:
            self.file_var.set(self.selected_files[0] if self.selected_files else "")
            self.btn_pages.configure(state="disabled")

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Choose the output folder",
                                    initialdir=self.out_var.get() or str(Path.home()))
        if d:
            self.out_var.set(d)

    def insert_template(self):
        lines = template_lines(self.instrument_obj())
        current = self.text.get("1.0", "end").rstrip()
        prefix = "\n\n" if current else ""
        self.text.insert("end", prefix + "\n".join(lines) + "\n")

    def log_line(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy, status=None):
        self.running = busy
        state = "disabled" if busy else "normal"
        self.btn_file.configure(state=state)
        self.btn_text.configure(state=state)
        self.btn_chart.configure(state=state)
        if status:
            self.status.configure(text=status)

    # ------------------------------------------------------------ converts
    def common_args(self):
        args = ["--instrument", self.instrument_obj().key]
        if self.capo_var.get() > 0:
            args += ["--capo", str(self.capo_var.get())]
        if self.compact_var.get():
            args.append("--compact")
        args += ["--tab-style", STYLE_KEYS.get(self.style_var.get(), "asfound")]
        return args

    def convert_file(self):
        if self.running:
            return
        entry = self.file_var.get().strip().strip('"')
        if len(self.selected_files) > 1 and entry == self.multi_label:
            paths = [Path(p) for p in self.selected_files]
        elif entry:
            paths = [Path(entry)]
        else:
            messagebox.showinfo("TABifier", "Choose a sheet music file first.")
            return
        for p in paths:
            if not p.exists():
                messagebox.showerror("TABifier", f"File not found:\n{p}")
                return
        if len(paths) > 1:
            # same shared-stem rule as the engine: "page-1" -> "page"
            stem = re.sub(r"[-_ ]*\d+$", "", paths[0].stem) or paths[0].stem
        else:
            stem = paths[0].stem
        scan = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
        if any(p.suffix.lower() in scan for p in paths):
            missing = [name for name, ok
                       in engine.check_dependencies(omr=True).items() if not ok]
            if missing:
                self._pending_convert = (
                    lambda: self.launch([str(p) for p in paths],
                                        paths[0].parent, stem))
                self.offer_omr_setup(retry=True)
                return
        self.launch([str(p) for p in paths], paths[0].parent, stem)

    def convert_text(self):
        if self.running:
            return
        text = self.text.get("1.0", "end")
        if text.count("-") < 10:
            messagebox.showinfo(
                "TABifier",
                "Type or paste some TAB first (use 'Insert empty TAB lines' "
                "for a starting point).")
            return
        title = self.title_var.get().strip()
        if not title:
            title = engine.guess_title(text) or "Untitled"
            self.title_var.set(title)
        safe = re.sub(r'[\\/:*?"<>|]', "", title).strip() or "Untitled"
        outdir = Path(self.out_var.get().strip() or
                      (Path.home() / "Documents" / "TABs"))
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("TABifier", f"Cannot create folder:\n{e}")
            return
        src = self._write_scratch(text)
        extra = ["--title", title, "--time", self.time_var.get().strip() or "4/4",
                 "--base", self.base_var.get()]
        self.launch([src] + extra, outdir, safe)

    def convert_chart(self):
        if self.running:
            return
        text = self.chart_text.get("1.0", "end")
        if not text.strip():
            messagebox.showinfo(
                "TABifier",
                "Paste a chord chart first - chord names above the lyrics.")
            return
        title = self.chart_title_var.get().strip()
        if not title:
            # the first real line of a pasted page is nearly always the song
            title = engine.guess_title(text) or "Untitled"
            self.chart_title_var.set(title)
        safe = re.sub(r'[\\/:*?"<>|]', "", title).strip() or "Untitled"
        outdir = Path(self.out_var.get().strip() or
                      (Path.home() / "Documents" / "TABs"))
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("TABifier", f"Cannot create folder:\n{e}")
            return
        src = self._write_scratch(text)
        self.launch([src, "--mode", "chart", "--title", title],
                    outdir, safe)

    def _write_scratch(self, text):
        """Typed TAB / charts go through a temp file, not the save folder."""
        self._clear_scratch()
        fd, name = tempfile.mkstemp(prefix="tabifier_", suffix=".txt")
        os.close(fd)
        Path(name).write_text(text, encoding="utf-8")
        self._scratch = name
        return name

    def _clear_scratch(self):
        if not self._scratch:
            return
        try:
            os.unlink(self._scratch)
        except OSError:
            pass
        self._scratch = None

    def launch(self, engine_args, outdir, stem):
        if stem.endswith("_TAB"):
            stem = stem[:-4]
        elif stem.endswith("_CTAB"):
            stem = stem[:-5]
        tag = "_CTAB" if self.compact_var.get() else "_TAB"
        self.last_pdf = Path(outdir) / f"{stem}{tag}.pdf"
        self.last_dir = Path(outdir)
        self.btn_open_pdf.configure(state="disabled")
        self.btn_open_dir.configure(state="disabled")
        self._last_convert = (list(engine_args), outdir, stem)
        self._need_omr = False
        self.set_busy(True, "Working...")
        self.log_line("")
        self.spawn_engine(engine_args + self.common_args()
                          + ["--out", str(outdir)])

    def launch_setup(self, engine_args):
        self.last_pdf = None
        self.last_dir = None
        self.set_busy(True, "Installing helpers...")
        self.spawn_engine(engine_args)

    def spawn_engine(self, tail):
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--engine"] + tail
        else:
            cmd = [sys.executable, "-u", str(ENGINE)] + tail
        threading.Thread(target=self.worker, args=(cmd,), daemon=True).start()

    def worker(self, cmd):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace",
                                    creationflags=flags)
            for line in proc.stdout:
                self.queue.put(("log", line))
            proc.wait()
            self.queue.put(("done", proc.returncode))
        except Exception as e:
            self.queue.put(("log", f"ERROR: {e}"))
            self.queue.put(("done", 1))

    def pump(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log_line(payload)
                    if "needs Audiveris" in payload:
                        self._need_omr = True
                elif kind == "done":
                    self._clear_scratch()
                    if self.last_pdf is None:
                        ok = payload == 0
                        retry = ok and self._retry_after_setup
                        pending = self._pending_convert if retry else None
                        self._retry_after_setup = False
                        self._pending_convert = None
                        self.set_busy(False, "Helpers installed - ready to go."
                                      if ok else "Setup incomplete - see the "
                                                 "log above.")
                        if pending:
                            pending()
                        continue
                    ok = payload == 0 and self.last_pdf.exists()
                    if not ok and self._need_omr:
                        self._need_omr = False
                        self.set_busy(False, "Scan reader needed - see above.")
                        if self._last_convert:
                            args, outdir, stem = self._last_convert
                            self._pending_convert = (
                                lambda a=args, d=outdir, s=stem:
                                self.launch(a, d, s))
                            self.offer_omr_setup(retry=True)
                        continue
                    self.set_busy(False, "Done - PDF ready." if ok
                                  else "Failed - see the log above.")
                    if ok:
                        self.btn_open_pdf.configure(state="normal")
                        self.btn_open_dir.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    # -------------------------------------------------------------- opens
    def open_pdf(self):
        if self.last_pdf and self.last_pdf.exists():
            open_os_path(self.last_pdf)

    def open_dir(self):
        if self.last_dir and self.last_dir.exists():
            open_os_path(self.last_dir)


def main():
    if "--engine" in sys.argv:
        # frozen TABifier.exe re-invokes itself in this mode for conversions
        i = sys.argv.index("--engine")
        sys.argv = [sys.argv[0]] + sys.argv[i + 1:]
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
        engine.main()
        return
    smoke = "--smoke" in sys.argv
    # anything else on the command line is a file dropped on the program
    # icon (or opened with TABifier)
    dropped = [a for a in sys.argv[1:] if not a.startswith("-")]
    root = TkinterDnD.Tk() if HAVE_TKDND else tk.Tk()
    app = TabMakerApp(root, smoke=smoke)
    if dropped:
        root.after(200, lambda: app.accept_files(dropped))
        report = os.environ.get("TABIFIER_SMOKE_REPORT")
        if report and smoke:
            def note_routing():
                with open(report, "a", encoding="utf-8") as fh:
                    print("dropped=%d" % len(dropped), file=fh)
                    print("routed_to_tab=%d" % app.nb.index("current"),
                          file=fh)
            root.after(600, note_routing)
    root.mainloop()


if __name__ == "__main__":
    main()
