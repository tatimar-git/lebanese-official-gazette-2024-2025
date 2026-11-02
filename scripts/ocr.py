# ============================================================
# Arabic OCR of the downloaded Gazette pages
#
#   Runs automatically inside scraper.py (background thread).
#   Stand-alone, when the scraper is NOT running:
#     python scripts/ocr.py --year 2025                 # pending pages
#     python scripts/ocr.py --year 2025 --retry-failed  # + failed pages
#
# Engine: EasyOCR (Arabic + English), GPU when available, CPU
# otherwise. Pip-only install (no system program), same engine as
# gazette/ocr.py of the main project. The scans are 516 px wide:
# they are upscaled x2 before OCR. Boxes are rebuilt into lines in
# reading order (top -> bottom, right -> left for Arabic lines,
# left -> right for Latin lines).
#
# Output: <year>/ocr_text/<issue>/<year>_<issue>_part<P>_page<NNNNN>.txt
# (UTF-8, OCR text only, nothing invented: an empty page stays empty)
# and <year>/metadata/ocr_status.json (status, confidence, errors).
# ============================================================

import argparse
import glob
import os
import re
import subprocess
import threading
import time

import numpy as np
from PIL import Image

from common import Logger, YearPaths, load_json, now, save_json

ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
LATIN_RE = re.compile(r"[A-Za-z]")
SCALE = 2.0
GROUP = 32
MAX_OCR_ATTEMPTS = 2


# ------------------------------------------------------------
# Engines
# ------------------------------------------------------------

GPU_FAILS_BEFORE_PAUSE = 5      # GPU out of memory this many pages in a row ->
CPU_ONLY_PAGES = 50             # ... read the next pages on the CPU, then try the GPU again


def out_of_memory(error):
    return "out of memory" in str(error).lower()


class EasyOcrEngine:
    """GPU when available; a page that does not fit in the GPU memory is read on the CPU."""
    name = "easyocr"

    def __init__(self, log=print):
        import easyocr
        import torch
        self.easyocr = easyocr
        self.torch = torch
        self.log = log
        self.gpu = torch.cuda.is_available()
        self.gpu_reader = easyocr.Reader(["ar", "en"], gpu=True, verbose=False) if self.gpu else None
        self.cpu_reader = None if self.gpu else easyocr.Reader(["ar", "en"], gpu=False, verbose=False)
        self.gpu_fails = 0
        self.cpu_only_left = 0
        self.label = f"EasyOCR {'GPU' if self.gpu else 'CPU'}"
        self.last_device = None

    def _cpu(self):
        if self.cpu_reader is None:
            self.log("[ocr] loading a CPU copy of EasyOCR (used when the GPU is out of memory)")
            self.cpu_reader = self.easyocr.Reader(["ar", "en"], gpu=False, verbose=False)
        return self.cpu_reader

    def _free_gpu(self):
        import gc
        gc.collect()
        try:
            self.torch.cuda.empty_cache()
        except Exception:
            pass

    def read(self, array):
        if self.gpu and self.cpu_only_left <= 0:
            try:
                result = self._read_page(self.gpu_reader, array)
                self.gpu_fails = 0
                self.last_device = "GPU"
                return result
            except Exception as e:
                if not out_of_memory(e):
                    raise
                self.gpu_fails += 1
                self._free_gpu()
                if self.gpu_fails >= GPU_FAILS_BEFORE_PAUSE:
                    self.log(f"[ocr] GPU out of memory {self.gpu_fails} pages in a row: "
                             f"CPU for the next {CPU_ONLY_PAGES} pages")
                    self.cpu_only_left = CPU_ONLY_PAGES
                    self.gpu_fails = 0
        elif self.cpu_only_left > 0:
            self.cpu_only_left -= 1
        self.last_device = "CPU"
        return self._read_page(self._cpu(), array)

    def _read_page(self, reader, array):
        texts, confs = [], []
        try:
            for crop in reading_regions(array):
                text, boxes = self._read_region(reader, crop)
                if text:
                    texts.append(text)
                confs += boxes
        finally:
            if reader is self.gpu_reader:
                self._free_gpu()            # give the memory back between pages
        return "\n".join(texts), float(np.mean(confs)) if confs else 0.0

    def _read_region(self, reader, array):
        with self.torch.inference_mode():
            horizontal, free = reader.detect(array)
            # recognise lines of similar width together (faster LSTM, same text)
            boxes = sorted(horizontal[0], key=lambda b: (b[1] - b[0]) / max(1, b[3] - b[2]))
            results = []
            for i in range(0, len(boxes), GROUP):
                results += reader.recognize(array, horizontal_list=boxes[i:i + GROUP], free_list=[],
                                            batch_size=GROUP)
            if free[0]:
                results += reader.recognize(array, horizontal_list=[], free_list=free[0], batch_size=GROUP)
        return "\n".join(to_lines(results)), [c for _, _, c in results]


