#!/usr/bin/env python3
"""Two hosts talking about your notes, out loud, from passages they can cite.

    python3 overview.py shelf "Claude Code"
    python3 overview.py entity Richard
    python3 overview.py corpus
    python3 overview.py list
    python3 overview.py unload            release the TTS model

An audio overview is the one thing people remember about NotebookLM, and the
reason is not the voices. It is that listening is the only way to read a corpus
you are never going to sit down and read. The cockpit already draws what your
notes are about; this says it.

Everything that makes the Archivist trustworthy has to survive the format, and
the format fights it. A cited answer shows its working on the page and the
reader can look. Speech cannot: it goes past once, it sounds confident whatever
it says, and nobody stops a podcast to check a claim. So the grounding here is
mechanical rather than asked for, in the same shape as archivist.ask():

  Passages are retrieved first, by the machinery that already exists, and the
  hosts are given nothing else.
  Every spoken line must end with the passage it came from.
  A line that cites nothing, or cites a passage that was not sent, is deleted
  before it is ever synthesised.

That last one is the load-bearing part. An instruction to cite is a request and
can be ignored silently; a filter is a rule. What gets deleted is counted and
reported, because a script that lost half its lines is a script that was mostly
invention and you want to know that.

Two constraints from the device shape the whole design. It does one inference at
a time, and it caps a single request at about 220 seconds. So nothing here is
one big call: the script is written a section at a time, and each spoken line is
its own synthesis request. Measured on this device the TTS runs at about 1.6x
realtime, so the 600 character speech cap below is roughly 25 seconds of work
against a 220 second ceiling. That margin is deliberate. The device is shared.
"""
import io
import json
import os
import pathlib
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

import archive
import entities

# Two voices the device actually ships, checked against
# /v1/capabilities?service=tts rather than guessed. The supported list is
# aiden, dylan, eric, ono_anna, ryan, serena, sohee, uncle_fu, vivian; an
# unsupported name comes back as a 500 with "Unsupported speaker", not a
# fallback, so this is not a place to be creative.
VOICES = {"A": os.environ.get("BRAIN_VOICE_A", "ryan"),
          "B": os.environ.get("BRAIN_VOICE_B", "serena")}
