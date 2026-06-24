"""Tests for har_extract — synthetic HAR, no network (all bodies inline)."""
import base64
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/teams-video-dl/scripts"))
import har_extract  # noqa: E402

COOKIE = "FedAuth=FAKE==; rtFa=FAKE/x+y=="
KEY16 = b"0123456789abcdef"  # exactly 16 bytes
PAC = "FAKEPAC"

TRANSCODE_MPD = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:DASH:schema:MPD:2011" xmlns:sea="urn:mpeg:dash:schema:sea:2012">
  <BaseURL>https://contoso-my.sharepoint.com/personal/jane_contoso_com/_api/v2.1/drives/b!TRANSCODE/items/01TRANSCODE/oneDrive.transcode/</BaseURL>
  <Period>
    <AdaptationSet contentType="video">
      <ContentProtection>
        <sea:CryptoPeriod keyUriTemplate="https://eu-mediap.svc.ms/transform/VideoProtectionKey?provider=Spo&amp;kid=fake" IV="0x00000000000000000000000000000000" />
      </ContentProtection>
      <Representation id="1" />
      <SegmentTemplate initialization="$RepresentationID$/init.m4s" media="$RepresentationID$/$Time$.m4s">
        <SegmentTimeline><S d="1" /></SegmentTimeline>
      </SegmentTemplate>
    </AdaptationSet>
  </Period>
