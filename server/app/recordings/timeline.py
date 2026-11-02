"""Cálculo de intervalos gravados, lacunas e resumo por dia (puro, sem I/O)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# Dois segmentos consecutivos são considerados contínuos se a diferença for menor que isto.
# Absorve o arredondamento de keyframe e a latência de anotação do horário de cada segmento.
DEFAULT_GAP_TOLERANCE = 2.0


def merge_intervals(spans: list[tuple[float, float]], tolerance: float = DEFAULT_GAP_TOLERANCE) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if e <= s:
            continue
        if out and s <= out[-1][1] + tolerance:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


def clip(intervals: list[tuple[float, float]], start: float, end: float) -> list[tuple[float, float]]:
    return [(max(s, start), min(e, end)) for s, e in intervals if e > start and s < end]


def gaps(intervals: list[tuple[float, float]], start: float, end: float, min_gap: float = DEFAULT_GAP_TOLERANCE) -> list[tuple[float, float]]:
    out = []
    cursor = start
    for s, e in intervals:
        if s - cursor >= min_gap:
            out.append((cursor, s))
        cursor = max(cursor, e)
    if end - cursor >= min_gap:
        out.append((cursor, end))
    return out


def locate(intervals: list[tuple[float, float]], t: float) -> dict:
    """Diz se ``t`` está gravado; caso contrário aponta o próximo e o anterior disponíveis."""
    prev_end = None
    for s, e in intervals:
        if s <= t < e:
            return {"recorded": True, "interval": [s, e]}
        if s > t:
            return {"recorded": False, "next_available": s, "previous_end": prev_end}
        prev_end = e
    return {"recorded": False, "next_available": None, "previous_end": prev_end}


def day_bounds(day: date, tz: ZoneInfo) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = start + timedelta(days=1)
    # Normaliza via UTC para respeitar dias de 23/25 h em fusos com horário de verão.
    nxt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return start.timestamp(), nxt.timestamp()


def month_days(year: int, month: int) -> list[date]:
    d = date(year, month, 1)
    out = []
    while d.month == month:
        out.append(d)
        d += timedelta(days=1)
    return out


def recorded_seconds_per_day(spans: list[tuple[float, float]], year: int, month: int, tz: ZoneInfo) -> dict[str, float]:
    merged = merge_intervals(spans)
    result: dict[str, float] = {}
    for d in month_days(year, month):
        s, e = day_bounds(d, tz)
        total = sum(b - a for a, b in clip(merged, s, e))
        if total > 0:
            result[d.isoformat()] = round(total, 1)
    return result
