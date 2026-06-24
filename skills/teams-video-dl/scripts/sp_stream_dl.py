#!/usr/bin/env python3
"""sp_stream_dl.py - download a SharePoint/Stream SEA-encrypted DASH recording.

Two auth realms:
  * KEY host  (<region>-mediap.svc.ms)      -> needs X-SPOPacToken. We sidestep it by
                                               capturing the 16-byte key once (--key key.bin).
  * SEGMENT host (<tenant>-my.sharepoint.com) -> needs your SPO session cookies (--cookie /
                                               --from-curl video.curl).

The media is SEA aes128-cbc and **PKCS#7-padded**; every decrypted segment (even
block-aligned ones, which get a full 0x10 pad block) must have its padding stripped.

Segments are cached per track and re-runs skip ones already present, so a mid-run
401/network drop resumes by re-running the same command (optionally a fresh cookie).
Decrypt+concat+mux happen once all segments for a track are on disk.

No host is baked in: Origin/Referer are derived from the manifest's BaseURL.

USAGE
  uv run --with cryptography --with requests sp_stream_dl.py out.mpd --key key.bin --probe
  uv run --with cryptography --with requests sp_stream_dl.py out.mpd --key key.bin \
        --from-curl video.curl --full -o recording.mp4 --work-dir tmp/stream
"""

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sp_curl  # noqa: E402

try:
    import requests
    _SESS = requests.Session()
    def http_get(url):
        r = _SESS.get(url, headers=headers_for_url(url), timeout=60)
        r.raise_for_status()
        return r.content
except ImportError:
    import urllib.request
    def http_get(url):
        req = urllib.request.Request(url, headers=headers_for_url(url))
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

NS = {"mpd": "urn:mpeg:DASH:schema:MPD:2011", "sea": "urn:mpeg:dash:schema:sea:2012"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
}

SEGMENT_AUTH_HEADERS = {"authorization", "x-authorization", "x-spopactoken"}


def headers_for_url(url):
    """Use PAC/Bearer auth only for the key endpoint; segment URLs use cookies."""
    out = dict(HEADERS)
    if "videoprotectionkey" not in url.lower():
        for name in list(out):
            if name.lower() in SEGMENT_AUTH_HEADERS:
                out.pop(name, None)
    return out


MP4_BOX_TYPES = (b"ftyp", b"styp", b"moof", b"moov", b"sidx", b"free", b"mdat", b"skip")


def looks_like_mp4(b: bytes) -> bool:
    return len(b) >= 8 and b[4:8] in MP4_BOX_TYPES


