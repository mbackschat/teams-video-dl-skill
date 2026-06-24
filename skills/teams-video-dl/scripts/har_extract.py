#!/usr/bin/env python3
"""har_extract.py - pull everything the downloaders need out of one HAR file.

A Microsoft Stream / SharePoint recording is gated by several short-lived secrets
captured in different requests (the DASH manifest, the AES key from the key host,
the SPO session cookies, and the transcript's Bearer token). Rather than asking a
user to hunt down four separate requests, we let them export ONE HAR of the player
loading, and pull each piece out by request-URL pattern.

Produces (in --out-dir, default cwd):
  out.mpd        - the DASH manifest (for sp_stream_dl.py)
  key.bin        - the 16-byte AES key (for sp_stream_dl.py --key)
  video.curl     - synthetic cURL carrying the segment-host cookie (sp_stream_dl --from-curl)
  transcript.curl- synthetic cURL: discovery URL + Bearer + cookie (sp_transcript_dl --from-curl)
  basename.txt   - a suggested output base name derived from the recording filename

Response bodies are read straight from the HAR when present ("Export HAR (with
content)"); if a body is missing we re-fetch the request with its captured headers.

USAGE
  uv run --with requests har_extract.py recording.har --out-dir .
"""

import argparse
import base64
import html
import json
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sp_curl  # noqa: E402


class RefetchError(Exception):
    """A network refetch failed; message is safe to print."""


def _req_headers(entry):
    """Request headers as a dict, dropping HTTP/2 pseudo-headers (:authority …)."""
    out = {}
    for h in entry.get("request", {}).get("headers", []):
        name = h.get("name", "")
        if name and not name.startswith(":"):
            out[name] = h.get("value", "")
    return out


def _header(entry, name):
    return next((v for k, v in _req_headers(entry).items() if k.lower() == name.lower()), "")


def _auth_header(entry):
    for name in ("authorization", "x-authorization"):
        value = _header(entry, name)
        if value.lower().startswith("bearer"):
            return value
    return ""


def _cookie(entry):
    ck = _header(entry, "cookie")
    if ck:
        return ck
    parts = [f"{c.get('name')}={c.get('value')}" for c in entry.get("request", {}).get("cookies", [])]
    return "; ".join(parts)


def _body_bytes(entry):
    c = entry.get("response", {}).get("content", {})
    text = c.get("text")
    if text is None or text == "":
        return None
    if c.get("encoding") == "base64":
        try:
            return base64.b64decode(text)
        except Exception:
            return None
    return text.encode("utf-8", "replace")


def _refetch(entry):
    url = entry["request"]["url"]
    r = requests.get(url, headers=_req_headers(entry), timeout=60, allow_redirects=True)
    r.raise_for_status()
    return r.content


def _refetch_url(url, headers):
    r = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
    r.raise_for_status()
    return r.content


def _redacted_refetch_message(label, url):
    host = urlsplit(url or "").netloc or "unknown host"
    return f"{label} refetch failed: DNS/network/auth error for {host}"


def _get_body(entry, label):
    """Body from the HAR if captured, else re-fetched with the request's headers."""
    b = _body_bytes(entry)
    if b:
        return b
    print(f"  ({label}: body not in HAR — re-fetching with captured headers)")
    try:
        return _refetch(entry)
    except requests.RequestException as e:
        raise RefetchError(_redacted_refetch_message(label, _url(entry))) from e


def _entries(har):
    return har.get("log", {}).get("entries", [])


def _find(entries, pred):
    return [e for e in entries if pred(e)]


def _url(e):
    return e.get("request", {}).get("url", "")


def _curl(url, headers=(), cookie=""):
    parts = ["curl", shlex.quote(url)]
    for name, value in headers:
        if value:
            parts += ["-H", shlex.quote(f"{name}: {value}")]
    if cookie:
        parts += ["-b", shlex.quote(cookie)]
    return " ".join(parts) + "\n"


# ---------------- locators (all pattern-based, no tenant specifics) ----------------

