from datetime import date
from zoneinfo import ZoneInfo

from app.recordings import timeline as tl

SP = ZoneInfo("America/Sao_Paulo")


def test_merge_absorbs_small_gaps_and_overlaps():
    spans = [(0, 60), (60.8, 120), (119, 180), (200, 260)]
    assert tl.merge_intervals(spans) == [(0, 180), (200, 260)]


def test_merge_ignores_invalid_and_unsorted():
    assert tl.merge_intervals([(100, 90), (50, 70), (0, 49)]) == [(0, 70)]


def test_gaps_between_and_at_edges():
    iv = [(10, 20), (30, 40)]
    assert tl.gaps(iv, 0, 50) == [(0, 10), (20, 30), (40, 50)]
    assert tl.gaps([(0, 50)], 0, 50) == []


def test_locate_inside_gap_offers_next():
    iv = [(100, 200), (300, 400)]
    assert tl.locate(iv, 150) == {"recorded": True, "interval": [100, 200]}
    r = tl.locate(iv, 250)
    assert r["recorded"] is False and r["next_available"] == 300 and r["previous_end"] == 200
    assert tl.locate(iv, 500)["next_available"] is None


def test_clip():
    assert tl.clip([(0, 100), (150, 300)], 50, 200) == [(50, 100), (150, 200)]


def test_day_bounds_local_timezone():
    s, e = tl.day_bounds(date(2026, 11, 1), SP)
    assert e - s == 86400
    # 01/11/2026 00:00 em São Paulo (UTC-3) == 03:00 UTC
    assert s == 1793502000


def test_recorded_seconds_per_day_splits_at_midnight():
    s, e = tl.day_bounds(date(2026, 11, 1), SP)
    spans = [(e - 600, e + 300)]  # 23:50 -> 00:05
    per_day = tl.recorded_seconds_per_day(spans, 2026, 11, SP)
    assert per_day == {"2026-11-01": 600.0, "2026-11-02": 300.0}