class TesseractEngine:
    """Optional: needs Tesseract installed + ara.traineddata (see README)."""
    name = "tesseract"

    def __init__(self, exe=None, tessdata=None):
        self.exe = exe or os.environ.get("TESSERACT_EXE", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        self.tessdata = tessdata or os.environ.get("TESSDATA_DIR")
        self.label = "Tesseract ara+eng"

    def read(self, array):
        texts = [self._read_region(crop) for crop in reading_regions(array)]
        return "\n".join(t for t in texts if t), None

    def _read_region(self, array):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            png = os.path.join(tmp, "page.png")
            Image.fromarray(array).save(png)
            cmd = [self.exe, png, "stdout", "-l", "ara+eng", "--psm", "4"]
            if self.tessdata:
                cmd += ["--tessdata-dir", self.tessdata]
            r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:])
        return r.stdout.decode("utf-8", "replace").strip()


def make_engine(name, log=print):
    return TesseractEngine() if name == "tesseract" else EasyOcrEngine(log)


def to_lines(results):
    boxes = []
    for box, text, conf in results:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        if text.strip():
            boxes.append({"top": min(ys), "bottom": max(ys), "left": min(xs), "right": max(xs),
                          "text": text.strip()})
    boxes.sort(key=lambda b: (b["top"] + b["bottom"]) / 2)
    lines, current = [], []
    for b in boxes:
        mid = (b["top"] + b["bottom"]) / 2
        if current:
            ref = current[-1]
            height = max(8, min(b["bottom"] - b["top"], ref["bottom"] - ref["top"]))
            if abs(mid - (ref["top"] + ref["bottom"]) / 2) > 0.5 * height:
                lines.append(current)
                current = []
        current.append(b)
    if current:
        lines.append(current)
    out = []
    for line in lines:
        text = " ".join(b["text"] for b in line)
        latin_line = len(LATIN_RE.findall(text)) > len(ARABIC_RE.findall(text))
        line.sort(key=lambda b: b["left"] if latin_line else -b["right"])
        out.append(" ".join(b["text"] for b in line))
    return out


# ------------------------------------------------------------
# Page layout: Part 1 pages are often two columns (Arabic: right
# column first). A column gutter = a vertical band in the middle
# of the page with no ink on most text rows. Rows where the gutter
# has ink (titles, rules, full-width text) are read across the
# whole width; between them, right column then left column.
# ------------------------------------------------------------

