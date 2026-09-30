"""Shadow director: Jev (TypeSafe System One) makes the small on-air decisions.

Jev has no system prompt and doesn't generate text. It gets a `state` (what the
show is, who the panelists are, the discussion so far) plus typed questions, and
returns probabilities. Code owns everything else: masking the last speaker,
sampling, thresholds, cutting the line, and the wording of the hints. Instructions
are in English (Jev's strongest language); the discussion itself stays Polish.
Questions are literal and point at state fields by name, as the Jev docs advise.
"""

import random
import re
from dataclasses import dataclass, field

from typesafe_sdk import Choice, Noul, NoulCriteria, TypeSafeClient

SHOW = (
    "A fictional Polish football TV panel of four pundits sitting on a sofa, in the style of Liga+ Extra and "
    "Futbol Futbolu. They discuss real, current football news. Every panelist sincerely believes he is giving "
    "expert analysis; most of what they say is either a concrete fact from the news or a confident cliche. "
    "The episode is about the Polish national team: the lost game in Sweden, the next match against Romania, "
    "and whether coach Jan Urban should be sacked or kept. "
    "Like a real talk show, panelists address each other by first name, disagree, interrupt, "
    "and repeat each other in different words."
)

PANELISTS = {
    "Radecki": "Michał Radecki, the host (43). Asks short questions, has his own pet theories (mountains and fitness, "
               "locker-room culture, project continuity) and argues like everyone else. Never played professionally.",
    "Kierski": "Robert 'Bolo' Kierski (49), ex-player with two Poland caps and an Italian lower-league spell. "
               "Explains everything through dressing-room experience, mentality, duels and 'entering the match'.",
    "Cieślak": "Adrian Cieślak (33), tactical analyst from the internet. Translates the obvious into structural jargon "
               "(half-spaces, rest defence). Defensive about not having played.",
    "Rybarczyk": "Jerzy Rybarczyk (66), veteran columnist. Turns conventional wisdom into timeless-sounding aphorisms "
                 "and historical parallels. Calm, courtly, reduces modern analysis to old sayings.",
}

# What the next speaker should do. Keys are Jev options; values are the Polish hint sent to the speaker.
MOVES = {
    "free": ("The next speaker just reacts naturally, no special move.", ""),
    "disagree": ("The next speaker disagrees with the last statement, openly.",
                 "Nie zgadzasz się z przedmówcą i mówisz to wprost, po swojemu."),
    "agree_rephrase": ("The next speaker agrees but restates the same point in his own words, as if correcting it.",
                       "Zgadzasz się z przedmówcą, ale mówisz to swoimi słowami, jakbyś go poprawiał."),
    "concrete_fact": ("The next speaker brings in one concrete fact from his notes (a number, a name, a minute).",
                      "Wtrąć jedną konkretną informację ze swoich notatek (liczba, nazwisko, minuta) i skomentuj ją."),
    "cliche": ("The next speaker answers with a confident cliche that sounds wise but adds nothing new.",
               "Odpowiedz pewnym siebie komunałem, który brzmi mądrze, a nie dodaje nic nowego."),
    "anecdote": ("The next speaker tells a short anecdote from his own career or memory.",
                 "Opowiedz krótką anegdotę z własnej biografii (trzymaj się swojego kanonu) i wyciągnij z niej ogólny wniosek."),
    "ask": ("The next speaker asks one colleague a short direct question.",
            "Zadaj jednemu z kolegów krótkie pytanie po imieniu."),
    "new_topic": ("The next speaker moves to another angle of the national team story (the next match, the lineup, the group, the coach's future).",
                  "Przejdź do innego wątku kadry (mecz z Rumunią, skład, sytuacja w grupie albo przyszłość selekcjonera) i zapowiedz to naturalnie."),
}


@dataclass
class Decision:
    next: str
    move_hint: str = ""
    interrupt: bool = False
    cut_text: str | None = None   # replacement text for the last statement when it is interrupted
    italy: bool = False           # nudge the storyteller (Kierski) to tell one short Italian anecdote
    steer: bool = False           # the host should pull the discussion back to the national team
    detail: dict = field(default_factory=dict)


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s]


def interrupted_text(text: str, cut_after: int) -> str:
    parts = sentences(text)
    if len(parts) >= 2:
        kept = " ".join(parts[:max(1, min(cut_after, len(parts) - 1))])
    else:
        words = text.split()
        kept = " ".join(words[: max(4, int(len(words) * 0.6))])
    return kept.rstrip(".!?…,;: ") + " —"


