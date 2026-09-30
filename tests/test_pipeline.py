import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Production scripts run directly rather than as an installed package.
sys.path.insert(0, str(ROOT / "pipeline"))

import room
import subtitles
import video
import voice


class OfflineTests(unittest.TestCase):
    def test_audio_dry_run_does_not_call_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "sample.jsonl"
            transcript.write_text(json.dumps({"speaker": "Radecki", "text": "Dzień dobry państwu."}) + "\n")
            with (
                patch.object(sys, "argv", ["voice.py", str(transcript), "--dry-run"]),
                patch.object(voice, "TypeSafeClient", side_effect=AssertionError("remote director")),
                patch.object(voice.subprocess, "run", side_effect=AssertionError("external CLI")),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                voice.main()
            self.assertIn("offline estimate", output.getvalue())

    def test_room_dry_run_produces_a_transcript_without_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "new" / "sample.jsonl"
            with (
                patch.object(room, "gather_notes", side_effect=AssertionError("remote research")),
                patch.object(voice.subprocess, "run", side_effect=AssertionError("external CLI")),
                patch("director.TypeSafeClient", side_effect=AssertionError("remote director")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                room.run(True, "", transcript, 1, 12)
            turns = [json.loads(line) for line in transcript.read_text().splitlines()]
            self.assertTrue(turns)
            self.assertEqual({t["speaker"] for t in turns}, {"Radecki", "Kierski", "Cieślak", "Rybarczyk"})


class PipelineTests(unittest.TestCase):
    def test_interrupted_turns_remain_separate_for_audio(self):
        turns = [
            {"speaker": "Radecki", "text": "Dzień dobry."},
            {"speaker": "Radecki", "text": "Panowie —", "interrupted_full": "Panowie, zaczynamy."},
            {"speaker": "Kierski", "text": "Ja powiem."},
        ]
        merged = voice.merge_same_speaker(turns)
        self.assertIn("interrupted_full", merged[1])
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[1]["text"], "Panowie —")
        self.assertEqual(turns[0]["text"], "Dzień dobry.")

    def test_captions_do_not_overlap(self):
        turns = [
            {"turn": 0, "speaker": "Radecki", "text": "Dzień dobry.", "start": 0, "end": 3},
            {"turn": 1, "speaker": "Kierski", "text": "Witam państwa.", "start": 2.9, "end": 5},
        ]
        cues = subtitles.make_cues(turns)
        self.assertLessEqual(cues[0]["end"], cues[1]["start"])
        self.assertEqual(cues[-1]["end"], 5)

    def test_long_turn_uses_head_and_tail_closeups(self):
        config = video.cfg()
        timing = [{"turn": 0, "start": 0, "end": 60, "speaker": "Radecki"}]
        segments = video.plan(timing, config)
        self.assertEqual([(s["t0"], s["t1"]) for s in segments], [(3, 13), (51, 56)])

    def test_published_transcript_matches_final_timing_and_captions(self):
        base = ROOT / "episode-1" / "episode-1"
        transcript = [json.loads(line) for line in base.with_suffix(".jsonl").read_text().splitlines()]
        timing = json.loads(base.with_suffix(".timing.json").read_text())
        self.assertEqual([(t["speaker"], t["text"]) for t in transcript],
                         [(t["speaker"], t["text"]) for t in timing])
        cues = subtitles.make_cues(timing)
        self.assertTrue(all(c["start"] < c["end"] for c in cues))
        self.assertTrue(all(a["end"] <= b["start"] for a, b in zip(cues, cues[1:])))
        with tempfile.TemporaryDirectory() as tmp:
            regenerated = Path(tmp) / "episode-1"
            subtitles.save(cues, regenerated)
            for suffix in (".srt", ".ass"):
                self.assertEqual(base.with_suffix(suffix).read_bytes(), regenerated.with_suffix(suffix).read_bytes())


if __name__ == "__main__":
    unittest.main()
