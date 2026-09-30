"""Create surname-labelled SRT and ASS captions from rendered audio turn timings.

    uv run pipeline/subtitles.py audio/episode-1.timing.json video/episode-1

Turn boundaries are exact. Within each turn, caption times are estimated by
text length, not aligned to individual spoken words.
"""

import argparse
import json
import re
import textwrap
from pathlib import Path


def split_captions(speaker: str, text: str, width: int = 48) -> list[str]:
    """Prefer sentence boundaries; leave room for the surname on the first line."""
    captions, words = [], text.split()
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        lines = textwrap.wrap(f"{speaker}: {candidate}", width,
                              break_long_words=False, break_on_hyphens=False)
        if current and len(lines) > 2:
            captions.append(" ".join(current))
            current = []
        current.append(word)
        if len(" ".join(current)) >= 35 and re.search(r'[.!?][”"»)]?$', word):
            captions.append(" ".join(current))
            current = []
    if current:
        captions.append(" ".join(current))
    # Avoid flashing a lone trailing word when a line limit split a sentence.
    for i in range(len(captions) - 1, 0, -1):
        if len(captions[i]) >= 20:
            continue
        combined = captions[i - 1] + " " + captions[i]
        lines = textwrap.wrap(f"{speaker}: {combined}", width,
                              break_long_words=False, break_on_hyphens=False)
        if len(lines) <= 2:
            captions[i - 1] = combined
            captions.pop(i)
        else:
            previous, trailing = captions[i - 1].split(), captions[i].split()
            while len(" ".join(trailing)) < 25 and len(" ".join(previous)) > 35:
                trailing.insert(0, previous.pop())
            captions[i - 1], captions[i] = " ".join(previous), " ".join(trailing)
    return captions


def make_cues(turns: list[dict]) -> list[dict]:
    cues = []
    for i, turn in enumerate(turns):
        start, end = float(turn["start"]), float(turn["end"])
        # Tiny overlaps in the generated dialogue should not stack captions.
        if i + 1 < len(turns):
            end = min(end, float(turns[i + 1]["start"]))
        if end <= start:
            raise ValueError(f"Invalid timing for turn {turn['turn']}")
        parts = split_captions(turn["speaker"], turn["text"])
        if not parts:
            raise ValueError(f"Empty text for turn {turn['turn']}")
        total = sum(len(part) for part in parts)
        consumed = 0
        for part in parts:
            cue_start = start + (end - start) * consumed / total
            consumed += len(part)
            cue_end = start + (end - start) * consumed / total
            lines = textwrap.wrap(f"{turn['speaker']}: {part}", 48,
                                  break_long_words=False, break_on_hyphens=False)
            cues.append({"start": cue_start, "end": cue_end,
                         "lines": lines, "speaker": turn["speaker"]})
    return cues


def timestamp(seconds: float, ass: bool = False) -> str:
    precision = 100 if ass else 1000
    ticks = round(seconds * precision)
    whole, fraction = divmod(ticks, precision)
    minutes, second = divmod(whole, 60)
    hour, minute = divmod(minutes, 60)
    if ass:
        return f"{hour}:{minute:02}:{second:02}.{fraction:02}"
    return f"{hour:02}:{minute:02}:{second:02},{fraction:03}"


def save(cues: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    srt = []
    events = []
    for i, cue in enumerate(cues, 1):
        text = "\n".join(cue["lines"])
        srt.append(f"{i}\n{timestamp(cue['start'])} --> {timestamp(cue['end'])}\n{text}\n")
        # Literal transcript characters must not become ASS formatting commands.
        ass_text = text.replace("\\", "/").replace("{", "(").replace("}", ")")
        label = cue["speaker"] + ":"
        ass_text = r"{\b1}" + ass_text[:len(label)] + r"{\b0}" + ass_text[len(label):]
        ass_text = ass_text.replace("\n", r"\N")
        events.append(f"Dialogue: 0,{timestamp(cue['start'], True)},"
                      f"{timestamp(cue['end'], True)},Default,,0,0,0,,{ass_text}")
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 832
PlayResY: 480
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Liberation Sans,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,0,2,35,35,22,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    output.with_suffix(".srt").write_text("\n".join(srt), encoding="utf-8")
    output.with_suffix(".ass").write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timing", type=Path)
    parser.add_argument("output", type=Path, help="Output basename, without extension")
    args = parser.parse_args()
    turns = json.loads(args.timing.read_text(encoding="utf-8"))
    cues = make_cues(turns)
    save(cues, args.output)
    print(f"Wrote {len(cues)} captions; within-turn timing is approximate.")


if __name__ == "__main__":
    main()