TTS_MODEL = os.environ.get("BRAIN_TTS", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")

# One synthesis request per this many characters at most. The device caps a
# request at ~220s and runs at ~1.6x realtime, which puts the real ceiling near
# 5,000 characters; this sits an order of magnitude under it so a busy device
# still finishes. Longer lines are split on sentence ends and stitched back.
TTS_CHARS = int(os.environ.get("BRAIN_TTS_CHARS", "600"))

SECTIONS = 5          # topics the overview moves through
PER_SECTION = 8       # passages each topic is written from
LINES = 8             # lines of dialogue asked for per topic

# Long enough to be worth quoting, short enough that eight of them plus the
# prompt is a small request. Same budget entities.profile() uses.
PASSAGE_CHARS = 900

# Fewer than this and there is nothing for two people to say to each other, so
# the topic is skipped and the next candidate gets the slot.
MIN_PASSAGES = 4

# mentions_of() round-robins across documents and then truncates, and the
# document order is insertion order, so a small limit hands back the oldest
# notes that mention a name and nothing else. Asking for eight passages about
# Tailscale returned eight from one project, because that project was ingested
# first. The SQL behind it reads every mention whatever the limit says, so
# asking for all of them is free and lets the selection below choose on
# something better than the order the corpus happened to be built in.
ALL_MENTIONS = 1000000

GAP = 0.30            # seconds of silence between lines
TURN_GAP = 0.45       # a little more when the speaker changes


def out_dir():
    """Where overviews are written: beside the corpus, not beside the code.

    Resolved on every call rather than at import, because the selfcheck points
    ARCHIVER_HOME at a temp directory and reloads archive underneath us.
    """
    d = archive.HOME / "overviews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slug_for(kind, name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return ("%s-%s" % (kind, s)).rstrip("-")


SLUG_OK = re.compile(r"^[a-z0-9-]{1,80}$")


# --------------------------------------------------------------- the passages
def _passage(n, row):
    """One numbered passage, carrying enough provenance to cite it later."""
    return {"n": n, "text": (row["text"] or "")[:PASSAGE_CHARS],
            "title": row["title"], "source": row["source"],
            "doc_id": row["doc_id"], "chunk_id": row["id"],
            "cite": "%s · %s" % (row["title"], row["source"])}


def _section(topic, why, rows, start=1):
    return {"topic": topic, "why": why,
            "passages": [_passage(i, r) for i, r in enumerate(rows, start)]}


def _spread(rows, n):
    """n passages taken a project at a time instead of straight off the top.

    The same round-robin mentions_of() uses across documents, applied one level
    up. Without it a section about Stripe is eight passages from whichever
    project was ingested first, which reads as an overview of that project
    rather than of the name, and the hosts have nothing to compare.
    """
    by = {}
    for r in rows:
        by.setdefault(r["source"], []).append(r)
    out = []
    while len(out) < n and by:
        for k in list(by):
            if not by[k]:
                del by[k]
                continue
            out.append(by[k].pop(0))
            if len(out) >= n:
                break
    return out


def _fallback_names(c, n):
    """The names to talk about when the co-occurrence graph has nothing to say.

    It has nothing to say on a small corpus: the graph drops any name written in
    more than 12% of the notes as boilerplate, and in a nine note archive that is
    every name in it. The selfcheck runs on exactly such an archive, so this path
    is not theoretical.
    """
    return [r["name"] for r in c.execute(
        "SELECT name FROM entity ORDER BY docs DESC, mentions DESC LIMIT ?", (n,))]


def _plan_entity(c, name, sections, per_section):
    """Sections for one name: who they are, then what each project says about them.

    mentions_of() hands back passages round-robin across documents, so the first
    slice is already one passage from each of several notes rather than eight
    from whichever note the join reached first. That spread is what makes the
    opening section an introduction instead of an anecdote.
    """
    row, rows = entities.mentions_of(c, name, limit=ALL_MENTIONS)
    if not row:
        return {"error": "no entity called %r in this archive" % name}
    opening = _spread(rows, per_section)
    used = {r["id"] for r in opening}
    by_shelf = {}
    for r in rows:
        if r["id"] not in used:
            by_shelf.setdefault(r["source"], []).append(r)
    order = sorted(by_shelf, key=lambda s: -len(by_shelf[s]))
    secs = [_section(row["name"],
                     "named in %d notes, %d times" % (row["docs"], row["mentions"]),
                     opening)]
    for shelf in order:
        if len(secs) >= sections:
            break
        got = by_shelf[shelf]
        if len(got) < MIN_PASSAGES:
            continue
        secs.append(_section("%s in %s" % (row["name"], shelf),
                             "%d passages from that project" % len(got),
                             got[:per_section]))
    return {"kind": "entity", "name": row["name"],
            "title": row["name"],
            "subtitle": "named in %d notes, %d times" % (row["docs"], row["mentions"]),
            "sections": secs}


def _plan_shelf(c, name, sections, per_section):
    """Sections for a project: its recurring names, one topic each.

    A project's own top entities are already the right table of contents. Nobody
    filed the notes at random and the names that keep coming back inside one
    folder are what that folder is about, which is the same reasoning the shelf
    panel in the cockpit runs on.
    """
    import cockpit                      # lazy: cockpit imports this module
    d = cockpit.shelf(c, name)
    if not d["notes"]:
        return {"error": "no project called %r in this archive" % name}
    names = [e["name"] for e in d["entities"]] or _fallback_names(c, sections * 3)
    secs = []
    for topic in names:
        if len(secs) >= sections:
            break
        row, rows = entities.mentions_of(c, topic, limit=ALL_MENTIONS)
        if not row:
            continue
        # Only the passages from this project. These names were chosen because
        # they are characteristic of the shelf, but most of them are written
        # about elsewhere too: the top names in Claude Code are Telegram, Slack
        # and Discord, and unfiltered they returned passages from ArgentOS Core
        # Docs and AMP Cortex without one line from the project being covered.
        # An overview of a project that quotes four other projects is not an
        # overview of that project.
        inside = [r for r in rows if r["source"] == name]
        if len(inside) < MIN_PASSAGES:
            continue
        secs.append(_section(topic, "%d passages from this project" % len(inside),
                             inside[:per_section]))
    if not secs:
        return {"error": "nothing in %r is named in enough of its own notes "
                         "to talk about" % name}
    return {"kind": "shelf", "name": name, "title": name,
            "subtitle": "%d notes in this project" % len(d["notes"]),
            "sections": secs}


def _plan_corpus(c, sections, per_section):
    """Sections for the whole archive: the names that bridge its neighbourhoods.

    Not the biggest names. You already know what your biggest names are, and an
    overview that opens on them is an overview you can skip. The bridges are the
    connections you made once and have not looked at since, which is the only
    thing a machine can tell you about your own notes that you did not already
    know.
    """
    import cockpit
    ents, kept, n_docs = cockpit._graph(c)
    picks = ([b["name"] for b in cockpit.bridges(ents, kept, sections * 4)]
             or _fallback_names(c, sections * 4))
    secs = []
    for topic in picks:
        if len(secs) >= sections:
            break
        row, rows = entities.mentions_of(c, topic, limit=ALL_MENTIONS)
        if not row or len(rows) < MIN_PASSAGES:
            continue
        secs.append(_section(topic, "named in %d notes" % row["docs"],
                             _spread(rows, per_section)))
    if not secs:
        return {"error": "this archive has no entity index yet; "
                         "run: python3 entities.py build"}
    return {"kind": "corpus", "name": "", "title": "The whole archive",
            "subtitle": "%d notes" % n_docs, "sections": secs}


def plan(c, kind, name=None, sections=SECTIONS, per_section=PER_SECTION):
    """What the overview will cover, and the passages each part is built from.

    No new retrieval lives here. An entity's passages come from
    entities.mentions_of(), a project's topics from the shelf panel, and the
    archive's from the co-occurrence graph the cockpit already reduces. Writing
    a fourth way to find passages would mean a fourth thing that can disagree
    with the citations.
    """
    kind = (kind or "corpus").strip().lower()
    if kind == "entity":
        return _plan_entity(c, name, sections, per_section)
    if kind == "shelf":
        return _plan_shelf(c, name, sections, per_section)
    if kind == "corpus":
        return _plan_corpus(c, sections, per_section)
    return {"error": "scope must be entity, shelf or corpus, not %r" % kind}


# ---------------------------------------------------------------- the script
SYSTEM = """You are writing the script for a two host audio overview of \
someone's own notes. It should sound like two people who have both read the \
notes talking to each other about them. Not a lecture. Not a summary read out.

You are given numbered passages from those notes. Everything either host says \
comes from those passages and from nothing else. You have no other knowledge.

FORMAT. One line per turn, marked A: or B:, ending with the passage it came \
from in square brackets:

A: Something one of them says, taken from a passage. [2]
B: The other one picking it up, from another. [1][4]

EVERY line carries at least one bracket, questions included: ask about the \
passage you are about to get into and cite that. A line with no bracket is \
deleted before anyone hears it, so a line without one is a line you wasted.

Never cite a number you were not given.

HOW IT SHOULD SOUND. Short sentences, contractions, one idea per turn. They \
interrupt, ask, and push back on each other. They are reading someone's notes, \
so they say "the notes say" and never "I know". No host names: the notes do not \
say who is presenting, so they do not address each other by name. No stage \
directions, no music cues, no markdown. Plain sentences only, because every \
character is going to be spoken aloud.

If the passages are thin, say so in the script and move on. Padding a thin \
section is the one thing worse than a short one."""


LINE = re.compile(r"^\s*(?:\*\*)?(?:host\s+)?([AB])(?:\*\*)?\s*[:\.]\s*(.+?)\s*$", re.I)
CITE = re.compile(r"\[\s*(\d+)\s*\]")
# The model is asked for plain text and mostly obliges, but a stray ** or a
# backtick gets read out as "asterisk" by the TTS, which is jarring enough to
# ruin a line. Stripped from what is spoken; the script keeps what it said.
MARKUP = re.compile(r"[*_`#]+")


def parse_script(text, n_passages):
    """Dialogue lines out of a model reply, with the ungrounded ones removed.

    Returns (lines, dropped). A line survives only if it cites at least one
    passage that was actually sent. This is the same guard archivist.ask() puts
    on an answer and it exists for the same reason: the instruction to cite is
    in the prompt, prompts get ignored quietly, and an invented line reads
    exactly like a real one. Out loud it is worse, because there is nothing to
    look at.
    """
    lines, dropped = [], []
    for raw in (text or "").splitlines():
        m = LINE.match(raw)
        if not m:
            continue
        host = m.group(1).upper()
        body = m.group(2)
        cites = [int(x) for x in CITE.findall(body)]
        valid = sorted({n for n in cites if 1 <= n <= n_passages})
        spoken = MARKUP.sub("", CITE.sub("", body))
        spoken = " ".join(spoken.split()).strip()
        if not spoken:
            continue
        if not valid:
            dropped.append({"host": host, "text": spoken,
                            "why": "cited nothing" if not cites
                                   else "cited %s, which was not sent" % cites})
            continue
        lines.append({"host": host, "text": spoken, "cites": valid})
    return lines, dropped


def _user_prompt(sec, i, total, scope, lines):
    passages = "\n\n".join("[%d] (%s)\n%s" % (p["n"], p["cite"], p["text"])
                           for p in sec["passages"])
    where = ("This is the opening section. Start the show: one line saying what "
             "these notes are and what is coming, then get straight into it."
             if i == 0 else
             "This is section %d of %d. The hosts are already talking, so do not "
             "reintroduce the show." % (i + 1, total))
    if i == total - 1:
        where += (" It is also the last section, so close the show in the final "
                  "line or two.")
    return ("OVERVIEW OF: %s\nTHIS SECTION IS ABOUT: %s\n%s\n\n"
            "PASSAGES:\n\n%s\n\nWrite about %d lines of dialogue."
            % (scope, sec["topic"], where, passages, lines))


def write_section(sec, i, total, scope, chat, lines=LINES, timeout=200):
    """One section of dialogue, from one chat call.

    One call per section rather than one for the whole script, because the
    device caps a request at about 220 seconds and a full script does not fit in
    that even on a fast endpoint. It is also better writing: each section sees
    eight passages instead of forty, so it can quote them instead of summarising
    them into mush.
    """
    import archivist
    model, base, key = chat
    body = {"model": model, "max_tokens": 1200, "temperature": 0.6,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user",
                          "content": _user_prompt(sec, i, total, scope, lines)}]}
    d = archivist.api("/v1/chat/completions", body, timeout=timeout,
                      base=base, key=key)
    reply = (d["choices"][0]["message"].get("content") or "").strip()
    got, dropped = parse_script(reply, len(sec["passages"]))
    by_n = {p["n"]: p for p in sec["passages"]}
    for ln in got:
        ln["sources"] = [by_n[n]["cite"] for n in ln["cites"]]
        ln["docs"] = [by_n[n]["doc_id"] for n in ln["cites"]]
    return got, dropped