def find_manifest(entries):
    hits = _find(entries, lambda e: "videomanifest" in _url(e).lower())
    if not hits:
        hits = _find(entries, lambda e: "dash+xml" in
                     e.get("response", {}).get("content", {}).get("mimeType", "").lower())
    return hits[0] if hits else None


def find_key(entries):
    return next((e for e in entries
                 if "videoprotectionkey" in _url(e).lower()), None)


def find_segment_context(entries, manifest):
    """Prefer a real transcode segment request for media cookies; fall back to manifest."""
    hits = _find(entries, lambda e: "onedrive.transcode" in _url(e).lower() and _cookie(e))
    return hits[0] if hits else manifest


def find_auth(entries):
    """An _api/v2.x drives/items transcript/audio metadata request with a Bearer token."""
    def is_api(e):
        u = _url(e)
        lu = u.lower()
        return bool(re.search(r"/_api/v[\d.]+/drives/[^/]+/items/", u)) \
            and ("transcript" in lu or "audiotrack" in lu) \
            and bool(_auth_header(e))
    api = _find(entries, is_api)
    api.sort(key=lambda e: ("transcripts" not in _url(e).lower(), len(_url(e))))
    return api[0] if api else None


def find_media_auth(entries):
    """A SharePoint media request with X-Authorization can stand in for transcript auth."""
    hits = _find(entries, lambda e: "sharepoint.com" in urlsplit(_url(e)).netloc.lower()
                 and _auth_header(e) and _cookie(e))
    hits.sort(key=lambda e: ("onedrive.transcode" not in _url(e).lower(), len(_url(e))))
    return hits[0] if hits else None


def mpd_context(mpd):
    root = ET.fromstring(mpd.decode("utf-8-sig", errors="replace"))
    base = next((elem.text.strip() for elem in root.iter()
                 if elem.tag.endswith("BaseURL") and elem.text and elem.text.strip()), "")
    key_tpl = next((elem.get("keyUriTemplate") for elem in root.iter()
                    if elem.tag.endswith("CryptoPeriod") and elem.get("keyUriTemplate")), "")
    base = html.unescape(base)
    key_tpl = html.unescape(key_tpl)
    drive = item = None
    m = re.search(r"/drives/([^/]+)/items/([^/?]+)", base)
    if m:
        drive, item = m.group(1), m.group(2)
    origin, site = sp_curl.host_site_from_url(base)
    return {
        "base_url": base,
        "key_url": urljoin(base, key_tpl) if base and key_tpl else "",
        "origin": origin,
        "site": site,
        "drive": drive,
        "item": item,
    }


