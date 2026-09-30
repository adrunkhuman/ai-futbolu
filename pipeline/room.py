"""Put the seats on a sofa and let them talk.

    uv run pipeline/room.py --dry-run      # stub replies, free
    uv run pipeline/room.py --topic "..."  # real run
"""

import argparse
import json
import re
import random
import tomllib
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import postfix
import providers
from director import Director
from news import gather_notes

ROOT = Path(__file__).resolve().parents[1]

RULES = """Jesteś uczestnikiem programu w formacie „czterech facetów na kanapie rozmawia o piłce nożnej” (jak Liga+ Extra, Cafe Futbol, Futbol Futbolu). Postacie panelu są fikcyjne, ale rozmawiacie o prawdziwych, aktualnych wydarzeniach z sekcji „Newsy” i z sieci.

Temat programu: reprezentacja Polski (ostatni mecz, następne mecze, selekcjoner). Ligi i kluby wspominasz tylko mimochodem, jeśli wiążą się z kadrą.

Zasada nadrzędna: nikt nie robi komedii, wszyscy robią ekspertyzę. Naprawdę wierzysz, że mówisz coś mądrego. Nigdy nie mrugasz do widza, nie parodiujesz siebie i nie mówisz, że coś brzmi jak komunał.

Fakty:
- Wyniki, daty, nazwiska, transfery i tabele muszą być prawdziwe: bierz je ze swoich notatek lub z wyszukiwarki. Kiedy nie jesteś pewien, powiedz to jak człowiek („chyba”, „nie pamiętam dokładnie”). Nie wymyślaj wyników ani cytatów prawdziwych osób.
- O prawdziwych piłkarzach, trenerach i działaczach mówisz wyłącznie o tym, co dzieje się na boisku i w publicznych informacjach. Bez plotek, życia prywatnego i oskarżeń.
- Biografia Twojej postaci jest fikcją. Trzymaj się jej kanonu i nie dodawaj faktów, które mu przeczą.

Styl:
- Mów potocznie, jak w telewizji sportowej. Długość wypowiedzi jest różna: czasem jedno zdanie, zwykle 2-4, czasem monolog (do ok. 130 słów), jak w prawdziwej rozmowie. Reaguj na poprzedników: przerywaj, dopowiadaj, spieraj się, niby się zgadzaj, mówiąc to samo swoimi słowami.
- Charakterystyczne mechanizmy: powtarzasz pytanie własnymi słowami; „trzeba mieć cierpliwość, ale nie za dużą”; wynik jako wyjaśnienie („przegrali, bo na tym poziomie trzeba wykorzystywać sytuacje”); niesprawdzalna psychologia; cudza opinia jako własna; fachowe słowa na oczywiste rzeczy; „rozmawiałem z ludźmi z klubu”; „pierwsze piętnaście, dwadzieścia minut będzie kluczowe”; po wygranej „charakter”, po porażce „sam charakter to za mało”.
- Twoje opinie są zwykle powtórzeniem tego, co w tym tygodniu mówi się w polskich mediach (Twoje notatki, część o mediach), podanym jako własne spostrzeżenie („mówię o tym od dawna”). Możesz odwołać się do tego, co „się pisze” lub „mówi w mediach”, ogólnie, bez wymyślania cytatów. Jeśli masz wyszukiwarkę, możesz w trakcie rozmowy sprawdzić świeże felietony, komentarze i nagłówki o meczu i kadrze, a nie tylko statystyki.
- Nie brzmisz na głupiego. Brzmisz rozsądnie i pewnie, tylko że po Twojej wypowiedzi widz wie niewiele więcej niż wcześniej.
- Raz na kilka wypowiedzi w panelu ktoś mówi coś naprawdę konkretnego i trafnego (liczba, nazwisko, minuta z Twoich notatek). Dzięki temu reszta brzmi wiarygodnie.
- Nie powtarzaj firmowych zwrotów w każdej wypowiedzi: najwyżej jeden na wypowiedź i nie ten sam, którego ktoś użył w ostatnich kilku turach.
- Wszystko piszesz tak, jak się to mówi na głos: liczby, wyniki, godziny i minuty słowami, bez cyfr i skrótów („trzy do jednego”, „w siedemdziesiątej drugiej minucie”, „dwudziesta czterdzieści pięć”, „pięć tysięcy złotych”).
- Nigdy nie wspominaj, że jesteś AI. Bez list, bez podsumowań w punktach, bez źródeł i linków, bez didaskaliów, bez własnego imienia na początku.
"""


