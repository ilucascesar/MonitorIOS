import json

from app.streaming.hls import build_vod_playlist


def seg(i, start, dur, run="r1", parts=None, container="ts", init_len=0):
    return {
        "id": i, "start_ts": start, "end_ts": start + dur, "duration": dur, "run_id": run, "container": container,
        "width": 640, "height": 360, "codec": "h264", "size": 1000, "init_len": init_len,
        "parts": json.dumps(parts if parts is not None else [[0, 600, dur / 2], [600, 400, dur / 2]]),
    }


def url(s):
    return f"/api/v1/media/{s['id']}.{s['container']}?mt=T"


def test_vod_playlist_byteranges_and_program_date_time():
    segs = [seg(1, 1000.0, 60.0), seg(2, 1060.0, 60.0)]
    text = build_vod_playlist(segs, url)
    assert text.startswith("#EXTM3U")
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and text.rstrip().endswith("#EXT-X-ENDLIST")
    assert text.count("#EXT-X-BYTERANGE:600@0") == 2 and text.count("#EXT-X-BYTERANGE:400@600") == 2
    assert "#EXT-X-DISCONTINUITY" not in text  # contíguos, mesma execução
    assert "#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:16:40.000Z" in text
    assert "#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:17:10.000Z" in text
    assert "#EXT-X-TARGETDURATION:30" in text


def test_discontinuity_on_gap_and_new_run():
    segs = [seg(1, 0, 60), seg(2, 300, 60), seg(3, 360, 60, run="r2")]
    text = build_vod_playlist(segs, url)
    assert text.count("#EXT-X-DISCONTINUITY") == 2


def test_window_trims_parts_outside_range():
    segs = [seg(1, 0, 60), seg(2, 60, 60)]
    text = build_vod_playlist(segs, url, start=40, end=70)
    # Parte [30,60) do seg 1 e parte [60,90) do seg 2
    assert text.count("#EXTINF") == 2


def test_fmp4_uses_ext_x_map():
    s = seg(1, 0, 60, container="mp4", init_len=800, parts=[[800, 5000, 60]])
    text = build_vod_playlist([s], url)
    assert '#EXT-X-MAP:URI="/api/v1/media/1.mp4?mt=T",BYTERANGE="800@0"' in text
    assert "#EXT-X-VERSION:7" in text


def test_part_durations_scaled_to_real_duration():
    s = seg(1, 0, 61.0, parts=[[0, 10, 30.0], [10, 10, 30.0]])
    text = build_vod_playlist([s], url)
    assert "#EXTINF:30.500," in text