def reading_regions(array):
    ink = array < 128
    h, w = ink.shape
    text_rows = ink.any(axis=1)
    if text_rows.sum() < 50:
        return [array]
    best_x, best = None, None
    for x in range(int(w * 0.35), int(w * 0.65), 2):
        band = ink[:, x - 4:x + 4].any(axis=1)
        score = int(band.sum())
        if best is None or score < best:
            best_x, best = x, score
    gutter_ink = ink[:, best_x - 4:best_x + 4].any(axis=1)
    left_ink, right_ink = ink[:, :best_x].sum(), ink[:, best_x:].sum()
    total = left_ink + right_ink
    if best > 0.25 * text_rows.sum() or min(left_ink, right_ink) < 0.2 * total:
        return [array]                                   # single column

    # rows: True = full-width (gutter has ink); grow by a few px so a title is cut cleanly
    span = gutter_ink.copy()
    pad = 6
    for y in np.flatnonzero(gutter_ink):
        span[max(0, y - pad):y + pad] = True
    segments, start = [], 0
    for y in range(1, h + 1):
        if y == h or span[y] != span[start]:
            segments.append((start, y, bool(span[start])))
            start = y
    # a band is really two columns only if it is tall (several lines) and BOTH
    # sides have text on most of its rows; otherwise it is read full width
    def two_columns(y0, y1):
        rows = text_rows[y0:y1]
        if y1 - y0 < 80 or rows.sum() == 0:
            return False
        left = ink[y0:y1, :best_x - 4].any(axis=1)[rows].mean()
        right = ink[y0:y1, best_x + 4:].any(axis=1)[rows].mean()
        return left >= 0.5 and right >= 0.5

    marked = [(y0, y1, not is_span and two_columns(y0, y1)) for y0, y1, is_span in segments]
    column_rows = sum(text_rows[y0:y1].sum() for y0, y1, cols in marked if cols)
    if column_rows < 0.3 * text_rows.sum():
        return [array]
    regions, run = [], None                     # consecutive full-width bands are merged
    for y0, y1, cols in marked:
        if cols:
            if run:
                regions.append(array[run[0]:run[1]])
                run = None
            regions.append(array[y0:y1, best_x:])            # right column first
            regions.append(array[y0:y1, :best_x])
        else:
            run = (run[0], y1) if run else (y0, y1)
    if run:
        regions.append(array[run[0]:run[1]])
    return [r for r in regions if (r < 128).any()] or [array]


def load_page(path):
    image = Image.open(path).convert("L")
    image = image.resize((int(image.width * SCALE), int(image.height * SCALE)), Image.LANCZOS)
    return np.array(image)


# ------------------------------------------------------------
# Worker (one per year; used by scraper.py and by this script)
# ------------------------------------------------------------

