"""Add Sorcery TCG cards to your sorcerytcg.com collection from a text file.

Usage:
    python sorcery_collector.py FILE --set SET [--foil] [-d | --dry-run] [--cleanup]

    FILE              text file with one (partial) card name per line (format below)
    --set SET         set to add the cards from, by name or code, e.g. beta, alpha,
                      "arthurian legends", gothic. Required.
    --foil            add the foil printings instead of the non-foil ones
    -d, --dry-run     only match the cards and show what would be added;
                      no browser is opened and nothing is added
    --cleanup         delete lines from FILE once their cards have been added
                      successfully, so a rerun only retries the failures

Examples:
    python sorcery_collector.py cards.txt --set beta
    python sorcery_collector.py cards.txt --set "arthurian" --foil
    python sorcery_collector.py cards.txt --set alpha -d
    python sorcery_collector.py cards.txt --set beta --cleanup

Card file format (one card per line, partial names are fine):
    scarecrow
    crave go
    2 sunken trea        <- a leading number is the number of copies
    magnetic mu
Blank lines and lines starting with '#' are ignored. For a card whose name
starts with a number, give the count explicitly: "1 13 treasures".

All lines are matched first and listed in a window for review; nothing is
added until you click "Add ... to collection". Double-click a row to change
its quantity, fix a typo or pick a different printing. Rows that fail can be
fixed the same way and added with another click.

On the first run a browser window opens; log in to sorcerytcg.com there.
The login is kept in ./browser_profile for later runs.

Requirements (checked at startup; anything missing is reported with how to get it):
    Python 3.10+ with tkinter
    pip install playwright
    playwright install firefox     (not needed for --dry-run)
"""

import argparse
import json
import queue
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info < (3, 10):
    raise SystemExit(f"Python 3.10 or newer is needed; this is Python {sys.version.split()[0]}.\n"
                     "Get a newer one from https://www.python.org/downloads/")
try:
    import tkinter as tk
    from tkinter import simpledialog, ttk
except ImportError:
    raise SystemExit("tkinter, which draws the importer window, is missing from this Python.\n"
                     "Reinstall Python from https://www.python.org/downloads/ with "
                     "\"tcl/tk and IDLE\" ticked.")

HERE = Path(__file__).resolve().parent
SITE = "https://sorcerytcg.com"
CARDS_URL = "https://api.sorcerytcg.com/api/cards"
CARDS_CACHE = HERE / "cards_cache.json"
PROFILE_DIR = HERE / "browser_profile"
CACHE_MAX_AGE = 24 * 3600


# --------------------------------------------------------------------------
# Card data and matching
# --------------------------------------------------------------------------

@dataclass
class Printing:
    id: str
    card_id: str
    card_name: str
    set_name: str
    finish: str
    product: str
    type: str
    rarity: str

    def label(self):
        return (f"{self.card_name}   —   {self.set_name} · {self.product} · {self.finish}"
                f"   ({self.type}, {self.rarity})")


@dataclass
class Entry:
    line_no: int
    text: str
    partial: str
    qty: int
    candidates: list = field(default_factory=list)
    chosen: Printing | None = None


