"""Tests for sp_stream_dl request header selection."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/teams-video-dl/scripts"))
import sp_stream_dl  # noqa: E402


def test_segment_requests_strip_key_and_bearer_auth():
    old = dict(sp_stream_dl.HEADERS)
    try:
        sp_stream_dl.HEADERS.clear()
        sp_stream_dl.HEADERS.update({
            "Cookie": "FedAuth=FAKE; rtFa=FAKE",
            "X-SPOPacToken": "FAKEPAC",
            "Authorization": "Bearer FAKE",
            "X-Authorization": "Bearer MEDIAFAKE",
        })
        headers = sp_stream_dl.headers_for_url(
            "https://contoso-my.sharepoint.com/_api/v2.1/drives/b!D/items/01I/oneDrive.transcode"
        )
    finally:
        sp_stream_dl.HEADERS.clear()
        sp_stream_dl.HEADERS.update(old)

    assert headers == {"Cookie": "FedAuth=FAKE; rtFa=FAKE"}


def test_key_requests_keep_pac_auth():
    old = dict(sp_stream_dl.HEADERS)
    try:
        sp_stream_dl.HEADERS.clear()
        sp_stream_dl.HEADERS.update({
            "Cookie": "FedAuth=FAKE; rtFa=FAKE",
            "X-SPOPacToken": "FAKEPAC",
            "Authorization": "Bearer FAKE",
        })
        headers = sp_stream_dl.headers_for_url(
            "https://eu-mediap.svc.ms/transform/VideoProtectionKey?provider=Spo&kid=FAKE"
        )
    finally:
        sp_stream_dl.HEADERS.clear()
        sp_stream_dl.HEADERS.update(old)

    assert headers["X-SPOPacToken"] == "FAKEPAC"
    assert headers["Authorization"] == "Bearer FAKE"


def test_full_download_uses_explicit_work_dir(tmp_path, monkeypatch):
    work = tmp_path / "tmp" / "stream"
    out_path = tmp_path / "target" / "recording.mp4"
    jobs = [{"type": "video", "media_urls": []}, {"type": "audio", "media_urls": []}]
    used_caches = []

    def fake_download_track(job, key, cache):
        used_caches.append(cache)
        segment = cache / "init.m4s"
        cache.mkdir(parents=True, exist_ok=True)
        segment.write_bytes(b"")
        return [segment]

    def fake_run(cmd, check):
        assert check is True
        assert cmd[-1] == str(out_path)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(sp_stream_dl, "download_track", fake_download_track)
    monkeypatch.setattr(sp_stream_dl, "heal_legacy_padding", lambda data: data)
    monkeypatch.setitem(sys.modules, "subprocess", SimpleNamespace(run=fake_run))

    sp_stream_dl.cmd_full(jobs, b"0123456789abcdef", str(out_path), work)

    assert used_caches == [work / ".cache_video", work / ".cache_audio"]
    assert (work / "video.mp4").exists()
    assert (work / "audio.mp4").exists()
