#!/usr/bin/env python3
"""sp_transcript_dl.py - download & convert a SharePoint/Stream meeting transcript.

The transcript is NOT encrypted, but the SharePoint "vroom" API that serves it needs
BOTH a short-lived SPO access token (Authorization: Bearer, ~1h) AND the SPO session
cookies (FedAuth/rtFa). Cookies alone -> 401 unauthenticated.

The crucial detail: append `?format=json` to the /content endpoint. That returns the
Stream "Transcript" JSON (entries[] with speakerDisplayName + startOffset/endOffset) —
i.e. WITH speaker attribution. WITHOUT it the endpoint serves speaker-stripped WebVTT.

No host or drive/item ids are baked in; they're read from the captured request URL.

Flows:
  --from-curl FILE   read URL + Bearer + cookies from a DevTools "Copy as cURL"
                     (or the transcript.curl that har_extract.py writes). Recommended.
  --bearer/--cookie  pass the token + cookies directly, plus --host/--drive/--item.
  --from-json FILE   convert an already-downloaded transcript JSON/VTT, no network.

USAGE
  uv run --with requests sp_transcript_dl.py --from-curl transcript.curl -o My_Meeting
  uv run --with requests sp_transcript_dl.py --from-json My_Meeting.raw.json -o My_Meeting
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sp_curl  # noqa: E402

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Accept": "*/*",
}
SESS = requests.Session()


# ---------------- network ----------------

def _get(url, accept="*/*"):
    h = dict(HEADERS)
    h["Accept"] = accept
    r = SESS.get(url, headers=h, timeout=60, allow_redirects=True)  # follow 302
    if r.status_code in (401, 403):
        raise SystemExit(
            f"\n{r.status_code} on {url}\n"
            "Auth failed. The Bearer token is ~1h and is almost certainly the problem.\n"
            "Re-capture a fresh request (Copy as cURL / re-export the HAR) and rerun immediately.\n")
    r.raise_for_status()
    return r


def discover(origin, site, drive, item):
    url = (f"{origin}{site}/_api/v2.1/drives/{drive}/items/{item}"
           "?select=media/transcripts,audioTracks&$expand=media/transcripts,media/audioTracks")
    data = _get(url, accept="application/json").json()
    return (data.get("media") or {}).get("transcripts", []) or []


def content_url(origin, site, drive, item, transcript_id):
    # ?format=json -> Stream Transcript JSON with speakerDisplayName. Without it the
    # endpoint serves speaker-stripped WebVTT, so always ask for json.
    return (f"{origin}{site}/_api/v2.1/drives/{drive}/items/{item}"
            f"/versions/current/media/transcripts/{transcript_id}/content?format=json")


# ---------------- parsing ----------------

def _norm_ts(ts):
    """Normalize a timestamp to SRT form HH:MM:SS,mmm.
    Accepts '00:01:23.4567890' (Teams), '00:01:23.456' (VTT), '83.5' (seconds)."""
    ts = str(ts).strip()
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})[.,](\d+)$", ts)
    if m:
        h, mm, ss, frac = m.groups()
        return f"{int(h):02d}:{int(mm):02d}:{int(ss):02d},{(frac + '000')[:3]}"
    m = re.match(r"^(\d{1,2}):(\d{2})[.,](\d+)$", ts)  # MM:SS.fff
    if m:
        mm, ss, frac = m.groups()
        return f"00:{int(mm):02d}:{int(ss):02d},{(frac + '000')[:3]}"
    try:  # bare seconds
        total = float(ts)
        h = int(total // 3600); mm = int((total % 3600) // 60)
        ss = int(total % 60); ms = int(round((total - int(total)) * 1000))
        return f"{h:02d}:{mm:02d}:{ss:02d},{ms:03d}"
    except ValueError:
        return "00:00:00,000"


def parse_teams_json(text):
    """Return [(start, end, speaker, text), ...] from a Stream Transcript JSON.
    Top-level 'entries' array; each entry has text, speakerDisplayName,
    startOffset/endOffset. Falls back to a few known alternate key names."""
    obj = json.loads(text)
    entries = obj.get("entries") if isinstance(obj, dict) else None
    if entries is None and isinstance(obj, dict):
        entries = obj.get("recognizedPhrases") or obj.get("captions") or obj.get("results")
    if entries is None and isinstance(obj, list):
        entries = obj
    if not entries:
        raise SystemExit("No 'entries' found in JSON — inspect the file; schema differs "
                         "from the expected Stream Transcript shape.")
    cues = []
    for e in entries:
        txt = (e.get("text") or e.get("displayText") or e.get("caption") or "").strip()
        if not txt:
            continue
        spk = (e.get("speakerDisplayName") or e.get("speaker") or e.get("speakerId") or "").strip()
        start = e.get("startOffset") or e.get("start") or e.get("offset") or e.get("startTime") or "0"
        end = e.get("endOffset") or e.get("end") or e.get("endTime") or start
        cues.append((_norm_ts(start), _norm_ts(end), spk, txt))
    return cues


def parse_vtt(text):
    cues = []
    for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [l for l in b.split("\n") if l.strip()]
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        a, _, b2 = lines[ti].partition("-->")
        start, end = a.strip(), b2.strip().split(" ")[0]
        body = " ".join(lines[ti + 1:]).strip()
        spk = ""
        mv = re.match(r"<v\s+([^>]+)>(.*)", body)
        if mv:
            spk, body = mv.group(1).strip(), mv.group(2)
        body = re.sub(r"</?v[^>]*>", "", body).strip()
        if body:
            cues.append((_norm_ts(start), _norm_ts(end), spk, body))
    return cues


def cues_from_content(text):
    text = text.lstrip("﻿")  # drop a UTF-8 BOM if present
    if text.lstrip().startswith("WEBVTT"):
        return parse_vtt(text)
    return parse_teams_json(text)


# ---------------- output ----------------

def write_outputs(cues, out_base):
    srt = []
    for i, (s, e, spk, txt) in enumerate(cues, 1):
        body = f"{spk}: {txt}" if spk else txt
        srt.append(f"{i}\n{s} --> {e}\n{body}\n")
    Path(f"{out_base}.srt").write_text("\n".join(srt), encoding="utf-8")

    vtt = ["WEBVTT", ""]
    for s, e, spk, txt in cues:
        body = f"<v {spk}>{txt}</v>" if spk else txt
        vtt.append(f"{s.replace(',', '.')} --> {e.replace(',', '.')}\n{body}\n")
    Path(f"{out_base}.vtt").write_text("\n".join(vtt), encoding="utf-8")

    txt_lines, last = [], None
    for s, e, spk, txt in cues:
        if spk and spk != last:
            txt_lines.append(f"\n[{s[:8]}] {spk}:")
            last = spk
        txt_lines.append(txt if spk else f"[{s[:8]}] {txt}")
    Path(f"{out_base}.txt").write_text("\n".join(txt_lines).strip() + "\n", encoding="utf-8")
    print(f"wrote {out_base}.srt / .vtt / .txt  ({len(cues)} cues)")


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-curl", dest="from_curl",
                    help="file with a 'Copy as cURL' of any _api request (or har_extract's transcript.curl)")
    ap.add_argument("--from-json", dest="from_json",
                    help="convert an already-downloaded transcript JSON/VTT (no network)")
    ap.add_argument("--bearer", help="SPO access token (Authorization: Bearer <...>), ~1h TTL")
    ap.add_argument("--cookie", help='SPO Cookie header: "FedAuth=...; rtFa=..."')
    ap.add_argument("--host", help="origin, e.g. https://<tenant>-my.sharepoint.com (with --bearer)")
    ap.add_argument("--site", default="", help="site path, e.g. /personal/<user> (with --bearer)")
    ap.add_argument("--drive", help="driveId (with --bearer)")
    ap.add_argument("--item", help="itemId (with --bearer)")
    ap.add_argument("-o", "--out", default="transcript", help="output basename (match the video's)")
    args = ap.parse_args()

    # Offline conversion path
    if args.from_json:
        text = Path(args.from_json).read_text(encoding="utf-8-sig", errors="replace")
        write_outputs(cues_from_content(text), args.out)
        return 0

    # Resolve auth + endpoint location
    origin = args.host or ""
    site = args.site or ""
    drive, item = args.drive, args.item
    bearer, cookie = args.bearer, args.cookie

    if args.from_curl:
        c = sp_curl.parse_curl(Path(args.from_curl).read_text(encoding="utf-8", errors="replace"))
        bearer = bearer or c["bearer"]
        cookie = cookie or c["cookie"]
        o, s = sp_curl.host_site_from_url(c["url"])
        origin = origin or o
        site = site or s
        d, it = sp_curl.drive_item_from_url(c["url"])
        drive = drive or d
        item = item or it
        print("Loaded auth + ids from curl (token not shown).")

    if not all([bearer, cookie, origin, drive, item]):
        raise SystemExit(
            "Need Bearer + cookie + host + drive + item. Easiest: --from-curl <file>. "
            f"(have bearer={bool(bearer)}, cookie={bool(cookie)}, host={bool(origin)}, "
            f"drive={bool(drive)}, item={bool(item)})")

    HEADERS["Authorization"] = sp_curl.normalize_bearer(bearer)
    HEADERS["Cookie"] = cookie

    transcripts = discover(origin, site, drive, item)
    if not transcripts:
        raise SystemExit("No transcripts returned by discovery — none attached, or schema changed.")
    print(f"Found {len(transcripts)} transcript(s):")
    for t in transcripts:
        print("  id=", t.get("id"), "lang=", t.get("languageTag"),
              "auto=", t.get("isAutoGenerated"), "src=", t.get("source"))

    multi = len(transcripts) > 1
    for t in transcripts:
        tid, lang = t.get("id"), (t.get("languageTag") or "x")
        # SharePoint omits a charset, so decode bytes as utf-8-sig (else umlauts mojibake).
        raw = _get(content_url(origin, site, drive, item, tid)).content.decode("utf-8-sig")
        suffix = f".{lang}" if multi else ""
        Path(f"{args.out}{suffix}.raw.json").write_text(raw, encoding="utf-8")
        write_outputs(cues_from_content(raw), f"{args.out}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
