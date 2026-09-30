"""Cut the first N turns (e.g. the introductions) out of a rendered episode, keeping timing valid.

    uv run tools/trim.py audio/episode-1.mp3 --skip 6      # drops turns 0-5, writes audio/episode-1.trim.mp3 + .trim.timing.json
Uses the per-turn timing written by voice.py; the MP3 is stream-copied (cut on a frame boundary, ~26 ms).
"""

import argparse
import json
import subprocess
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("mp3", type=Path)
ap.add_argument("--skip", type=int, required=True, help="number of leading timing entries to drop")
a = ap.parse_args()

timing_path = a.mp3.with_suffix(".timing.json")
timing = json.loads(timing_path.read_text())
start = timing[a.skip]["start"]
out = a.mp3.with_suffix(".trim.mp3")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(a.mp3), "-c", "copy", str(out)], check=True)
kept = [{**t, "start": round(t["start"] - start, 3), "end": round(t["end"] - start, 3)} for t in timing[a.skip:]]
a.mp3.with_suffix(".trim.timing.json").write_text(json.dumps(kept, ensure_ascii=False, indent=1))
print(f"dropped {a.skip} turns ({start:.1f}s); wrote {out} and timing for {len(kept)} turns; first kept: {kept[0]['speaker']}")
