"""Episode audio + timing -> video. Hedra Character 3 lip-syncs close-ups; a silent-driven wide shot fills the rest.

    uv run pipeline/video.py plan episode-1                   # plan and estimated cost
    uv run pipeline/video.py ambient episode-1 --yes          # wide-shot clips (paid)
    uv run pipeline/video.py render episode-1 --turns 0,1,3,5  # selected turns, cached
    uv run pipeline/video.py render episode-1 --budget 15      # estimated spend cap
    uv run pipeline/video.py render episode-1 --redo 7,9       # regenerate turns (paid)
    uv run pipeline/video.py assemble episode-1 --until 60     # local preview
    uv run pipeline/video.py assemble episode-1               # video/episode-1.mp4

Every clip is one Hedra job (billed per output second), cached in video/clips/<episode>/; a job id is written
right after submit, so a crashed run resumes polling instead of paying twice. Auth is `hedra-cli auth login`.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "video"
MODEL = "submit-hedra-character-3"
MIN_CLIP_S = 0.6   # Hedra accepts audio from 0.5 s


def cfg() -> dict:
    return tomllib.loads((ROOT / "config.toml").read_text())["video"]


def sh(*cmd, check=True) -> str:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"{cmd[0]} failed: {(r.stderr or r.stdout)[:400]}")
    return r.stdout


def hedra(*args) -> dict:
    r = subprocess.run(["hedra-cli", *map(str, args), "--format", "json"], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"hedra-cli {args[0]} {args[1]}: {(r.stderr or r.stdout)[:300]}")
    if r.returncode:
        raise RuntimeError(f"hedra-cli {args[0]} {args[1]}: {json.dumps(d.get('error', d))[:300]}")
    return d


def sha(*parts: bytes | str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else p.encode())
    return h.hexdigest()[:10]


def upload(path: Path) -> dict:
    return {"source": "url", "url": hedra("files", "upload", "--file", path)["url"]}


def run_job(inp: dict, out: Path) -> float:
    """Submit (or resume) one Character 3 job, wait for it and download the mp4 to `out`. Returns its cost in USD."""
    jobfile = out.with_suffix(".job")
    if jobfile.exists():
        job_id = jobfile.read_text().strip()
    else:
        job_id = hedra("jobs", MODEL, "--input", json.dumps(inp, ensure_ascii=False))["job_id"]
        jobfile.write_text(job_id)
    while True:
        status = hedra("jobs", "get-status", "--job-id", job_id).get("status")
        if status in ("COMPLETED", "FAILED"):
            break
        time.sleep(4)
    job = hedra("jobs", "get", "--job-id", job_id)
    if status == "FAILED" or not job.get("outputs"):
        jobfile.unlink(missing_ok=True)
        raise RuntimeError(f"{out.name}: {(job.get('error') or {}).get('message', 'no output')}")
    urllib.request.urlretrieve(job["outputs"][0]["url"], out)
    return float(job.get("cost") or 0)


def load(episode: str):
    audio = ROOT / "audio" / f"{episode}.mp3"
    timing = json.loads((ROOT / "audio" / f"{episode}.timing.json").read_text())
    return audio, timing


def plan(timing: list[dict], c: dict) -> list[dict]:
    """Lip-synced segments: [{turn, k, speaker, t0, t1}] in absolute seconds; everything else is the wide shot."""
    segs, last = [], timing[-1]["turn"]
    for t in timing:
        a, b = t["start"], t["end"]
        dur = b - a
        if t["turn"] == timing[0]["turn"]:
            a += c["opening_wide_s"]
        if t["turn"] == last and dur > c["closing_wide_s"] + 2:
            b -= c["closing_wide_s"]
        if b - a > c["long_turn_s"]:
            parts = [(a, a + c["head_s"]), (b - c["tail_s"], b)]
        else:
            parts = [(a, b)]
        for k, (p0, p1) in enumerate(parts):
            if p1 - p0 >= MIN_CLIP_S:
                segs.append({"turn": t["turn"], "k": k, "speaker": t["speaker"], "t0": round(p0, 3), "t1": round(p1, 3)})
    return segs


def portrait(name: str, c: dict) -> Path:
    p = c["portraits"][name]
    out = VIDEO / "crops" / f"{name}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        sh("ffmpeg", "-y", "-loglevel", "error", "-i", ROOT / p["file"], "-vf", f"crop=1122:631:0:{p['crop_y']}", out)
    return out


def clip_paths(episode: str, seg: dict, c: dict, audio: Path) -> tuple[Path, Path]:
    """(audio slice, cached mp4). The mp4 name carries a hash of image + audio + prompt, so changed inputs re-render."""
    d = VIDEO / "clips" / episode
    d.mkdir(parents=True, exist_ok=True)
    wav = d / f"t{seg['turn']:02d}-{seg['k']}.slice.mp3"
    sh("ffmpeg", "-y", "-loglevel", "error", "-ss", seg["t0"], "-t", round(seg["t1"] - seg["t0"], 3), "-i", audio,
       "-c:a", "libmp3lame", "-q:a", "2", wav)
    key = sha(portrait(seg["speaker"], c).read_bytes(), wav.read_bytes(), c["talk_prompt"], c["resolution"])
    return wav, d / f"t{seg['turn']:02d}-{seg['k']}-{key}.mp4"


def cached(episode: str, seg: dict, c: dict, audio: Path) -> tuple[Path, Path, bool]:
    wav, mp4 = clip_paths(episode, seg, c, audio)
    return wav, mp4, mp4.exists()


def cmd_plan(a, c):
    audio, timing = load(a.episode)
    segs = plan(timing, c)
    secs = sum(s["t1"] - s["t0"] for s in segs)
    total = timing[-1]["end"]
    todo = [s for s in segs if not cached(a.episode, s, c, audio)[2]]
    todo_s = sum(s["t1"] - s["t0"] for s in todo)
    print(f"{len(segs)} clips, {secs:.0f} s of {total:.0f} s lip-synced ({secs / total:.0%}), ~${secs * c['price_per_second']:.2f}")
    print(f"still to render: {len(todo)} clips, {todo_s:.0f} s, ~${todo_s * c['price_per_second']:.2f}")
    for s in segs[:14]:
        print(f"  turn {s['turn']:2d}.{s['k']} {s['speaker']:9s} {s['t0']:7.1f}-{s['t1']:7.1f} ({s['t1'] - s['t0']:.1f} s)")


def cmd_render(a, c):
    audio, timing = load(a.episode)
    segs = plan(timing, c)
    if a.turns:
        want = {int(x) for x in a.turns.split(",")}
        segs = [s for s in segs if s["turn"] in want]
    redo = {int(x) for x in a.redo.split(",")} if a.redo else set()
    todo = []
    for s in segs:
        wav, mp4, have = cached(a.episode, s, c, audio)
        if s["turn"] in redo:
            for f in (mp4, mp4.with_suffix(".job")):
                f.unlink(missing_ok=True)
            have = False
        if not have:
            todo.append((s, wav, mp4))
    secs = sum(s["t1"] - s["t0"] for s, _, _ in todo)
    cost = secs * c["price_per_second"]
    print(f"{len(todo)} clips to render, {secs:.0f} s, ~${cost:.2f} (budget ${a.budget:.2f})")
    if cost > a.budget:
        sys.exit("over budget; raise --budget or narrow with --turns")

    def one(item):
        s, wav, mp4 = item
        try:
            inp = {"aspect_ratio": "16:9", "resolution": c["resolution"], "prompt": c["talk_prompt"],
                   "start_image": upload(portrait(s["speaker"], c)), "audio": upload(wav)}
            spent = run_job(inp, mp4)
            print(f"  turn {s['turn']}.{s['k']} {s['speaker']}: ok ${spent:.2f}", flush=True)
            return spent
        except RuntimeError as e:
            print(f"  turn {s['turn']}.{s['k']} {s['speaker']}: FAILED {e}", flush=True)
            return 0.0

    with ThreadPoolExecutor(c["concurrency"]) as pool:
        spent = sum(pool.map(one, todo))
    print(f"spent ${spent:.2f}; balance ${hedra('billing', 'get-balance')['balance']:.2f}")


def cmd_ambient(a, c):
    d = VIDEO / "ambient"
    d.mkdir(parents=True, exist_ok=True)
    silence = d / "silence.mp3"
    sh("ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", c["ambient_s"], "-c:a", "libmp3lame", silence)
    todo = [(i, p) for i, p in enumerate(c["ambient_prompts"]) if not list(d.glob(f"{i:02d}-{sha(p)}.mp4"))]
    print(f"{len(todo)} ambient clips to render, ~${len(todo) * c['ambient_s'] * c['price_per_second']:.2f}")
    if todo and not a.yes:
        sys.exit("pass --yes to spend")
    wide = upload(ROOT / "assets" / "studio_shot.png")
    for i, p in todo:
        out = d / f"{i:02d}-{sha(p)}.mp4"
        spent = run_job({"aspect_ratio": "16:9", "resolution": c["resolution"], "prompt": p, "start_image": wide,
                         "audio": upload(silence)}, out)
        print(f"  {out.name}: ok ${spent:.2f}", flush=True)


def normalize(src: Path, out: Path, c: dict, ss: float = 0, frames: int | None = None):
    w, h = c["size"]
    pad = "tpad=stop=-1:stop_mode=clone," if frames else ""   # repeat the last frame if a clip is short; needs the frame cap below
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-i", src,
           "-vf", f"{pad}fps={c['fps']},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p",
           "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    if frames:
        cmd += ["-frames:v", frames]
    sh(*cmd, out)


def cmd_assemble(a, c):
    audio, timing = load(a.episode)
    fps = c["fps"]
    total = min(timing[-1]["end"], a.until) if a.until else timing[-1]["end"]
    n_total = round(total * fps)
    tmp = VIDEO / "tmp" / a.episode
    tmp.mkdir(parents=True, exist_ok=True)
    for f in tmp.glob("*.mp4"):
        f.unlink()

    amb = sorted((VIDEO / "ambient").glob("[0-9][0-9]-*.mp4"))
    if not amb:
        sys.exit("no ambient clips: run `pipeline/video.py ambient` first")
    stream = tmp / "ambient-stream.mp4"
    parts = []
    for i, p in enumerate(amb):
        parts.append(tmp / f"amb{i}.mp4")
        normalize(p, parts[-1], c)
    lst = tmp / "amb.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    sh("ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", stream)
    amb_frames = round(float(sh("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", stream)) * fps)

    have = {}
    for s in plan(timing, c):
        _, mp4, ok = cached(a.episode, s, c, audio)
        if ok:
            have[round(s["t0"] * fps)] = (s, mp4)

    cursor, amb_pos, shot_files = 0, 0, []
    starts = sorted(have)

    def wide(n):
        nonlocal amb_pos
        while n > 0:
            take = min(n, amb_frames - amb_pos)
            f = tmp / f"s{len(shot_files):03d}.mp4"
            normalize(stream, f, c, ss=amb_pos / fps, frames=take)
            shot_files.append(f)
            n -= take
            amb_pos = (amb_pos + take) % amb_frames

    for f0 in starts:
        s, mp4 = have[f0]
        f1 = min(round(s["t1"] * fps), n_total)
        if f0 >= n_total or f0 < cursor:
            continue
        wide(f0 - cursor)
        f = tmp / f"s{len(shot_files):03d}.mp4"
        normalize(mp4, f, c, frames=f1 - f0)
        shot_files.append(f)
        cursor = f1
    wide(n_total - cursor)

    lst = tmp / "shots.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in shot_files))
    silent = tmp / "video-only.mp4"
    sh("ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", silent)
    out = VIDEO / (f"{a.episode}.mp4" if not a.until else f"{a.episode}.preview.mp4")
    sh("ffmpeg", "-y", "-loglevel", "error", "-i", silent, "-t", f"{total:.3f}", "-i", audio, "-c:v", "copy", "-c:a", "aac",
       "-b:a", "128k", "-shortest", out)
    print(f"wrote {out}: {total / 60:.1f} min, {len(have)} close-up clips, {len(shot_files)} shots")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "render", "ambient", "assemble"):
        p = sub.add_parser(name)
        p.add_argument("episode")
        if name == "render":
            p.add_argument("--turns", help="comma-separated turn indexes")
            p.add_argument("--redo", help="comma-separated turn indexes to regenerate")
            p.add_argument("--budget", type=float, default=3.0, help="refuse to spend more than this many USD")
        if name == "ambient":
            p.add_argument("--yes", action="store_true")
        if name == "assemble":
            p.add_argument("--until", type=float, help="render only the first N seconds")
    a = ap.parse_args()
    {"plan": cmd_plan, "render": cmd_render, "ambient": cmd_ambient, "assemble": cmd_assemble}[a.cmd](a, cfg())


if __name__ == "__main__":
    main()