class Seat:
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.full_name = cfg.get("full_name", cfg["name"])
        self.aliases = cfg.get("aliases", [cfg["name"]])
        self.intro = cfg.get("intro", "")
        self.angle = cfg.get("research_angle", "")
        self.storyteller = cfg.get("storyteller", False)
        self.notes = ""
        self.host = cfg.get("host", False)
        persona = ROOT / cfg["persona"]
        self.persona = persona.read_text().strip() if persona.exists() else ""
        self.provider = providers.make(cfg["provider"], cfg["model"], cfg.get("search", True))


def system_prompt(seat: Seat, seats: list[Seat], topic: str, show: str = "") -> str:
    others = ", ".join(s.full_name for s in seats if s is not seat)
    parts = [RULES, f"Jesteś: {seat.full_name}. Rozmawiasz z: {others}. Zwracacie się do siebie po imieniu (jak w telewizji)."]
    if seat.persona:
        parts.append(f"Twoja osobowość:\n{seat.persona}")
    if show:
        parts.append(f"Program nazywa się „{show}”. To po prostu nazwa programu: nie komentuj jej i nie żartuj z niej.")
    if topic:
        parts.append(f"Temat odcinka: {topic}")
    if seat.notes:
        parts.append(f"Twoje notatki (przygotowałeś je samodzielnie przed programem):\n{seat.notes}")
    return "\n\n".join(parts)


def render(transcript: list[dict]) -> str:
    return "\n".join(f"{t['speaker']}: {t['text']}" for t in transcript) or "(rozmowa jeszcze się nie zaczęła)"


def find_addressee(seats: list[Seat], entry: dict) -> Seat | None:
    """Whoever the last speaker asked a question or opened with by name."""
    others = [s for s in seats if s.name != entry["speaker"]]

    def hits(chunk: str) -> list[Seat]:
        return [s for s in others if any(re.search(rf"\b{re.escape(a)}\b", chunk, re.I) for a in s.aliases)]

    questions = [x for x in re.split(r"(?<=[.!?])\s+", entry["text"]) if x.endswith("?")]
    for chunk in (questions[-1:] + [entry["text"][:30]]):
        h = hits(chunk)
        if len(h) == 1:
            return h[0]
    return None


def pick_next(seats: list[Seat], transcript: list[dict]) -> Seat:
    """Addressee first; otherwise weighted by how long each seat has been silent (never the last speaker)."""
    addressee = find_addressee(seats, transcript[-1])
    if addressee:
        return addressee
    last = transcript[-1]["speaker"]
    recent = [t["speaker"] for t in transcript]
    cands = [s for s in seats if s.name != last]
    weights = [(1 + next((i for i, n in enumerate(reversed(recent)) if n == s.name), len(recent))) ** 1.5 for s in cands]
    return random.choices(cands, weights)[0]


def longest_silent(seats: list[Seat], transcript: list[dict], max_silence: int) -> Seat | None:
    """A panelist who has sat out `max_silence` turns comes in next (some ping-pong is fine, starving a seat is not)."""
    last = transcript[-1]["speaker"]
    gaps = {s.name: next((i for i, t in enumerate(reversed(transcript)) if t["speaker"] == s.name), len(transcript))
            for s in seats}
    silent = [s for s in seats if s.name != last and gaps[s.name] >= max_silence]
    return max(silent, key=lambda s: gaps[s.name]) if silent else None


