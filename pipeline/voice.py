"""Transcript -> audio + timing, via ElevenLabs text-to-dialogue (eleven_v4).

    uv run pipeline/voice.py episode-1/episode-1.jsonl --dry-run  # offline estimate
    uv run pipeline/voice.py episode-1/episode-1.jsonl            # cached per chunk
    uv run pipeline/voice.py episode-1/episode-1.jsonl --only 3   # re-render chunk 3

Delivery tags are picked per turn by Jev from a per-persona menu (voices.toml), so the text can't be altered.
Interrupted lines already end in "—"; the interrupter gets "[jumping in]".
"""

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from typesafe_sdk import Choice, TypeSafeClient

ROOT = Path(__file__).resolve().parents[1]
CHARS_PER_SEC = 15.0   # measured on a v4 test render (spoken characters, Polish)


def load():
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    voices = tomllib.loads((ROOT / "voices.toml").read_text())
    ids = {s["name"]: s["voice_id"] for s in cfg["seat"]}
    return cfg, voices, ids


def spoken(text: str, pronounce: dict[str, str]) -> str:
    for k, v in pronounce.items():
        text = text.replace(k, v)
    return text


def pick_tag(client: TypeSafeClient, deliv: dict, speaker: str, prev: dict | None, text: str, min_conf: float) -> str:
    menu = deliv.get(speaker)
    if not menu:
        return ""
    options = {k: v[0] for k, v in menu.items() if k != "brief"}
    if not options:
        return ""
    criteria = {"neutral": "Plain delivery, nothing special.", **options}
    state = {
        "speaker": speaker,
        "speaker_profile": menu.get("brief", ""),
        "previous_statement": f"{prev['speaker']}: {prev['text']}" if prev else "(none, first statement)",
        "statement": text,
    }
    ans = client.system_one(
        state=state,
        questions={"delivery": Choice(
            instructions="How would `speaker` most plausibly deliver `statement` in this conversation? "
                         "Choose 'neutral' unless one delivery clearly fits the wording and context.",
            criteria=criteria)},
    ).answers["delivery"]
    if ans.choice == "neutral" or ans.confidence < min_conf:
        return ""
    return menu[ans.choice][1]


def merge_same_speaker(transcript):
    """Back-to-back turns by one speaker (e.g. the host's producer nudge and his next line) become one turn."""
    out = []
    for e in transcript:
        if (out and out[-1]["speaker"] == e["speaker"]
                and "interrupted_full" not in out[-1] and "interrupted_full" not in e):
            out[-1] = {**out[-1], "text": out[-1]["text"] + " " + e["text"]}
        else:
            out.append(dict(e))
    return out


def build_turns(transcript, ids, voices, use_tags: bool):
    transcript = merge_same_speaker(transcript)
    client = TypeSafeClient() if use_tags else None
    tts = voices["tts"]
    turns = []
    for i, e in enumerate(transcript):
        text = spoken(e["text"], voices["pronounce"])
        prev = transcript[i - 1] if i else None
        if prev and "interrupted_full" in prev:
            tag = "[jumping in]"
        elif client:
            tag = pick_tag(client, voices["delivery"], e["speaker"], prev, e["text"], tts["tag_min_confidence"])
        else:
            tag = ""
        turns.append({"i": i, "speaker": e["speaker"], "voice_id": ids[e["speaker"]],
                      "text": f"{tag} {text}".strip(), "tag": tag, "display": e["text"]})
    return turns


def chunk(turns, max_chars):
    chunks, cur, n = [], [], 0
    for t in turns:
        if cur and n + len(t["text"]) > max_chars:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(t)
        n += len(t["text"])
    return chunks + ([cur] if cur else [])