class Director:
    def __init__(self, seats, interrupt_scale: float = 0.5, min_gap_since_interrupt: int = 2,
                 temperature: float = 1.0, stale_threshold: float = 0.8, stale_min_turns: int = 8,
                 move_min_confidence: float = 0.5, steer_threshold: float = 0.65, steer_cooldown: int = 5,
                 italy_threshold: float = 0.6, italy_late_turn: int = 14, italy_force_turn: int = 22,
                 model: str = "jev-latest"):
        self.names = [s.name for s in seats]
        self.scale = interrupt_scale   # P(interrupt) = Jev's noul * scale; Jev's raw values cluster near 0.5
        self.min_gap = min_gap_since_interrupt
        self.temperature = temperature
        # Real panels happily beat one point to death, so "stale" is a rare, late nudge.
        self.stale_threshold = stale_threshold
        self.stale_min_turns = stale_min_turns
        self.move_min_confidence = move_min_confidence
        self.steer_threshold = steer_threshold
        self.steer_cooldown = steer_cooldown
        self.since_steer = 99
        self.italy_threshold, self.italy_late_turn, self.italy_force_turn = italy_threshold, italy_late_turn, italy_force_turn
        self.client = TypeSafeClient(model=model)
        self.since_interrupt = 99

    def _state(self, transcript: list[dict]) -> dict:
        last = transcript[-1]
        turns_since = {n: next((i for i, t in enumerate(reversed(transcript)) if t["speaker"] == n), len(transcript))
                       for n in self.names}
        return {
            "show": SHOW,
            "panelists": PANELISTS,
            "discussion_so_far": [f"{t['speaker']}: {t['text']}" for t in transcript[:-1]],
            "last_statement": {
                "speaker": last["speaker"],
                "sentences": {str(i + 1): s for i, s in enumerate(sentences(last["text"]))},
            },
            "turns_since_each_panelist_last_spoke": turns_since,
        }

    def decide(self, transcript: list[dict], italy_told: bool = True) -> Decision:
        last = transcript[-1]
        others = [n for n in self.names if n != last["speaker"]]
        sents = sentences(last["text"])[:6]
        questions = {
            "next_speaker": Choice(
                instructions=(
                    "Who would most naturally speak right after `last_statement` in this panel? Consider: who was "
                    "addressed by name or asked a question, who would disagree with what was said, whose pet topic "
                    "was touched, and who has been quiet for the longest (`turns_since_each_panelist_last_spoke`)."
                ),
                criteria={n: PANELISTS[n] for n in others},
            ),
            "interrupt": Noul(
                instructions=(
                    "While `last_statement` was being said, would one of the other panelists plausibly have cut in "
                    "before the speaker finished? Yes if the speaker was rambling, repeating a cliche, or saying "
                    "something another panelist would strongly object to."
                ),
                criteria=NoulCriteria(
                    true="Another panelist would jump in mid-statement.",
                    false="The speaker would be allowed to finish.",
                ),
            ),
            "stale": Noul(
                instructions=(
                    "Do the last four statements in `discussion_so_far` and `last_statement` keep repeating the same "
                    "point or the same topic that was already covered earlier in `discussion_so_far`?"
                ),
                criteria=NoulCriteria(
                    true="The panel keeps going around the same point.",
                    false="The conversation is still adding new points or topics.",
                ),
            ),
            "off_topic": Noul(
                instructions=(
                    "Look at the last three statements (`last_statement` and the end of `discussion_so_far`). Have the "
                    "panelists drifted away from the Polish national team (the game in Sweden, the next match against "
                    "Romania, the group, coach Jan Urban's future) to league or club matters, transfers, or another topic?"
                ),
                criteria=NoulCriteria(
                    true="The last statements are mainly about a league, a club, or something else than the national team.",
                    false="The last statements are about the national team, its game, its next steps or its coach.",
                ),
            ),
            "italy_fits": Noul(
                instructions=(
                    "Would a short, casual comparison to a mundane detail from a former footballer's time in a small Italian "
                    "lower-league club (how organised it was, kit laid out before training, espresso after training) fit "
                    "naturally right after `last_statement`, as something a former player would add to the point being made?"
                ),
                criteria=NoulCriteria(
                    true="A brief comparison to lower-league Italy would fit here without derailing the point.",
                    false="It would feel forced or off-topic here.",
                ),
            ),
            "next_move": Choice(
                instructions="What should the next speaker do in response to `last_statement`?",
                criteria={k: v[0] for k, v in MOVES.items()},
            ),
        }
        if len(sents) >= 2:
            questions["cut_point"] = Choice(
                instructions=(
                    "If another panelist cut in during `last_statement`, after which sentence would the cut most "
                    "plausibly come? Sentence numbers refer to `last_statement.sentences`."
                ),
                criteria={f"after_{i}": f"Right after sentence {i}: {s}" for i, s in enumerate(sents[:-1], 1)},
            )
        ans = self.client.system_one(state=self._state(transcript), questions=questions).answers

        probs = ans["next_speaker"].probabilities
        weights = [max(probs.get(n, 0.0), 1e-6) ** (1 / self.temperature) for n in others]
        nxt = random.choices(others, weights)[0]

        p_int = ans["interrupt"].noul
        interrupt = random.random() < p_int * self.scale and self.since_interrupt >= self.min_gap
        self.since_interrupt = 0 if interrupt else self.since_interrupt + 1
        cut_text = None
        if interrupt:
            k = int(ans["cut_point"].choice.removeprefix("after_")) if "cut_point" in ans else 1
            cut_text = interrupted_text(last["text"], k)

        steer = ans["off_topic"].noul >= self.steer_threshold and self.since_steer >= self.steer_cooldown
        self.since_steer = 0 if steer else self.since_steer + 1
        n = len(transcript)
        italy = (not italy_told and n >= 8 and
                 (ans["italy_fits"].noul >= (self.italy_threshold if n < self.italy_late_turn else 0.4) or n >= self.italy_force_turn))
        move = ans["next_move"].choice
        if len(transcript) >= self.stale_min_turns and ans["stale"].noul >= self.stale_threshold:
            hint = MOVES["new_topic"][1]
        elif ans["next_move"].confidence >= self.move_min_confidence:
            hint = MOVES[move][1]
        else:
            hint = ""
        return Decision(
            italy=italy,
            steer=steer,
            next=nxt,
            move_hint=hint,
            interrupt=interrupt,
            cut_text=cut_text,
            detail={
                "next_probs": {k: round(v, 2) for k, v in probs.items()},
                "p_interrupt": round(p_int, 2),
                "p_stale": round(ans["stale"].noul, 2),
                "p_off_topic": round(ans["off_topic"].noul, 2),
                "p_italy": round(ans["italy_fits"].noul, 2),
                "move": move,
                "move_confidence": round(ans["next_move"].confidence, 2),
            },
        )