class OcrWorker:
    def __init__(self, paths, log, engine="easyocr", retry_failed=False):
        self.paths = paths
        self.log = log
        self.engine_name = engine
        self.engine = None
        self.retry_failed = retry_failed
        self.state_path = os.path.join(paths.metadata, "ocr_status.json")
        self.state = load_json(self.state_path, {}) or {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.draining = threading.Event()
        self.thread = None

    def state_snapshot(self):
        with self.lock:
            return dict(self.state)

    def pending(self):
        """Downloaded pages without OCR text, in issue / page order."""
        out = []
        for f in sorted(glob.glob(os.path.join(self.paths.issues_dir, "*.json"))):
            rec = load_json(f) or {}
            for p in rec.get("pages", []):
                if p["status"] != "downloaded":
                    continue
                st = self.state.get(p["image"], {})
                if st.get("status") == "done" and os.path.exists(self.paths.abs(p["text"])):
                    continue
                # GPU out-of-memory failures are retried (the CPU fallback reads them now)
                limit = MAX_OCR_ATTEMPTS + 3 if out_of_memory(st.get("error")) else MAX_OCR_ATTEMPTS
                if st.get("status") == "failed" and not self.retry_failed \
                        and st.get("attempts", 0) >= limit:
                    continue
                if os.path.exists(self.paths.abs(p["image"])):
                    out.append((rec["issue_id"], p))
        return out

    def process(self, issue_id, p):
        if self.engine is None:
            self.log(f"[ocr] loading {self.engine_name} (first run downloads its models)...")
            self.engine = make_engine(self.engine_name, self.log)
            self.log(f"[ocr] engine ready: {self.engine.label}")
        key = p["image"]
        prev = self.state.get(key, {})
        started = time.perf_counter()
        try:
            text, conf = self.engine.read(load_page(self.paths.abs(p["image"])))
            path = self.paths.abs(p["text"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            label = self.engine.label
            if getattr(self.engine, "last_device", None):
                label = f"EasyOCR {self.engine.last_device}"
            entry = {"status": "done", "engine": label, "confidence": conf,
                     "chars": len(text), "arabic_chars": len(ARABIC_RE.findall(text)),
                     "empty": not text.strip(), "text": p["text"],
                     "seconds": round(time.perf_counter() - started, 1), "at": now(),
                     "attempts": prev.get("attempts", 0) + 1, "error": None}
        except Exception as e:                          # recorded, retried later
            entry = {"status": "failed", "engine": self.engine_name, "error": f"{type(e).__name__}: {e}",
                     "at": now(), "attempts": prev.get("attempts", 0) + 1}
            self.log(f"[ocr] FAILED {key}: {entry['error']}")
        with self.lock:
            self.state[key] = entry
            save_json(self.state_path, self.state)
        return entry

    def loop(self):
        while not self.stopping.is_set():
            try:
                todo = self.pending()
            except OSError as e:                     # issue file being rewritten by the scraper
                self.log(f"[ocr] could not read the issue files ({e}), retrying in 30 s")
                self.wake.wait(30)
                continue
            if not todo:
                if self.draining.is_set():
                    return
                self.wake.wait(30)
                self.wake.clear()
                continue
            for issue_id, p in todo:
                if self.stopping.is_set():
                    return
                e = self.process(issue_id, p)
                if e["status"] == "done":
                    conf = f"{e['confidence']:.2f}" if e["confidence"] is not None else "-"
                    print(f"  [ocr] {issue_id} part{p['part']} page {p['page']}: {e['chars']} chars, "
                          f"conf {conf}, {e['seconds']}s", flush=True)

    # --- thread control (scraper.py) ---
    def start(self):
        self.thread = threading.Thread(target=self._safe_loop, daemon=True, name="ocr")
        self.thread.start()

    def _safe_loop(self):
        try:
            self.loop()
        except Exception as e:
            self.log(f"[ocr] worker stopped by an error: {type(e).__name__}: {e} "
                     f"(downloads continue; run scripts/ocr.py later)")

    def notify(self):
        self.wake.set()

    def finish(self):
        """Process what is left, then return."""
        self.draining.set()
        self.wake.set()
        while self.thread.is_alive():
            self.thread.join(1)

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(120)


def main():
    ap = argparse.ArgumentParser(description="OCR the downloaded pages of one year (scraper not running).")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--engine", default="easyocr", choices=["easyocr", "tesseract"])
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--limit", type=int, help="test: OCR at most N pages")
    args = ap.parse_args()
    paths = YearPaths(args.year)
    log = Logger(paths.ocr_log)
    w = OcrWorker(paths, log, engine=args.engine, retry_failed=args.retry_failed)
    todo = w.pending()
    if args.limit:
        todo = todo[:args.limit]
    log(f"[ocr] {args.year}: {len(todo)} pages to OCR")
    try:
        for n, (issue_id, p) in enumerate(todo, 1):
            e = w.process(issue_id, p)
            if e["status"] == "done":
                print(f"  {n}/{len(todo)} {issue_id} part{p['part']} page {p['page']}: {e['chars']} chars, "
                      f"{e['seconds']}s", flush=True)
    except KeyboardInterrupt:
        log("[ocr] stopped (progress saved; run the same command to continue)")
    import progress
    pr = progress.compute(paths)
    old = load_json(paths.progress, {}) or {}
    pr["status"] = old.get("status", "ocr_only")
    save_json(paths.progress, pr)
    log(f"[ocr] done: {pr['ocr_pages_completed']} pages with text, {pr['ocr_failures']} failures")


if __name__ == "__main__":
    main()
