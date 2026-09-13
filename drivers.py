#!/usr/bin/env python3
"""OCR drivers.

One function shape, several engines behind it:

    ocr(png_path) -> (text, confidence 0..1, engine_name)

Tiiny first, because this is a Tiiny app and the device is the point. But the
device is not the only machine in the house, and on the day GLM-OCR is wedged
you still want the corpus built - so every driver is interchangeable and the
Archiver picks the first one that answers.

Confidence matters more here than in ordinary OCR work. This text ends up in a
vault somebody consults when they cannot look anything up, and a page that
reads "50 mg" where the paper said "5 mg" is worse than a page that is missing.
Drivers that cannot report a real confidence return a conservative estimate and
say so, and the Archiver flags anything under the floor rather than trusting it.
"""
import json
import os
import re
import shutil
import subprocess
import urllib.request

TIINY_HOST = os.environ.get("TIINY_HOST", "")
TIINY_KEY = os.environ.get("TIINY_KEY", "")
TIINY_PORT = os.environ.get("TIINY_PORT", "80")  # :8800 is docker-bridge-only since 1.0.0
TIINY_OCR_MODEL = os.environ.get("TIINY_OCR_MODEL", "zai-org/GLM-OCR")

PROMPT = ("Transcribe every word of text on this page exactly as printed. "
          "Preserve paragraph breaks. Do not summarise, explain, or add "
          "commentary. Output only the transcription.")


class Unavailable(Exception):
    """This engine is not usable on this machine right now."""


# --------------------------------------------------------------- helpers
def _b64(path):
    import base64
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def _looks_like_prose(text):
    """A cheap, engine-independent sanity score.

    Real OCR failure on a scanned page does not come back empty, it comes back
    as confident nonsense. These three signals catch most of it: too few real
    words, too many stray symbols, and implausibly short output for a full page.
    """
    if not text or len(text) < 40:
        return 0.0
    words = re.findall(r"[A-Za-z]{2,}", text)
    if not words:
        return 0.0
    letters = sum(len(w) for w in words)
    junk = len(re.findall(r"[^\w\s.,;:'\"()\-—/%°$&!?\[\]]", text))
    ratio = letters / max(1, len(text))
    junk_ratio = junk / max(1, len(text))
    score = min(1.0, ratio * 1.35) * (1.0 - min(0.6, junk_ratio * 8))
    if len(words) < 25:
        score *= 0.6
    return round(max(0.0, min(1.0, score)), 3)


# ---------------------------------------------------------------- tiiny
def tiiny_available():
    if not (TIINY_HOST and TIINY_KEY):
        return False, "TIINY_HOST/TIINY_KEY not set"
    try:
        req = urllib.request.Request(
            f"http://{TIINY_HOST}:{TIINY_PORT}/v1/models",
            headers={"Authorization": f"Bearer {TIINY_KEY}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ids = [m.get("id") for m in json.load(r).get("data", [])]
    except Exception as exc:  # noqa: BLE001
        return False, f"device unreachable: {str(exc)[:60]}"
    if TIINY_OCR_MODEL not in ids:
        return False, f"{TIINY_OCR_MODEL} not loaded on the device"
    # Listed is not the same as served. Firmware 0.1.29 reports GLM-OCR running
    # in the management API while the gateway answers "not loaded", so the only
    # honest check is to actually ask it something.
    try:
        body = {"model": TIINY_OCR_MODEL, "max_tokens": 4,
                "messages": [{"role": "user", "content": "ok"}]}
        req = urllib.request.Request(
            f"http://{TIINY_HOST}:{TIINY_PORT}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {TIINY_KEY}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:  # noqa: BLE001
        return False, f"loaded but not serving ({str(exc)[:44]})"
    return True, "ready"


def tiiny_ocr(png):
    body = {"model": TIINY_OCR_MODEL, "max_tokens": 4000,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{_b64(png)}"}},
                {"type": "text", "text": PROMPT}]}]}
    req = urllib.request.Request(
        f"http://{TIINY_HOST}:{TIINY_PORT}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TIINY_KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        v = json.load(r)
    text = (v["choices"][0]["message"].get("content") or "").strip()
    return text, _looks_like_prose(text)


# ------------------------------------------------------------ tesseract
def tesseract_available():
    if not shutil.which("tesseract"):
        return False, "not installed (brew install tesseract)"
    return True, "ready"


def tesseract_ocr(png):
    """Tesseract reports a real per-word confidence, so we use it rather than
    the heuristic, and take the mean over words it actually found."""
    out = subprocess.run(["tesseract", png, "stdout", "--psm", "1", "-c",
                          "preserve_interword_spaces=1"],
                         capture_output=True, text=True, errors="replace", timeout=300)
    text = (out.stdout or "").strip()
    conf = _looks_like_prose(text)
    try:
        tsv = subprocess.run(["tesseract", png, "stdout", "--psm", "1", "tsv"],
                             capture_output=True, text=True, errors="replace", timeout=300).stdout
        vals = []
        for line in tsv.splitlines()[1:]:
            p = line.split("\t")
            if len(p) > 11 and p[11].strip() and p[10] not in ("-1", "conf"):
                try:
                    vals.append(float(p[10]))
                except ValueError:
                    pass
        if vals:
            conf = round(min(conf, sum(vals) / len(vals) / 100.0), 3)
    except Exception:  # noqa: BLE001
        pass
    return text, conf


# ------------------------------------------------------------ paddleocr
def paddle_available():
    try:
        import paddleocr  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "not installed (pip install paddleocr)"
    return True, "ready"


_PADDLE = None


def paddle_ocr(png):
    global _PADDLE
    from paddleocr import PaddleOCR
    if _PADDLE is None:
        _PADDLE = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    res = _PADDLE.ocr(png, cls=True) or []
    lines, confs = [], []
    for block in res:
        for item in (block or []):
            try:
                txt, cf = item[1][0], float(item[1][1])
            except Exception:  # noqa: BLE001
                continue
            lines.append(txt)
            confs.append(cf)
    text = "\n".join(lines).strip()
    conf = round(sum(confs) / len(confs), 3) if confs else _looks_like_prose(text)
    return text, conf


DRIVERS = {
    "tiiny":     (tiiny_available, tiiny_ocr),
    "tesseract": (tesseract_available, tesseract_ocr),
    "paddle":    (paddle_available, paddle_ocr),
}
ORDER = ["tiiny", "tesseract", "paddle"]


def probe():
    """What could run right now, and why the others cannot."""
    out = []
    for name in ORDER:
        avail, why = DRIVERS[name][0]()
        out.append((name, avail, why))
    return out


def pick(preferred=None):
    """First engine that actually answers. Never guesses."""
    order = ([preferred] if preferred else []) + [n for n in ORDER if n != preferred]
    tried = []
    for name in order:
        if name not in DRIVERS:
            raise SystemExit(f"unknown engine {name!r}; known: {', '.join(ORDER)}")
        avail, why = DRIVERS[name][0]()
        if avail:
            return name, DRIVERS[name][1]
        tried.append(f"{name}: {why}")
    raise Unavailable("no OCR engine is usable here.\n    " + "\n    ".join(tried))