def credits_left() -> int | None:
    r = subprocess.run(["elevenlabs", "user", "subscription", "get", "--format", "json"], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        return d["character_limit"] - d["character_count"]
    except Exception:
        return None


def render_chunk(idx, ch, prev_chunk, next_chunk, tts, cache: Path) -> tuple[Path, dict]:
    body = {
        "inputs": [{"text": t["text"], "voice_id": t["voice_id"]} for t in ch],
        "model_id": tts["model_id"],
        "language_code": tts["language_code"],
        "settings": {"stability": tts["stability"], "similarity": tts["similarity"]},
    }
    if prev_chunk:
        body["previous_text"] = prev_chunk[-1]["text"][-100:]
    if next_chunk:
        body["future_text"] = next_chunk[0]["text"][:100]
    key = hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    mp3, meta = cache / f"{idx:02d}-{key}.mp3", cache / f"{idx:02d}-{key}.json"
    if mp3.exists() and meta.exists():
        return mp3, json.loads(meta.read_text())
    r = subprocess.run(["elevenlabs", "text-to-dialogue", "convert_with_timestamps", "--json", json.dumps(body, ensure_ascii=False),
                        "--format", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"chunk {idx} failed: {r.stdout[:500]} {r.stderr[:300]}")
    d = json.loads(r.stdout)
    mp3.write_bytes(base64.b64decode(d.pop("audio_base64")))
    meta.write_text(json.dumps({"voice_segments": d["voice_segments"]}, ensure_ascii=False))
    return mp3, {"voice_segments": d["voice_segments"]}


def duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def lint(transcript, force: bool):
    """Refuse to spend credits on a transcript that would sound broken or run too long."""
    import re
    problems = []
    text = " ".join(e["text"] for e in transcript)
    for pat, what in [(r"\d", "digits (numbers must be words)"), (r"\b[\w-]+\.(?:com|pl|net|org|eu|tv)\b|https?://|www\.", "links or domains"),
                      (r"\*\*|`|#", "markdown"), (r"[a-zęóąśłżźćń][.!?][A-ZŻŹĆĄŚĘŁÓŃ]", "glued sentences (tool narration?)"),
                      (r"(?i)\b(sprawdz\w*|wyszuk\w*)\b", "search narration"), (r"\bSolnej\b", "Solnej")]:
        if m := re.search(pat, text):
            problems.append(f"{what}: {m.group(0)!r}")
    minutes = sum(len(e["text"]) for e in transcript) / CHARS_PER_SEC / 60
    if minutes > 20.5:
        problems.append(f"~{minutes:.1f} min of speech, over the 20 min cap")
    if problems and not force:
        sys.exit("lint failed, not rendering:\n  " + "\n  ".join(problems))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="offline plan; skip delivery selection and credit lookup")
    ap.add_argument("--no-tags", action="store_true")
    ap.add_argument("--only", type=int, help="render only this chunk index (others must already be cached)")
    ap.add_argument("--force", action="store_true", help="ignore the credit check")
    ap.add_argument("--override", type=Path, help='JSON {"turn index": "new text"}: replaces those turns AFTER chunking (chunk boundaries stay put)')
    a = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    cfg, voices, ids = load()
    tts = voices["tts"]
    transcript = [json.loads(l) for l in a.transcript.read_text().splitlines() if l.strip()]
    lint(transcript, a.force)
    turns = build_turns(transcript, ids, voices, use_tags=not (a.no_tags or a.dry_run))
    chunks = chunk(turns, tts["max_chunk_chars"])   # boundaries fixed here, before any override
    if a.override:
        for k, new in json.loads(a.override.read_text()).items():
            t = next(t for t in turns if t["i"] == int(k))
            t["text"], t["display"], t["tag"] = spoken(new, voices["pronounce"]), new, ""

    total = sum(len(t["text"]) for i, ch in enumerate(chunks) if a.only is None or i == a.only for t in ch)
    tagged = sum(1 for t in turns if t["tag"])
    est = sum(len(t["text"]) - len(t["tag"]) for t in turns) / CHARS_PER_SEC / 60
    print(f"{len(turns)} turns, {len(chunks)} chunks, ~{est:.1f} min of speech, {tagged} tagged turns; "
          f"{total} characters to render (= credits){f' (chunk {a.only} only)' if a.only is not None else ''}")
    for i, ch in enumerate(chunks):
        print(f"  chunk {i}: turns {ch[0]['i']}-{ch[-1]['i']}, {sum(len(t['text']) for t in ch)} chars")
    for t in turns[:12]:
        print(f"  [{t['speaker']}] {t['text'][:90]}")
    if a.dry_run:
        print("offline estimate: delivery selection skipped; tagged chunk boundaries may differ")
        return
    left = credits_left()
    print(f"credits left: {left}")
    if left is not None and total > left and not a.force:
        sys.exit(f"need {total} credits, only {left} left (use --force to try anyway)")

    name = a.transcript.stem
    cache = ROOT / "audio" / "chunks" / name
    cache.mkdir(parents=True, exist_ok=True)
    rendered = []
    for i, ch in enumerate(chunks):
        if a.only is not None and i != a.only and not any(cache.glob(f"{i:02d}-*.mp3")):
            sys.exit(f"chunk {i} not cached; run without --only first")
        if a.only is not None and i != a.only:
            mp3 = sorted(cache.glob(f"{i:02d}-*.mp3"))[-1]
            meta = json.loads(mp3.with_suffix(".json").read_text())
        else:
            mp3, meta = render_chunk(i, ch, chunks[i - 1] if i else None, chunks[i + 1] if i + 1 < len(chunks) else None, tts, cache)
        rendered.append((mp3, meta, ch))
        print(f"chunk {i} ok: {mp3.name}", flush=True)

    # join and build absolute timing
    listing = cache / "concat.txt"
    listing.write_text("".join(f"file '{m.resolve()}'\n" for m, _, _ in rendered))
    out = ROOT / "audio" / f"{name}.mp3"
    for f in (out, out.with_suffix(".timing.json")):   # keep the previous render
        bak = f.with_name(f.name.replace(name, name + ".v1", 1))
        if f.exists() and not bak.exists():
            f.rename(bak)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)], check=True)
    timing, offset = [], 0.0
    for mp3, meta, ch in rendered:
        for seg, t in zip(meta["voice_segments"], ch):
            timing.append({"turn": t["i"], "speaker": t["speaker"], "start": round(offset + seg["start_time_seconds"], 3),
                           "end": round(offset + seg["end_time_seconds"], 3), "text": t["display"]})
        offset += duration(mp3)
    (ROOT / "audio" / f"{name}.timing.json").write_text(json.dumps(timing, ensure_ascii=False, indent=1))
    print(f"wrote {out} ({offset / 60:.1f} min) and timing for {len(timing)} turns")


if __name__ == "__main__":
    main()