def suggest_basename(entries):
    """Derive a clean output base from the recording's filename if we can find it."""
    name = None
    for e in entries:
        m = re.search(r"[?&]id=([^&]+)", _url(e))
        if m and ".mp4" in unquote(m.group(1)).lower():
            name = unquote(m.group(1)).split("/")[-1]
            break
    if not name:
        for e in entries:
            body = e.get("response", {}).get("content", {}).get("text") or ""
            m = re.search(r'"name"\s*:\s*"([^"]+\.mp4)"', body)
            if m:
                name = m.group(1)
                break
    if not name:
        return None
    stem = re.sub(r"\.mp4$", "", name, flags=re.IGNORECASE)
    date = None
    m = re.search(r"-(\d{4})(\d{2})(\d{2})_\d{6}(?:-|$)", stem)
    if m:
        stem = stem[:m.start()]
        date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    base = re.sub(r"[^0-9A-Za-z]+", "_", stem).strip("_")
    if base and date:
        return f"{base} {date}"
    return base or None


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("har", help="HAR file exported from the browser (DevTools → Network → Export HAR)")
    ap.add_argument("--out-dir", default=".", help="where to write out.mpd/key.bin/*.curl (default: cwd)")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    har = json.loads(Path(args.har).read_text(encoding="utf-8", errors="replace"))
    entries = _entries(har)
    if not entries:
        raise SystemExit("No entries in HAR — is this a valid export?")
    print(f"HAR has {len(entries)} requests.")

    missing = []

    man = find_manifest(entries)
    manifest_body = None
    key = find_key(entries)
    if man:
        try:
            manifest_body = _get_body(man, "manifest")
        except RefetchError as e:
            print(f"  ({e})")
            missing.append(f"DASH manifest body ({e})")
        if manifest_body:
            (out / "out.mpd").write_bytes(manifest_body)
            segment = find_segment_context(entries, man)
            cookie = _cookie(segment)
            spopac = _header(man, "X-SPOPacToken") or (_header(key, "X-SPOPacToken") if key else "")
            headers = [("X-SPOPacToken", spopac)] if spopac else []
            (out / "video.curl").write_text(_curl(_url(segment), headers=headers, cookie=cookie), encoding="utf-8")
            print(f"✓ out.mpd + video.curl (cookie {'found' if cookie else 'MISSING'}, PAC {'found' if spopac else 'not captured'})")
            if not cookie:
                missing.append("segment cookie (no Cookie header on the manifest/transcode request; HAR may be sanitized or sensitive data may be disabled)")
    else:
        missing.append("DASH manifest (no 'videomanifest' request — did the video start playing?)")

    kb = None
    key_issue_reported = False
    if key:
        try:
            kb = _get_body(key, "key")
        except RefetchError as e:
            print(f"  ({e})")
            missing.append(f"AES key ({e})")
            key_issue_reported = True
    elif manifest_body and man:
        ctx = mpd_context(manifest_body)
        spopac = _header(man, "X-SPOPacToken")
        if ctx["key_url"] and spopac:
            print("  (key: no key request in HAR — re-fetching key URL from manifest with captured PAC header)")
            try:
                kb = _refetch_url(ctx["key_url"], {"X-SPOPacToken": spopac})
            except requests.RequestException:
                msg = _redacted_refetch_message("key", ctx["key_url"])
                print(f"  ({msg})")
                missing.append(f"AES key ({msg})")
                key_issue_reported = True

    if kb is not None:
        if len(kb) == 16:
            (out / "key.bin").write_bytes(kb)
            print("✓ key.bin (16 bytes)")
        else:
            missing.append(f"AES key was {len(kb)} bytes, expected 16 (key request found but body wrong)")
    elif not key_issue_reported:
        missing.append("AES key (no VideoProtectionKey request — did the video start playing?)")

    auth = find_auth(entries)
    if auth:
        bearer = _auth_header(auth)
        cookie = _cookie(auth)
        transcript_url = _url(auth)
    elif manifest_body:
        auth = find_media_auth(entries)
        ctx = mpd_context(manifest_body)
        if auth and ctx["origin"] and ctx["drive"] and ctx["item"]:
            bearer = _auth_header(auth)
            cookie = _cookie(auth)
            transcript_url = (
                f"{ctx['origin']}{ctx['site']}/_api/v2.1/drives/{ctx['drive']}/items/{ctx['item']}"
                "?select=media/transcripts,audioTracks&$expand=media/transcripts,media/audioTracks"
            )
        else:
            transcript_url = ""
    else:
        transcript_url = ""

    if transcript_url:
        (out / "transcript.curl").write_text(
            _curl(transcript_url, headers=[("authorization", bearer)], cookie=cookie), encoding="utf-8")
        drive, item = sp_curl.drive_item_from_url(transcript_url)
        print(f"✓ transcript.curl (drive={drive}, item={item})")
    else:
        missing.append("transcript auth (no _api request with a Bearer token — open the transcript panel and make sure HAR sensitive data is enabled)")

    base = suggest_basename(entries)
    if base:
        (out / "basename.txt").write_text(base + "\n", encoding="utf-8")
        print(f"✓ suggested output base name: {base}")

    print()
    if missing:
        print("INCOMPLETE — missing:")
        for m in missing:
            print("  -", m)
        print("\nFix: in the browser, reload the stream page, PLAY a few seconds, open the")
        print("transcript panel, THEN export the HAR (DevTools → Network → ⤓ → Export HAR…),")
        print("and re-run. If a body is missing or a refetch failed, enable HAR sensitive")
        print("data in DevTools and retry promptly; tokens and signed URLs are short-lived.")
        return 1
    print("All inputs extracted. Hand off to sp_stream_dl.py and sp_transcript_dl.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