def run(dry_run: bool, topic: str, out: Path | None, minutes: float | None, max_turns: int | None):
    load_dotenv(ROOT / ".env")
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    ep = cfg["episode"]
    if minutes:
        ep["target_minutes"] = minutes
        ep["hard_max_minutes"] = minutes * 1.15
        ep["closing_reserve_words"] = min(ep["closing_reserve_words"], int(minutes * ep["words_per_minute"] * 0.35))
    ep["max_turns"] = max_turns or ep["max_turns"]
    if dry_run:
        for s in cfg["seat"]:
            s["provider"] = "stub"
    seats = [Seat(c) for c in cfg["seat"]]
    host = next(s for s in seats if s.host)
    show = cfg.get("show", {}).get("name", "")
    dcfg = cfg.get("director", {})
    director = Director(seats, dcfg.get("interrupt_scale", 0.5), dcfg.get("min_gap_since_interrupt", 2),
                        dcfg.get("temperature", 1.0), dcfg.get("stale_threshold", 0.8),
                        dcfg.get("stale_min_turns", 8), dcfg.get("move_min_confidence", 0.5),
                        dcfg.get("steer_threshold", 0.65), dcfg.get("steer_cooldown", 5), dcfg.get("italy_threshold", 0.6),
                        dcfg.get("italy_late_turn", 14), dcfg.get("italy_force_turn", 22)) if dcfg.get("enabled") and not dry_run else None
    topic = topic or cfg["show"]["topic"]
    if not dry_run:
        for name, notes in gather_notes(seats, topic, show).items():
            next(x for x in seats if x.name == name).notes = notes

    wpm = ep["words_per_minute"]
    hard_max_words = ep["hard_max_minutes"] * wpm
    nudge_words = min(ep["target_minutes"] * wpm, hard_max_words) - ep["closing_reserve_words"]
    out = out or ROOT / "transcripts" / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    transcript: list[dict] = []
    words = 0
    # opening (host greets + introduces guest 1) -> intro_reply (guest greets) -> intro_next (host introduces the next
    # guest) ... -> topic -> talking -> closing (guests' last words) -> farewell
    phase, closers = "opening", []
    guests = [s for s in seats if s is not host]
    gi = 0   # guests introduced so far
    story = next((s for s in seats if s.storyteller), None)
    italy_words = re.compile(r"Włoch|Gubbio|mister|espresso")

    def rewrite():
        out.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in transcript))

    max_silence = cfg.get("director", {}).get("max_silence_turns", 6)
    interrupted: set[str] = set()   # speakers who were cut off and haven't spoken since

    def plan_next() -> tuple[Seat, str]:
        """Director picks the next speaker, an optional move hint, and may cut the last line short."""
        nonlocal words
        quiet = longest_silent(seats, transcript, max_silence)
        if director is None:
            return quiet or pick_next(seats, transcript), ""
        italy_told = story is not None and any(x["speaker"] == story.name and italy_words.search(x["text"]) for x in transcript)
        dec = director.decide(transcript, italy_told=italy_told or story is None)
        seat = quiet or find_addressee(seats, transcript[-1]) or next(s for s in seats if s.name == dec.next)
        hint = dec.move_hint
        if quiet:
            hint = "[Dotąd milczałeś: wejdź do rozmowy z własnym zdaniem, możesz wejść komuś w słowo.] "
        if dec.steer and not quiet and transcript[-1]["speaker"] != host.name:
            seat = host
            hint = ("[Rozmowa odpłynęła od kadry: jako prowadzący wróć do niej naturalnie, po swojemu, "
                    f"do jednego z wątków: {cfg['show']['agenda']}. Możesz zadać pytanie komuś po imieniu.] ")
        if dec.italy and story and not quiet and not dec.steer and transcript[-1]["speaker"] != story.name:
            seat = story
            hint = ("[Wpleć jedną krótką włoską anegdotę z Gubbio jako porównanie do tego, o czym mówicie: zwyczajny szczegół "
                    "(skarpetki przygotowane przed treningiem, espresso po treningu, trener mówiący „mister”), z którego robisz "
                    "wniosek o kulturze futbolu. Dwa-trzy zdania, bez owijania.] ")
        if dec.interrupt and dec.cut_text:
            last = transcript[-1]
            words -= len(last["text"].split()) - len(dec.cut_text.split())
            last["interrupted_full"], last["text"] = last["text"], dec.cut_text
            rewrite()
            hint = "[Przerywasz przedmówcy w pół zdania: wchodzisz mu w słowo. Zacznij od razu, bez powitania.] " + hint
            interrupted.add(last["speaker"])
            print(f"      ~~ CUT by {seat.name}: {last['text']}\n", flush=True)
        if seat.name in interrupted:
            hint = "[Ktoś Ci wszedł w słowo: odpowiedz krótko i dokończ myśl, nie powtarzaj tego, co już powiedziałeś.] " + hint
            interrupted.discard(seat.name)
        with out.with_suffix(".director.jsonl").open("a") as f:
            f.write(json.dumps({"turn": len(transcript), "chosen": seat.name, "interrupt": dec.interrupt, **dec.detail}, ensure_ascii=False) + "\n")
        return seat, (hint + "\n" if hint and not hint.endswith("\n") else hint)

    def say(seat: Seat, hint: str = ""):
        nonlocal words
        user = f"{render(transcript)}\n\n{hint}Teraz Twoja kolej ({seat.name}). Powiedz swoją kwestię."
        text = providers.complete_with_retry(seat.provider, system_prompt(seat, seats, topic, show), user)
        text = re.sub(rf"^\W*(?:{re.escape(seat.name)}|{re.escape(seat.full_name)})\W*:\s*", "", text).strip()
        text = providers.clip_words(text, ep.get("closing_turn_max_words", 70) if phase == "closing"
                                    else 8 if phase == "intro_reply" else 50 if phase in ("opening", "intro_next")
                                    else ep.get("turn_max_words", 170))
        entry = {"speaker": seat.name, "text": text, "phase": phase}
        transcript.append(entry)
        words += len(text.split())
        with out.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[{words / ep['words_per_minute']:4.1f} min] {seat.name}: {text}\n", flush=True)

    for _ in range(ep["max_turns"]):
        if phase == "opening":
            g = guests[0]
            say(host, f"[Zaczynasz odcinek programu „{show}”. Przywitaj widzów (nazwy programu nie odmieniaj), krótko się przedstaw i przedstaw PIERWSZEGO gościa, tylko jego: {g.full_name}: {g.intro}. Zakończ na nim, niech on odpowie. Innych gości jeszcze nie przedstawiaj, tematu nie rozwijaj.]\n")
            phase = "intro_reply"
        elif phase == "intro_reply":
            say(guests[gi], "[Prowadzący właśnie Cię przedstawił: odpowiedz jednym krótkim powitaniem, np. „Dzień dobry państwu” albo „Dzień dobry”, i nic więcej.]\n")
            gi += 1
            phase = "intro_next" if gi < len(guests) else "topic"
        elif phase == "intro_next":
            g = guests[gi]
            say(host, f"[Przedstaw kolejnego gościa, tylko jego, jednym krótkim zdaniem: {g.full_name}: {g.intro}. Bez powitania widzów i bez tematu. Zakończ na nim, niech on odpowie.]\n")
            phase = "intro_reply"
        elif phase == "topic":
            say(host, f"[Wprowadź temat odcinka: {topic} Plan rozmowy: {cfg['show']['agenda']}. Zacznij od pierwszego punktu i zadaj pierwsze pytanie jednej osobie, po imieniu.]\n")
            phase = "talking"
        elif phase == "talking":
            if words >= nudge_words:
                say(host, "[PRODUCENT w słuchawce: zostały około dwie minuty, zapowiedz, że zbliżacie się do końca, i oddaj głos kolegom na ostatnie słowa. Jeszcze się NIE żegnaj. Powiedz to naturalnie, po swojemu.]\n")
                closers = [s for s in seats if s is not host]
                random.shuffle(closers)
                phase = "closing"
            else:
                seat, hint = plan_next()
                say(seat, hint)
        elif phase == "closing":
            if closers and words < hard_max_words:
                say(closers.pop(), "[Odcinek się kończy: powiedz swoje ostatnie zdanie: czy Urban powinien zostać selekcjonerem i dlaczego. Nie żegnaj się z widzami, to zrobi prowadzący.]\n")
            else:
                say(host, "[POŻEGNANIE: podziękuj widzom i zakończ program.]\n")
                break
    print(f"post-hoc fixes: {postfix.fix_file(out)} fields")
    print(f"Saved {out} ({words} words, ~{words / ep['words_per_minute']:.1f} min)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="stub providers, no API calls")
    ap.add_argument("--topic", default="")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--minutes", type=float, help="override target episode length")
    ap.add_argument("--max-turns", type=int)
    a = ap.parse_args()
    run(a.dry_run, a.topic, a.out, a.minutes, a.max_turns)