</MPD>
"""

HAR = {"log": {"entries": [
    {"request": {"url": "https://contoso-my.sharepoint.com/personal/jane_contoso_com/_api/v2.0/drives/b!FAKEdrive/items/01FAKEITEM/videomanifest?provider=spo&part=index",
                 "headers": [{"name": "Cookie", "value": COOKIE}], "cookies": []},
     "response": {"content": {"mimeType": "application/dash+xml", "text": "<MPD>fake</MPD>"}}},
    {"request": {"url": "https://eu-mediap.svc.ms/transform/VideoProtectionKey?provider=Spo&kid=fake",
                 "headers": [{"name": "X-SPOPacToken", "value": PAC}], "cookies": []},
     "response": {"content": {"encoding": "base64", "text": base64.b64encode(KEY16).decode()}}},
    {"request": {"url": "https://contoso-my.sharepoint.com/personal/jane_contoso_com/_api/v2.1/drives/b!FAKEdrive/items/01FAKEITEM?$expand=media/transcripts",
                 "headers": [{"name": "authorization", "value": "Bearer eyJFAKE.tok.sig"},
                             {"name": "Cookie", "value": COOKIE}], "cookies": []},
     "response": {"content": {}}},
    {"request": {"url": "https://contoso-my.sharepoint.com/personal/jane_contoso_com/_layouts/15/stream.aspx?id=%2Fpersonal%2Fjane%2FDocuments%2FRecordings%2FExample%20Recording-20260603_133539-Recording.mp4&referrer=x",
                 "headers": [], "cookies": []},
     "response": {"content": {}}},
]}}

TRANSCODE_HAR = {"log": {"entries": [
    {"request": {"url": "https://contoso-my.sharepoint.com/personal/jane_contoso_com/_api/v2.1/drives/b!TRANSCODE/items/01TRANSCODE/videomanifest?provider=spo&part=index",
                 "headers": [{"name": "Cookie", "value": COOKIE},
                             {"name": "X-SPOPacToken", "value": PAC}], "cookies": []},
     "response": {"content": {"mimeType": "application/dash+xml", "text": TRANSCODE_MPD}}},
    {"request": {"url": "https://contoso-my.sharepoint.com/personal/jane_contoso_com/_api/v2.1/drives/b!TRANSCODE/items/01TRANSCODE/oneDrive.transcode?part=0",
                 "headers": [{"name": "Cookie", "value": COOKIE},
                             {"name": "X-Authorization", "value": "Bearer eyJFAKE.media.sig"}], "cookies": []},
     "response": {"content": {}}},
]}}


def _run(tmp_path, har_obj=HAR):
    har = tmp_path / "rec.har"
    har.write_text(json.dumps(har_obj), encoding="utf-8")
    argv = sys.argv
    sys.argv = ["har_extract", str(har), "--out-dir", str(tmp_path)]
    try:
        return har_extract.main()
    finally:
        sys.argv = argv


def test_extracts_all_inputs(tmp_path):
    rc = _run(tmp_path)
    assert rc == 0
    assert (tmp_path / "out.mpd").read_text().startswith("<MPD")
    assert (tmp_path / "key.bin").read_bytes() == KEY16
    assert len((tmp_path / "key.bin").read_bytes()) == 16


def test_extracts_transcode_inputs_without_visible_key_request(tmp_path, monkeypatch):
    def fake_refetch(url, headers):
        assert "VideoProtectionKey" in url
        assert headers == {"X-SPOPacToken": PAC}
        return KEY16

    monkeypatch.setattr(har_extract, "_refetch_url", fake_refetch)
    rc = _run(tmp_path, TRANSCODE_HAR)

    assert rc == 0
    assert (tmp_path / "key.bin").read_bytes() == KEY16
    assert "X-SPOPacToken: FAKEPAC" in (tmp_path / "video.curl").read_text()
    tc = (tmp_path / "transcript.curl").read_text()
    assert "Bearer eyJFAKE.media.sig" in tc
    assert "b!TRANSCODE" in tc and "01TRANSCODE" in tc
    assert "media/transcripts" in tc
    assert "oneDrive.transcode" not in tc


def test_find_key_does_not_match_generic_transform_urls():
    entries = [{"request": {"url": "https://eu-mediap.svc.ms/transform/videomanifest?provider=Spo"}}]
    assert har_extract.find_key(entries) is None


def test_video_curl_has_cookie(tmp_path):
    _run(tmp_path)
    vc = (tmp_path / "video.curl").read_text()
    assert "FedAuth=" in vc and "rtFa=" in vc


def test_transcript_curl_has_bearer_and_ids(tmp_path):
    _run(tmp_path)
    tc = (tmp_path / "transcript.curl").read_text()
    assert "Bearer eyJFAKE.tok.sig" in tc
    assert "b!FAKEdrive" in tc and "01FAKEITEM" in tc


def test_suggested_basename(tmp_path):
    _run(tmp_path)
    assert (tmp_path / "basename.txt").read_text().strip() == "Example_Recording 2026-06-03"


def test_suggested_basename_without_teams_timestamp():
    entries = [{"request": {"url": "https://contoso.sharepoint.com/stream.aspx?id=%2FRecordings%2FPlain%20Meeting.mp4"}}]

    assert har_extract.suggest_basename(entries) == "Plain_Meeting"


def test_manifest_refetch_failure_is_redacted(tmp_path, monkeypatch, capsys):
    secret_url = (
        "https://swedencentral1-mediap.svc.ms/transform/videomanifest"
        "?P1=signed-secret&token=do-not-print"
    )
    har_obj = {"log": {"entries": [
        {"request": {"url": secret_url, "headers": [{"name": "Cookie", "value": COOKIE}], "cookies": []},
         "response": {"content": {"mimeType": "application/dash+xml"}}},
    ]}}

    def fail_refetch(entry):
        raise requests.RequestException(f"network failed for {secret_url}")

    monkeypatch.setattr(har_extract, "_refetch", fail_refetch)

    rc = _run(tmp_path, har_obj)
    output = capsys.readouterr().out

    assert rc == 1
    assert "manifest refetch failed: DNS/network/auth error for swedencentral1-mediap.svc.ms" in output
    assert "signed-secret" not in output
    assert "do-not-print" not in output


def test_incomplete_har_reports_missing(tmp_path):
    har = tmp_path / "empty.har"
    har.write_text(json.dumps({"log": {"entries": [HAR["log"]["entries"][3]]}}), encoding="utf-8")
    argv = sys.argv
    sys.argv = ["har_extract", str(har), "--out-dir", str(tmp_path)]
    try:
        rc = har_extract.main()
    finally:
        sys.argv = argv
    assert rc == 1  # missing manifest + key + auth