def strip_pkcs7(data: bytes) -> bytes:
    """Remove PKCS#7 padding. SEA aes128-cbc here is always PKCS#7-padded: the last
    byte n (1..16) is the pad length and the last n bytes all equal n. Even block-
    aligned plaintext gets a full 16-byte 0x10 pad block, so it's always present."""
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    n = (len(data) // 16) * 16
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(data[:n]) + dec.finalize()
    if n == len(data):           # fully block-aligned ciphertext -> PKCS#7 padded
        plain = strip_pkcs7(plain)
    return plain + data[n:]


def parse_iv(text: str) -> bytes:
    text = text.strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    iv = bytes.fromhex(text)
    if len(iv) != 16:
        raise ValueError(f"IV is {len(iv)} bytes, expected 16")
    return iv


def segment_times(seg_template):
    tl = seg_template.find("mpd:SegmentTimeline", NS)
    first = tl.find("mpd:S", NS)
    t = int(first.get("t")) if first is not None and first.get("t") else 0
    for s in tl.findall("mpd:S", NS):
        d = int(s.get("d"))
        for _ in range(int(s.get("r", "0")) + 1):
            yield t
            t += d


def adaptation_jobs(mpd_path: Path):
    root = ET.parse(mpd_path).getroot()
    base = root.findtext("mpd:BaseURL", namespaces=NS).strip()
    period = root.find("mpd:Period", NS)
    jobs = []
    for aset in period.findall("mpd:AdaptationSet", NS):
        rep = aset.find("mpd:Representation", NS)
        rep_id = rep.get("id")
        st = aset.find("mpd:SegmentTemplate", NS)
        crypto = aset.find("mpd:ContentProtection", NS).find("sea:CryptoPeriod", NS)
        init_tpl = st.get("initialization").replace("$RepresentationID$", rep_id)
        media_tpl = st.get("media").replace("$RepresentationID$", rep_id)
        jobs.append({
            "type": aset.get("contentType"),
            "key_url": urljoin(base, crypto.get("keyUriTemplate")),
            "iv": parse_iv(crypto.get("IV")),
            "init_url": urljoin(base, init_tpl),
            "media_urls": [urljoin(base, media_tpl.replace("$Time$", str(t)))
                           for t in segment_times(st)],
        })
    return base, jobs


def set_origin_from(url):
    """Set Origin/Referer to the segment host (some SPO endpoints check them)."""
    p = urlsplit(url)
    if p.scheme and p.netloc:
        HEADERS.setdefault("Origin", f"{p.scheme}://{p.netloc}")
        HEADERS.setdefault("Referer", f"{p.scheme}://{p.netloc}/")


def get_key(j, key_override):
    if key_override is not None:
        return key_override
    k = http_get(j["key_url"])
    if len(k) != 16:
        raise SystemExit("key endpoint did not return 16 bytes; capture the key and use --key key.bin")
    return k


def fetch_decrypt(url, key, iv):
    raw = http_get(url)
    if looks_like_mp4(raw):
        return raw, "cleartext"
    dec = aes_cbc_decrypt(key, iv, raw)
    return dec, ("decrypted-ok" if looks_like_mp4(dec) else "decrypted-UNVERIFIED")


def cmd_probe(jobs, key_override):
    print("== PROBE ==\n")
    ok = True
    for j in jobs:
        key = get_key(j, key_override)
        init, s1 = fetch_decrypt(j["init_url"], key, j["iv"])
        seg, s2 = fetch_decrypt(j["media_urls"][0], key, j["iv"])
        print(f"[{j['type']}] key={len(key)}B  init {len(init)}B->{s1} box={init[4:8]!r}  "
              f"seg0 {len(seg)}B->{s2} box={seg[4:8]!r}  segs={len(j['media_urls'])}")
        ok = ok and "UNVERIFIED" not in s2
    print("\nPROBE:", "OK, run --full" if ok else "framing mismatch -> paste this output back")


def download_track(j, key, cache: Path, retries=4):
    cache.mkdir(parents=True, exist_ok=True)
    files = []
    init_f = cache / "init.m4s"
    if not init_f.exists():
        data, _ = fetch_decrypt(j["init_url"], key, j["iv"])
        init_f.write_bytes(data)
    files.append(init_f)

    n = len(j["media_urls"])
    for i, url in enumerate(j["media_urls"]):
        out = cache / f"{i:05d}.m4s"
        if out.exists() and out.stat().st_size > 0:
            files.append(out)
            continue
        for attempt in range(retries):
            try:
                data, st = fetch_decrypt(url, key, j["iv"])
                if "UNVERIFIED" in st:
                    raise RuntimeError(f"segment {i} failed to decrypt cleanly")
                out.write_bytes(data)
                files.append(out)
                break
            except Exception as e:
                if attempt == retries - 1:
                    raise SystemExit(
                        f"\n[{j['type']}] segment {i}/{n} failed: {e}\n"
                        f"Re-run the SAME command to resume (downloaded segments are skipped).\n"
                        f"If it's a 401, capture a fresh cookie and pass --from-curl/--cookie."
                    )
                time.sleep(1.5 * (attempt + 1))
        if (i + 1) % 25 == 0 or i + 1 == n:
            print(f"\r[{j['type']}] {i+1}/{n}", end="", flush=True)
    print()
    return files


def _top_level_consumed(data: bytes) -> int:
    import struct as _s
    i = 0
    while i + 8 <= len(data):
        size = _s.unpack(">I", data[i:i+4])[0]
        if size == 1 and i + 16 <= len(data):
            size = _s.unpack(">Q", data[i+8:i+16])[0]
        if size < 8 or i + size > len(data):
            break
        i += size
    return i


def heal_legacy_padding(data: bytes) -> bytes:
    """Idempotently strip trailing PKCS#7 padding left by an older buggy cache.
    Clean fragments end exactly on a box boundary and pass through untouched."""
    consumed = _top_level_consumed(data)
    if consumed == len(data):
        return data
    trailing = data[consumed:]
    n = trailing[-1]
    if 1 <= n <= 16 and len(trailing) == n and trailing == bytes([n]) * n:
        return data[:consumed]
    return data


def cmd_full(jobs, key_override, out_path, work_dir=None):
    import subprocess
    work = Path(work_dir) if work_dir else Path(".")
    work.mkdir(parents=True, exist_ok=True)
    tracks = {}
    for j in jobs:
        key = get_key(j, key_override)
        cache = work / f".cache_{j['type']}"
        files = download_track(j, key, cache)
        merged = work / f"{j['type']}.mp4"
        with open(merged, "wb") as out:
            for f in files:
                out.write(heal_legacy_padding(f.read_bytes()))
        tracks[j["type"]] = merged

    cmd = ["ffmpeg", "-y"]
    for t in ("video", "audio"):
        if t in tracks:
            cmd += ["-i", str(tracks[t])]
    cmd += ["-c", "copy", "-movflags", "+faststart", out_path]
    print("muxing:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print("done ->", out_path)
    print(f"(segment caches kept in {work}; delete it to reclaim space)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mpd")
    ap.add_argument("--key", help="path to the 16-byte key file captured from the browser")
    ap.add_argument("--cookie", help='Cookie header for segment fetches, e.g. "FedAuth=...; rtFa=..."')
    ap.add_argument("--from-curl", dest="from_curl",
                    help="file with a 'Copy as cURL' (or har_extract's video.curl) to read the cookie from")
    ap.add_argument("--header", action="append", default=[], help='extra "Name: value" header (repeatable)')
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--work-dir",
                    help="directory for segment caches and intermediate track files (default: current directory)")
    ap.add_argument("-o", "--out", default="recording.mp4")
    args = ap.parse_args()

    if args.from_curl:
        c = sp_curl.parse_curl(Path(args.from_curl).read_text(encoding="utf-8", errors="replace"))
        if c["cookie"]:
            HEADERS["Cookie"] = c["cookie"]
        for k, v in c["headers"].items():        # carry through e.g. X-SPOPacToken
            if k.lower() in ("x-spopactoken",):
                HEADERS[k] = v
    if args.cookie:
        HEADERS["Cookie"] = args.cookie
    for h in args.header:
        k, _, v = h.partition(":")
        HEADERS[k.strip()] = v.strip()

    key_override = None
    if args.key:
        key_override = Path(args.key).read_bytes()
        if len(key_override) != 16:
            raise SystemExit(f"--key file is {len(key_override)} bytes, expected exactly 16")

    base, jobs = adaptation_jobs(Path(args.mpd))
    set_origin_from(base)
    if args.full:
        cmd_full(jobs, key_override, args.out, args.work_dir)
    else:
        cmd_probe(jobs, key_override)


if __name__ == "__main__":
    sys.exit(main())
