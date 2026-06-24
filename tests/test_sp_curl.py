"""Tests for sp_curl — all data here is synthetic (fake tenant/tokens)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/teams-video-dl/scripts"))
import sp_curl  # noqa: E402


# A realistic-shaped (but fake) Chrome "Copy as cURL (bash)". The URL uses the
# $'...' form because of the '!' in the drive id, exactly as Chrome emits it.
SAMPLE = r"""curl $'https://contoso-my.sharepoint.com/personal/jane_contoso_com/_api/v2.1/drives/b!FAKEdriveID123/items/01FAKEITEM456?select=media%2Ftranscripts&$expand=media%2Ftranscripts' \
  -H 'accept: application/json' \
  -H 'authorization: Bearer eyJFAKEheader.eyJFAKEpayload.FAKEsig' \
  -b 'FedAuth=FAKEFEDAUTH==; rtFa=FAKERTFA/abc+def==' \
  -H 'referer: https://contoso-my.sharepoint.com/' """


def test_parse_curl_extracts_url_bearer_cookie():
    r = sp_curl.parse_curl(SAMPLE)
    assert r["url"].startswith("https://contoso-my.sharepoint.com/")
    assert "b!FAKEdriveID123" in r["url"]          # ! decoded to '!'
    assert r["bearer"] == "Bearer eyJFAKEheader.eyJFAKEpayload.FAKEsig"
    assert "FedAuth=" in r["cookie"] and "rtFa=" in r["cookie"]


def test_cookie_via_header_variant():
    txt = SAMPLE.replace("-b 'FedAuth=FAKEFEDAUTH==; rtFa=FAKERTFA/abc+def=='",
                         "-H 'cookie: FedAuth=AAA; rtFa=BBB'")
    r = sp_curl.parse_curl(txt)
    assert r["cookie"] == "FedAuth=AAA; rtFa=BBB"


def test_drive_item_from_url():
    r = sp_curl.parse_curl(SAMPLE)
    drive, item = sp_curl.drive_item_from_url(r["url"])
    assert drive == "b!FAKEdriveID123"
    assert item == "01FAKEITEM456"


def test_host_site_from_url():
    r = sp_curl.parse_curl(SAMPLE)
    origin, site = sp_curl.host_site_from_url(r["url"])
    assert origin == "https://contoso-my.sharepoint.com"
    assert site == "/personal/jane_contoso_com"


def test_normalize_bearer_adds_prefix():
    assert sp_curl.normalize_bearer("abc") == "Bearer abc"
    assert sp_curl.normalize_bearer("Bearer abc") == "Bearer abc"
    assert sp_curl.normalize_bearer(None) is None


def test_double_quoted_curl_variant():
    txt = 'curl "https://contoso-my.sharepoint.com/x" -H "authorization: Bearer TKN"'
    r = sp_curl.parse_curl(txt)
    assert r["url"] == "https://contoso-my.sharepoint.com/x"
    assert r["bearer"] == "Bearer TKN"