def load_cards():
    fresh = CARDS_CACHE.exists() and time.time() - CARDS_CACHE.stat().st_mtime < CACHE_MAX_AGE
    if fresh:
        return json.loads(CARDS_CACHE.read_text(encoding="utf-8"))
    try:
        req = urllib.request.Request(CARDS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        CARDS_CACHE.write_bytes(data)
        return json.loads(data)
    except OSError as e:
        if CARDS_CACHE.exists():
            print(f"Could not refresh card list ({e}); using cached copy.", file=sys.stderr)
            return json.loads(CARDS_CACHE.read_text(encoding="utf-8"))
        raise


def resolve_set(cards, wanted):
    sets = {}
    for card in cards:
        for p in card["printings"]:
            sets[p["set"]["code"]] = p["set"]["name"]
    w = wanted.strip().lower()
    exact = [c for c, n in sets.items() if w in (c.lower(), n.lower())]
    partial = exact or [c for c, n in sets.items() if w in n.lower()]
    if len(partial) == 1:
        return sets[partial[0]], partial[0]
    available = ", ".join(sorted(sets.values()))
    if partial:
        raise SystemExit(f"--set '{wanted}' is ambiguous. Available sets: {available}")
    raise SystemExit(f"--set '{wanted}' not found. Available sets: {available}")


def build_printings(cards, set_code, finish):
    out = []
    for card in cards:
        engine = card.get("engine") or {}
        for p in card["printings"]:
            if p["set"]["code"] == set_code and p["meta"]["finish"] == finish:
                out.append(Printing(
                    id=p["id"], card_id=card["id"], card_name=card["name"],
                    set_name=p["set"]["name"], finish=finish, product=p["meta"]["product"],
                    type=engine.get("type") or "?", rarity=engine.get("rarity") or "?",
                ))
    return out


def norm(s):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def find_candidates(printings, partial):
    p = norm(partial)
    if not p:
        return []
    exact = [x for x in printings if norm(x.card_name) == p]
    matches = exact or [x for x in printings if p in norm(x.card_name)]
    if not matches:
        tokens = p.split()
        matches = [x for x in printings if all(t in norm(x.card_name) for t in tokens)]
    return sorted(matches, key=lambda x: (x.card_name, x.product != "Booster", x.product))


def parse_line(text):
    m = re.match(r"^(\d+)\s*[xX]?\s+(.+)$", text)
    return (int(m.group(1)), m.group(2).strip()) if m else (1, text)


def parse_file(path):
    entries = []
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        qty, partial = parse_line(text)
        entries.append(Entry(line_no, text, partial, qty))
    return entries


class CardFile:
    """Deletes successfully added lines from the card file (--cleanup).

    Works from the original lines, so line numbers stay valid as lines are
    removed. If the file is changed by something else meanwhile, it is left
    untouched rather than overwriting those edits.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.lines = self.data.decode("utf-8").splitlines(keepends=True)
        self.removed = set()

    def remove(self, line_no):
        if self.path.read_bytes() != self.data:
            return False
        self.removed.add(line_no)
        text = "".join(line for n, line in enumerate(self.lines, 1) if n not in self.removed)
        self.data = text.encode("utf-8")
        self.path.write_bytes(self.data)
        return True


# --------------------------------------------------------------------------
# Browser / site access (runs entirely in the worker thread)
# --------------------------------------------------------------------------

class TrpcError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code


# Calls the site's own tRPC API from inside the logged-in page, so the
# browser's session cookies are used exactly as when clicking in the UI.
TRPC_JS = """async ([path, input, isMutation]) => {
  const body = JSON.stringify({json: input});
  const headers = {"x-trpc-source": "nextjs-react"};
  const res = isMutation
    ? await fetch(`/api/trpc/${path}`, {method: "POST", credentials: "include",
        headers: {...headers, "content-type": "application/json"}, body})
    : await fetch(`/api/trpc/${path}?input=${encodeURIComponent(body)}`,
        {credentials: "include", headers});
  return {status: res.status, text: await res.text()};
}"""


class SiteClient:
    def __init__(self, context):
        self.context = context

    def _page(self):
        for page in self.context.pages:
            if page.url.startswith(SITE):
                return page
        return None

    def call(self, path, payload, mutation=False):
        page = self._page()
        if page is None:
            raise TrpcError("NO_PAGE", "no sorcerytcg.com tab is open")
        res = page.evaluate(TRPC_JS, [path, payload, mutation])
        try:
            data = json.loads(res["text"])
        except ValueError:
            raise TrpcError(f"HTTP {res['status']}", res["text"][:200])
        if isinstance(data, list):
            data = data[0]
        if "error" in data:
            err = data["error"].get("json", data["error"])
            raise TrpcError((err.get("data") or {}).get("code"), err.get("message", ""))
        result = data["result"].get("data")
        if isinstance(result, dict) and "json" in result:
            result = result["json"]
        return result

    def quantity(self, printing):
        result = self.call("inventory.getQuantities", {"cardIds": [printing.card_id]}) or {}
        return int((result.get(printing.id) or {}).get("quantity", 0))

    def set_quantity(self, printing, quantity):
        result = self.call("inventory.updateCard",
                           {"printingId": printing.id, "quantity": quantity}, mutation=True)
        if isinstance(result, dict) and "quantity" in result:
            return result["quantity"]
        return quantity

    def wait_for_login(self, probe_card_id, on_status):
        on_status("Checking the saved Sorcery login in the importer Firefox window…")
        last_status = None
        while True:
            try:
                self.call("inventory.getQuantities", {"cardIds": [probe_card_id]})
                return
            except TrpcError as e:
                if e.code in ("UNAUTHORIZED", "FORBIDDEN"):
                    status = (
                        "Login required in the Firefox window opened by the importer. "
                        "It reuses the saved browser_profile session when the login "
                        "is still valid."
                    )
                else:
                    status = f"Cannot verify the Sorcery session; retrying: {e}"
            except Exception as e:
                # Page navigating mid-call (e.g. during the login redirect).
                if "closed" in str(e).lower():
                    raise
                status = f"Login check failed; retrying: {e}"
            if status != last_status:
                on_status(status)
                last_status = status
            time.sleep(2)


def check_browser():
    """Exit with install instructions if Playwright or its Firefox is missing."""
    python = f'"{sys.executable}"'
    install_firefox = f"    {python} -m playwright install firefox"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("The Playwright package, which drives Firefox, is not installed. Install it with:\n"
                         f"    {python} -m pip install playwright\n{install_firefox}")
    try:
        with sync_playwright() as pw:
            firefox = Path(pw.firefox.executable_path)
    except Exception as e:
        raise SystemExit(f"Playwright is installed but could not start ({e}). Reinstall it with:\n"
                         f"    {python} -m pip install --force-reinstall playwright\n{install_firefox}")
    if not firefox.exists():
        raise SystemExit("Playwright's Firefox is not installed (a normal Firefox install is not used). "
                         f"Install it with:\n{install_firefox}")


def browser_worker(jobs, ui, probe_card_id):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context = pw.firefox.launch_persistent_context(
                str(PROFILE_DIR), headless=False, no_viewport=True)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(f"{SITE}/collection")
            ui.browser_status(
                f"Using saved Firefox profile: {PROFILE_DIR}. "
                "If sign-in is needed, use this importer-opened Firefox window."
            )
            client = SiteClient(context)
            client.wait_for_login(probe_card_id, ui.browser_status)
            ui.logged_in()
            # Runs until the window is closed, so cards can be added in several rounds.
            while (job := jobs.get()) is not None:
                idx, printing, qty = job
                try:
                    current = client.quantity(printing)
                    new = client.set_quantity(printing, current + qty)
                    ui.entry_status(idx, f"Added {qty} (now {new})", "added")
                except Exception as e:
                    ui.entry_status(idx, f"Error: {e}", "error")
                if jobs.empty():
                    ui.done()
            context.close()
    except Exception as e:
        ui.browser_failed(f"Browser error: {e}")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class ChoiceDialog(tk.Toplevel):
    def __init__(self, parent, entry):
        super().__init__(parent)
        self.choice = None
        self.title(f"Choose card for “{entry.partial}”")
        self.transient(parent)
        self.geometry("+%d+%d" % (parent.winfo_rootx() + 60, parent.winfo_rooty() + 60))

        ttk.Label(self, padding=(12, 10, 12, 4), font=("Segoe UI", 11, "bold"),
                  text=f"Line {entry.line_no}: “{entry.text}” — {entry.qty} cop"
                       f"{'y' if entry.qty == 1 else 'ies'}").pack(anchor="w")
        ttk.Label(self, padding=(12, 0, 12, 8),
                  text=f"{len(entry.candidates)} matching printings. Click the right one:").pack(anchor="w")

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=12)
        canvas = tk.Canvas(outer, highlightthickness=0,
                           height=min(len(entry.candidates) * 36, 480), width=640)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        if len(entry.candidates) * 36 > 480:
            bar.pack(side="right", fill="y")
            self.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta // 120, "units"))

        for printing in entry.candidates:
            ttk.Button(inner, text=printing.label(),
                       command=lambda p=printing: self.pick(p)).pack(fill="x", pady=2)

        ttk.Button(self, text="Skip this line", command=self.destroy).pack(pady=10)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.focus_set()

    def pick(self, printing):
        self.choice = printing
        self.destroy()


class App:
    def __init__(self, root, entries, title, dry_run, match, card_file=None):
        self.root = root
        self.entries = entries
        self.dry_run = dry_run
        self.match = match
        self.card_file = card_file
        self.jobs = queue.Queue()
        self.ui_queue = queue.Queue()
        self.browser_ready = False
        self.browser_dead = False
        self.adding = False

        root.title("Sorcery collection importer")
        root.geometry("980x560")
        ttk.Label(root, text=title, padding=(10, 8, 10, 0), font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(root, padding=(10, 2, 10, 8),
                  text="Double-click a row to change its quantity, fix the name or pick a different "
                       "printing.").pack(anchor="w")

        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True, padx=10)
        cols = ("line", "qty", "input", "card", "status")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col, text, width in [("line", "Line", 50), ("qty", "Qty", 45), ("input", "Input", 180),
                                 ("card", "Card", 380), ("status", "Status", 300)]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=col in ("card", "status"),
                             anchor="center" if col in ("line", "qty") else "w")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for tag, color in [("added", "#1b7f3b"), ("error", "#b3261e"), ("missing", "#b3261e"),
                           ("skipped", "#8a6d00"), ("queued", "#666666")]:
            self.tree.tag_configure(tag, foreground=color)
        for i, e in enumerate(entries):
            self.tree.insert("", "end", iid=str(i), values=(e.line_no, e.qty, e.partial, "", "Pending"))
        self.tree.bind("<Double-1>", self.on_double_click)

        bottom = ttk.Frame(root, padding=(10, 8))
        bottom.pack(fill="x")
        self.add_button = ttk.Button(bottom, command=self.add_cards, state="disabled",
                                     text="Add cards to collection")
        if not dry_run:
            self.add_button.pack(side="right", padx=(10, 0), ipadx=8, ipady=4)
        self.status_var = tk.StringVar(value="Dry run — nothing will be added" if dry_run else "Starting browser…")
        ttk.Label(bottom, textvariable=self.status_var, wraplength=760).pack(anchor="w")
        self.summary_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.summary_var).pack(anchor="w")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)

    # Thread-safe hooks used by the worker.
    def browser_status(self, text):
        self.ui_queue.put(lambda: self.status_var.set(text))

    def logged_in(self):
        self.ui_queue.put(self._logged_in)

    def _logged_in(self):
        self.browser_ready = True
        self.status_var.set("Logged in — adding cards…" if self.adding else
                            "Logged in. Check the list, then click the Add button.")

    def browser_failed(self, text):
        self.ui_queue.put(lambda: self._browser_failed(text))

    def _browser_failed(self, text):
        self.browser_ready = False
        self.browser_dead = True
        self.adding = False
        for i in self.rows_tagged("queued"):
            self.set_row(i, status="Not added — browser stopped", tag="error")
        self.status_var.set(f"{text} — restart the importer to add cards.")
        self.refresh_summary()

    def entry_status(self, idx, text, tag=""):
        self.ui_queue.put(lambda: self.set_row(idx, status=self.cleanup(idx, text, tag), tag=tag))

    def cleanup(self, idx, status, tag):
        if tag != "added" or self.card_file is None:
            return status
        try:
            if self.card_file.remove(self.entries[idx].line_no):
                return f"{status} · removed from file"
            return f"{status} · file changed on disk, line kept"
        except OSError as e:
            return f"{status} · could not update file: {e}"

    def done(self):
        self.ui_queue.put(self._done)

    def _done(self):
        self.adding = False
        if self.rows_tagged("error", "missing", "skipped"):
            self.status_var.set("Finished adding. Double-click a red or yellow row to fix it and click "
                                "Add again, or close the window when done.")
        else:
            self.status_var.set("Finished adding — close the window when done.")
        self.refresh_summary()

    def pump(self):
        while not self.ui_queue.empty():
            self.ui_queue.get()()
        self.root.after(100, self.pump)

    def set_row(self, idx, qty=None, partial=None, card=None, status=None, tag=None):
        values = list(self.tree.item(str(idx), "values"))
        if qty is not None:
            values[1] = qty
        if partial is not None:
            values[2] = partial
        if card is not None:
            values[3] = card
        if status is not None:
            values[4] = status
        self.tree.item(str(idx), values=values, tags=(tag,) if tag else ())
        self.tree.see(str(idx))
        self.refresh_summary()

    def rows_tagged(self, *tags):
        return [i for i in range(len(self.entries)) if set(tags) & set(self.tree.item(str(i), "tags"))]

    def resolve_all(self):
        try:
            self._resolve_all()
        except tk.TclError:
            pass  # main window closed while a choice dialog was open

    def _resolve_all(self):
        for i in range(len(self.entries)):
            self.resolve_entry(i)

    def resolve_entry(self, i):
        entry = self.entries[i]
        entry.chosen = None
        if not entry.candidates:
            self.set_row(i, card="", status="Not found — double-click to edit", tag="missing")
            return
        if len(entry.candidates) == 1:
            entry.chosen = entry.candidates[0]
        else:
            self.set_row(i, status="Waiting for your choice…")
            dialog = ChoiceDialog(self.root, entry)
            self.root.wait_window(dialog)
            entry.chosen = dialog.choice
        if entry.chosen is None:
            self.set_row(i, card="", status="Skipped — double-click to edit", tag="skipped")
        else:
            self.set_row(i, card=entry.chosen.label(), tag="ready",
                         status=f"{'Would add' if self.dry_run else 'Ready to add'} {entry.qty}")

    def add_cards(self):
        rows = self.rows_tagged("ready", "error")
        if not rows:
            return
        self.adding = True
        for i in rows:
            entry = self.entries[i]
            self.set_row(i, status="Queued", tag="queued")
            self.jobs.put((i, entry.chosen, entry.qty))
        self.status_var.set("Adding cards…" if self.browser_ready else
                            "The cards will be added once you are logged in in the Firefox window…")

    def on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row or {"queued", "added"} & set(self.tree.item(row, "tags")):
            return
        i = int(row)
        entry = self.entries[i]
        text = simpledialog.askstring(
            "Edit entry",
            f"Line {entry.line_no}: edit the card name.\n"
            "A leading number is the number of copies.",
            initialvalue=entry.text, parent=self.root)
        if not text or not text.strip():
            return
        entry.text = text.strip()
        entry.qty, entry.partial = parse_line(entry.text)
        entry.candidates = self.match(entry.partial)
        self.set_row(i, qty=entry.qty, partial=entry.partial)
        self.resolve_entry(i)

    def refresh_summary(self):
        counts = {}
        for i in range(len(self.entries)):
            tags = self.tree.item(str(i), "tags")
            key = tags[0] if tags else "pending"
            counts[key] = counts.get(key, 0) + 1
        self.summary_var.set(", ".join(f"{n} {k}" for k, n in counts.items()))
        copies = sum(self.entries[i].qty for i in self.rows_tagged("ready", "error"))
        self.add_button.configure(
            text=f"Add {copies} card{'' if copies == 1 else 's'} to collection" if copies
            else "Add cards to collection",
            state="normal" if copies and not self.browser_dead else "disabled")

    def close(self):
        self.jobs.put(None)
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description="Add cards from a text file to your sorcerytcg.com collection.")
    ap.add_argument("file", help="text file with one (partial) card name per line")
    ap.add_argument("--set", required=True, help="set name or code, e.g. beta, alpha, 'arthurian legends', gothic")
    ap.add_argument("--foil", action="store_true", help="add the foil printings (default: non-foil)")
    ap.add_argument("-d", "--dry-run", action="store_true", help="only show what would be added; no browser")
    ap.add_argument("--cleanup", action="store_true",
                    help="delete lines from the card file once they have been added successfully")
    args = ap.parse_args()
    if not args.dry_run:
        check_browser()

    cards = load_cards()
    set_name, set_code = resolve_set(cards, args.set)
    finish = "Foil" if args.foil else "Standard"
    printings = build_printings(cards, set_code, finish)
    entries = parse_file(args.file)
    if not entries:
        raise SystemExit(f"No card lines found in {args.file}")
    for entry in entries:
        entry.candidates = find_candidates(printings, entry.partial)

    root = tk.Tk()
    app = App(root, entries, f"{Path(args.file).name}  →  {set_name}, {finish}", args.dry_run,
              match=lambda partial: find_candidates(printings, partial),
              card_file=CardFile(args.file) if args.cleanup and not args.dry_run else None)
    if not args.dry_run:
        threading.Thread(target=browser_worker, args=(app.jobs, app, printings[0].card_id),
                         daemon=True).start()
    root.after(200, app.resolve_all)
    root.mainloop()


if __name__ == "__main__":
    main()
