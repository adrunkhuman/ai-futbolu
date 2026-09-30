"""Each panelist prepares on his own: one search pass per seat, from that persona's angle.

There is no shared brief. Seats may end up with different facts, exactly like real pundits who prepared
separately; they can still search during the discussion (search stays enabled per turn).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import providers

PROMPT = (
    "Dziś jest {today}. Przygotowujesz się do odcinka programu „{show}”. Temat: {topic}\n"
    "Przeszukaj sieć samodzielnie i zbierz notatki dla siebie, po polsku, 10-15 punktów: fakty (wynik, składy, "
    "tabela grupy, terminarz, kontuzje), wypowiedzi selekcjonera i piłkarzy, to, co i kto pisze i mówi w polskich "
    "mediach (podaj medium lub osobę i tezę), oraz spór o przyszłość selekcjonera. "
    "Szczególnie zwróć uwagę na: {angle}. "
    "Cytuj wyłącznie to, co naprawdę znalazłeś, słowo w słowie; niczego nie wymyślaj. Bez wstępu, bez linków."
)


def gather_notes(seats, topic: str, show: str) -> dict[str, str]:
    """{seat name: private notes}, gathered in parallel."""
    def one(seat) -> tuple[str, str]:
        prompt = PROMPT.format(today=date.today(), show=show, topic=topic, angle=seat.angle)
        system = f"Jesteś {seat.full_name}. Przygotowujesz własne notatki do programu; zachowujesz się jak rzetelny research-owiec."
        return seat.name, providers.complete_with_retry(seat.provider, system, prompt)

    with ThreadPoolExecutor(max_workers=len(seats)) as pool:
        return dict(pool.map(one, seats))