# ----------------------------------------------------------------- the voices
def _device(path, body=None, timeout=60, raw=False):
    """One call to the device, reading host and key at call time.

    At call time and not at import, because the cockpit's setup panel rebinds
    archivist.HOST and archivist.KEY while the process is running and a captured
    copy would keep talking to the old box.
    """
    import archivist
    url = "http://%s:%s%s" % (archivist.HOST, archivist.PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Authorization": "Bearer %s" % archivist.KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        blob = r.read()
    return blob if raw else json.loads(blob or b"{}")


def tts_running():
    try:
        d = _device("/api/v1/models/running", timeout=15)
    except Exception:                                       # noqa: BLE001
        return False
    return TTS_MODEL in (d.get("running") or [])


def ensure_tts(wait=420, log=None):
    """Load the TTS model, because nothing on this device loads itself.

    Inference against a stopped model does not start it, it returns 404 with
    "is not loaded" in the body, and that reads like a broken URL rather than a
    missing model. Start it and wait, which takes a few seconds from warm and
    longer from cold.
    """
    if tts_running():
        return True
    if log:
        log("loading %s" % TTS_MODEL.split("/")[-1])
    _device("/api/v1/models/%s/start" % urllib.parse.quote(TTS_MODEL, safe=""),
            body={}, timeout=90)
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(4)
        if tts_running():
            return True
    raise RuntimeError("%s did not come up within %ds" % (TTS_MODEL, wait))


def stop_tts():
    """Give the NPU back. The device is shared and a loaded model holds its share."""
    return _device("/api/v1/models/%s/stop" % urllib.parse.quote(TTS_MODEL, safe=""),
                   body={}, timeout=90)


SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def split_for_speech(text, cap=TTS_CHARS):
    """A line as one or more synthesis requests, each inside the request cap.

    Dialogue lines are usually one request. This exists for the occasional one
    that is not, and for the guarantee: nothing this function returns can put a
    request anywhere near the device's 220 second ceiling, whatever the model
    decided to write.
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    out, cur = [], ""
    for piece in SENTENCE.split(text):
        if not piece:
            continue
        if cur and len(cur) + 1 + len(piece) > cap:
            out.append(cur)
            cur = piece
        else:
            cur = (cur + " " + piece).strip()
    if cur:
        out.append(cur)
    # A single sentence longer than the cap still has to go somewhere. Commas
    # first, since a break there is barely audible, and a blunt cut only if the
    # writer managed a 600 character clause with no punctuation in it at all.
    final = []
    for part in out:
        while len(part) > cap:
            cut = part.rfind(", ", 0, cap)
            if cut < cap // 3:
                cut = part.rfind(" ", 0, cap)
            if cut < cap // 3:
                cut = cap
            final.append(part[:cut].strip())
            part = part[cut:].lstrip(", ").strip()
        if part:
            final.append(part)
    return final


def speak(text, voice, tries=3, timeout=220):
    """One line of speech as WAV bytes.

    The body is exactly model, input and voice. Nothing else: sending
    response_format is accepted, and sending language is not, "en" comes back as
    a 500 saying the speech decoder produced an empty waveform even though the
    same text synthesises fine without it. The device's own OpenAPI declares no
    request schema for this route at all, so the shape here is what was measured
    against it rather than what a spec promised.
    """
    body = {"model": TTS_MODEL, "input": text, "voice": voice}
    last = ""
    for attempt in range(tries):
        try:
            blob = _device("/v1/audio/speech", body=body, timeout=timeout, raw=True)
            if blob[:4] == b"RIFF":
                return blob
            last = "no RIFF header: %r" % blob[:120]
        except urllib.error.HTTPError as e:
            last = "%s %s" % (e.code, e.read()[:160].decode("utf-8", "replace"))
            # The decoder returns an empty waveform now and then on text it
            # handled a moment earlier, so a 500 here is worth one more go. An
            # unsupported speaker is a 500 too and retrying that is pointless,
            # but it is also a configuration mistake that shows up on line one.
            if e.code not in (500, 502, 503, 504):
                break
        except Exception as e:                              # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("speech failed: %s" % last[:200])


# ------------------------------------------------------------------- the file
def _read_wav(blob):
    """(params, frames) from a WAV in memory."""
    w = wave.open(io.BytesIO(blob), "rb")
    try:
        return w.getparams(), w.readframes(w.getnframes())
    finally:
        w.close()


def stitch(clips, gaps=None):
    """Clips into one WAV, in Python, with the silences between them.

    No ffmpeg. Nothing in this tool is allowed to run another program, and a WAV
    is a header and a block of samples, so the wave module is the whole
    dependency. The parameters are checked rather than assumed: every clip comes
    from one model on one device so they do agree, but a mismatched sample rate
    concatenates perfectly happily and plays back as chipmunks halfway through,
    and nobody debugging that would think to look at the join.

    Returns (wav_bytes, starts) where starts are the offsets in seconds, so a
    player can jump to the line that said a thing.
    """
    if not clips:
        raise ValueError("nothing to stitch")
    gaps = list(gaps or [])
    params, first = _read_wav(clips[0])
    body, starts, at = [first], [0.0], len(first)
    width = params.sampwidth * params.nchannels
    rate = params.framerate
    for i, blob in enumerate(clips[1:]):
        p, frames = _read_wav(blob)
        if (p.nchannels, p.sampwidth, p.framerate) != (
                params.nchannels, params.sampwidth, params.framerate):
            raise ValueError("clip %d is %dHz/%dch/%dbit, the first is %dHz/%dch/%dbit"
                             % (i + 1, p.framerate, p.nchannels, p.sampwidth * 8,
                                params.framerate, params.nchannels,
                                params.sampwidth * 8))
        gap = gaps[i] if i < len(gaps) else GAP
        pad = b"\x00" * (int(rate * gap) * width)
        body.append(pad)
        at += len(pad)
        starts.append(at / float(rate * width))
        body.append(frames)
        at += len(frames)
    joined = b"".join(body)
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    try:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(joined)
    finally:
        w.close()
    return buf.getvalue(), starts


def wav_seconds(blob):
    p, frames = _read_wav(blob)
    return len(frames) / float(p.framerate * p.sampwidth * p.nchannels)


# ------------------------------------------------------------------ the build
def build(kind, name=None, sections=SECTIONS, per_section=PER_SECTION,
          lines=LINES, audio=True, progress=None, keep_loaded=True):
    """Plan, write, speak, stitch, save. Returns the record it wrote.

    progress(phase, step, done, total) is called as it goes, because this takes
    minutes and a spinner with no numbers behind it is indistinguishable from a
    hang.
    """
    def say(phase, step, done, total):
        if progress:
            progress(phase, step, done, total)

    c = archive.db()
    p = plan(c, kind, name, sections, per_section)
    if p.get("error"):
        return p
    secs = p["sections"]
    scope = p["title"]

    import archivist
    chat = archivist.pick_chat()
    script, dropped, t0 = [], [], time.time()
    for i, sec in enumerate(secs):
        say("script", "writing: %s" % sec["topic"], i, len(secs))
        try:
            got, lost = write_section(sec, i, len(secs), scope, chat, lines)
        except Exception as e:                              # noqa: BLE001
            return {"error": "the writing model failed on %r: %s"
                             % (sec["topic"], str(e)[:200])}
        # A section that came back with nothing grounded is withheld whole,
        # rather than quietly shortening the show. The count is what tells you
        # the passages were thin, and hiding it is how a corpus looks richer
        # than it is.
        for ln in got:
            ln["section"] = sec["topic"]
        script += got
        dropped += [dict(d, section=sec["topic"]) for d in lost]
    say("script", "%d lines" % len(script), len(secs), len(secs))
    if not script:
        return {"error": "the hosts wrote nothing that cited a passage; "
                         "nothing was withheld because nothing was grounded"}
    wrote_in = time.time() - t0

    rec = {"kind": p["kind"], "name": p.get("name") or "", "slug": slug_for(kind, name),
           "title": p["title"], "subtitle": p.get("subtitle", ""),
           "created_at": archive.now(), "voices": dict(VOICES),
           "sections": [{"topic": s["topic"], "why": s["why"],
                         "passages": [{k: v for k, v in pp.items() if k != "text"}
                                      for pp in s["passages"]]} for s in secs],
           "lines": script, "dropped": dropped,
           "script_seconds": round(wrote_in, 1)}

    if not audio:
        rec["audio"] = None
        _save(rec)
        return rec

    ensure_tts(log=lambda m: say("speech", m, 0, len(script)))
    clips, gaps, t1, failed = [], [], time.time(), 0
    for i, ln in enumerate(script):
        say("speech", "%s: %s" % (ln["host"], ln["text"][:60]), i, len(script))
        parts = split_for_speech(ln["text"])
        got = []
        try:
            for part in parts:
                got.append(speak(part, VOICES.get(ln["host"], VOICES["A"])))
        except Exception as e:                              # noqa: BLE001
            # The line stays in the script and out of the audio. Dropping it from
            # both would make the transcript agree with the recording by lying
            # about what was written.
            ln["spoken"] = False
            ln["speech_error"] = str(e)[:160]
            failed += 1
            continue
        ln["spoken"] = True
        for j, blob in enumerate(got):
            if clips:
                gaps.append(TURN_GAP if (j == 0 and _turned(script, i)) else GAP)
            clips.append(blob)
        ln["_clips"] = len(got)
    if not clips:
        return {"error": "every line failed to synthesise; the TTS model is "
                         "loaded but not answering"}
    say("stitch", "joining %d clips" % len(clips), len(script), len(script))
    blob, starts = stitch(clips, gaps)

    k = 0
    for ln in script:
        n = ln.pop("_clips", 0)
        ln["start"] = round(starts[k], 2) if n else None
        k += n
    rec["speech_seconds"] = round(time.time() - t1, 1)
    rec["failed_lines"] = failed
    rec["clips"] = len(clips)
    rec["duration"] = round(wav_seconds(blob), 1)
    path = out_dir() / (rec["slug"] + ".wav")
    path.write_bytes(blob)
    rec["audio"] = str(path)
    rec["bytes"] = len(blob)
    _save(rec)
    return rec


def _turned(script, i):
    return i > 0 and script[i]["host"] != script[i - 1]["host"]


def _save(rec):
    (out_dir() / (rec["slug"] + ".json")).write_text(
        json.dumps(rec, indent=1), encoding="utf-8")
    return rec


def load(slug):
    if not SLUG_OK.match(slug or ""):
        return None
    f = out_dir() / (slug + ".json")
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return None


def audio_path(slug):
    """The wav for a slug, or None. Slug shape is checked, not the path: this is
    reachable from an HTTP handler and ../ in a query string is free to type."""
    if not SLUG_OK.match(slug or ""):
        return None
    f = out_dir() / (slug + ".wav")
    return f if f.exists() else None


def saved():
    """Every overview already built, newest first."""
    out = []
    for f in sorted(out_dir().glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        out.append({"slug": d.get("slug"), "title": d.get("title"),
                    "kind": d.get("kind"), "created_at": d.get("created_at"),
                    "duration": d.get("duration"), "lines": len(d.get("lines") or []),
                    "has_audio": bool(d.get("audio"))})
    out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return out


# -------------------------------------------------------------------- the job
# One at a time, and not because of the server. The device runs one inference at
# a time, so a second overview would sit inside the gateway behind the first and
# both would look hung from the browser.
_JOB = {"state": "idle"}
_LOCK = threading.Lock()


def job():
    d = dict(_JOB)
    d.pop("_thread", None)
    return d


def start_job(kind, name=None, **kw):
    with _LOCK:
        if _JOB.get("state") == "running":
            return {"error": "an overview of %r is already being built"
                             % _JOB.get("scope", "something"), "busy": True}
        _JOB.clear()
        _JOB.update({"state": "running", "scope": name or kind, "kind": kind,
                     "phase": "script", "step": "planning", "done": 0, "total": 1,
                     "started": time.time()})

    def run():
        def progress(phase, step, done, total):
            _JOB.update({"phase": phase, "step": step, "done": done, "total": total})
        try:
            rec = build(kind, name, progress=progress, **kw)
        except Exception as e:                              # noqa: BLE001
            _JOB.update({"state": "error", "error": "%s: %s"
                                                    % (type(e).__name__, str(e)[:200])})
            return
        if rec.get("error"):
            _JOB.update({"state": "error", "error": rec["error"]})
            return
        _JOB.update({"state": "done", "result": rec,
                     "elapsed": round(time.time() - _JOB["started"], 1)})

    t = threading.Thread(target=run, daemon=True)
    _JOB["_thread"] = t
    t.start()
    return job()


# ------------------------------------------------------------------ selftests
def _script_selftest():
    """The citation filter, which is the only thing standing between a listener
    and a fluent invention."""
    reply = ("Here is the script:\n"
             "A: The notes say the gateway moved to port eighty. [1]\n"
             "B: Right, and that broke every client with the old address. [2][1]\n"
             "A: I reckon they also rewrote the scheduler.\n"
             "**B**: Worth reading the note itself. [9]\n"
             "B: And the fix was to probe both ports. [3]\n"
             "not a line at all\n")
    lines, dropped = parse_script(reply, 3)
    assert [l["host"] for l in lines] == ["A", "B", "B"], lines
    assert lines[1]["cites"] == [1, 2], lines[1]
    assert "[1]" not in lines[0]["text"], lines[0]
    assert len(dropped) == 2, dropped                 # the uncited one and the [9]
    assert "cited nothing" in dropped[0]["why"], dropped[0]
    assert "not sent" in dropped[1]["why"], dropped[1]
    # markdown never reaches the synthesiser, which would read it out
    lines2, _ = parse_script("A: A **bold** claim with `code`. [1]", 1)
    assert lines2[0]["text"] == "A bold claim with code.", lines2
    # and a reply with no dialogue at all is not silently treated as a script
    assert parse_script("I cannot help with that.", 3) == ([], [])
    return "script: 8/8"


def _speech_split_selftest():
    """Nothing reaches the device larger than the request cap allows."""
    one = "Short line. [1]"
    assert split_for_speech(one) == ["Short line. [1]"]
    long = ("This is a sentence that has a reasonable length to it. " * 40).strip()
    parts = split_for_speech(long, cap=200)
    assert parts and all(len(p) <= 200 for p in parts), [len(p) for p in parts]
    assert "".join(p.replace(" ", "") for p in parts) == long.replace(" ", "")
    # one sentence longer than the cap still has to fit somewhere
    runon = "word, " * 90
    parts = split_for_speech(runon, cap=120)
    assert all(len(p) <= 120 for p in parts), [len(p) for p in parts]
    # and one with nothing to break on at all
    solid = "x" * 500
    parts = split_for_speech(solid, cap=100)
    assert parts and all(len(p) <= 100 for p in parts), [len(p) for p in parts]
    assert "".join(parts) == solid
    return "speech split: 5/5"


def _tone(seconds, rate=24000, value=1000):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(struct.pack("<%dh" % int(rate * seconds),
                              *([value] * int(rate * seconds))))
    w.close()
    return buf.getvalue()


def _stitch_selftest():
    """Concatenation in Python, including the check that stops a silent mismatch."""
    a, b = _tone(0.5), _tone(0.25)
    blob, starts = stitch([a, b], [0.5])
    assert abs(wav_seconds(blob) - 1.25) < 0.01, wav_seconds(blob)
    assert starts == [0.0, 1.0], starts
    p, _ = _read_wav(blob)
    assert (p.nchannels, p.sampwidth, p.framerate) == (1, 2, 24000), p
    try:
        stitch([a, _tone(0.25, rate=16000)], [0.0])
    except ValueError as e:
        assert "16000Hz" in str(e), str(e)
    else:
        raise AssertionError("a sample rate mismatch was concatenated anyway")
    return "stitch: 4/4"


def _plan_selftest(c):
    """The planner, over whatever archive is in front of it.

    Called by cockpit --selfcheck against its nine note temp corpus, so the
    small-corpus fallback in _plan_corpus is on the checked path rather than
    the one nobody runs until a user has nine notes.
    """
    d = plan(c, "corpus", None, sections=3, per_section=4)
    assert not d.get("error"), d
    assert d["sections"], d
    ns = [p["n"] for p in d["sections"][0]["passages"]]
    assert ns == list(range(1, len(ns) + 1)), ns
    for s in d["sections"]:
        for p in s["passages"]:
            assert p["cite"] and p["doc_id"], p
    assert plan(c, "nonsense", None).get("error")
    assert plan(c, "entity", "NotAName").get("error")
    return "plan: %d sections over %d passages" % (
        len(d["sections"]), sum(len(s["passages"]) for s in d["sections"]))


# -------------------------------------------------------------------- the cli
def _cli_progress(phase, step, done, total):
    sys.stdout.write("\r  %-8s %3d/%-3d  %-58s" % (phase, done, total, step[:58]))
    sys.stdout.flush()


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        print("  " + _script_selftest())
        print("  " + _speech_split_selftest())
        print("  " + _stitch_selftest())
        print("  " + _plan_selftest(archive.db()))
        return 0
    if cmd == "list":
        rows = saved()
        if not rows:
            print("  no overviews yet")
        for r in rows:
            print("  %-28s %5s min  %3d lines  %s"
                  % (r["slug"], round((r["duration"] or 0) / 60.0, 1),
                     r["lines"], r["title"]))
        return 0
    if cmd == "unload":
        print("  " + json.dumps(stop_tts())[:200])
        return 0
    if cmd not in ("entity", "shelf", "corpus"):
        print(__doc__)
        return 2
    rest = argv[1:]
    text_only = "--script-only" in rest
    name = " ".join(a for a in rest if not a.startswith("--")) or None
    t0 = time.time()
    rec = build(cmd, name, audio=not text_only, progress=_cli_progress)
    print()
    if rec.get("error"):
        print("  " + rec["error"])
        return 1
    print("\n  %s" % rec["title"])
    print("  %d lines over %d sections, %d withheld as ungrounded"
          % (len(rec["lines"]), len(rec["sections"]), len(rec["dropped"])))
    for ln in rec["lines"]:
        print("\n  %s  %s" % (ln["host"], ln["text"]))
        print("     %s" % " · ".join(ln["sources"]))
    if rec.get("audio"):
        print("\n  %s" % rec["audio"])
        print("  %.1f min of audio, written in %.0fs, spoken in %.0fs, %.0fs total"
              % (rec["duration"] / 60.0, rec["script_seconds"],
                 rec["speech_seconds"], time.time() - t0))
        if rec.get("failed_lines"):
            print("  %d line(s) are in the script and not in the audio"
                  % rec["failed_lines"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
