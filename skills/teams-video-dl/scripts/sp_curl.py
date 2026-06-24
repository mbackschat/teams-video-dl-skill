#!/usr/bin/env python3
"""Shared helpers for the SharePoint/Stream downloaders.

Parses a DevTools "Copy as cURL" command (the bash flavour Chrome/Edge emit) into
its URL, headers, and cookies. Also a couple of URL helpers for pulling the
SharePoint drive/item ids and the host/site out of an API URL. No project- or
tenant-specific values live here; everything is derived from the input.
"""

import re
from urllib.parse import urlsplit


# ---------------- cURL parsing ----------------

_ESCAPES = {"\\\\": "\\", "\\'": "'", '\\"': '"', "\\n": "\n",
            "\\t": "\t", "\\r": "\r"}


def _ansi_c_decode(s):
    """Decode the $'...' (ANSI-C) escapes Chrome uses when a value contains
    bytes like '!' -> \\u0021. Only the handful that show up in URLs/headers."""
    def repl(m):
        g = m.group(0)
        if g[:2] == "\\u" or g[:2] == "\\x":
            return chr(int(g[2:], 16))
        return _ESCAPES.get(g, g)
    return re.sub(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\\[\\'\"ntr]", repl, s)


def _tokenize(text):
    """Split a shell command into (kind, raw) tokens, honouring '...', "...",
    $'...' and line-continuations. kind is used to pick the right unescaping."""
    text = re.sub(r"\\\r?\n", " ", text)  # join line continuations
    toks, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] in "'\"":  # $'...' / $"..."
            q, j, buf = text[i + 1], i + 2, []
            while j < n and text[j] != q:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2]); j += 2
                else:
                    buf.append(text[j]); j += 1
            toks.append(("ansi", "".join(buf))); i = j + 1
            continue
        if c in "'\"":
            q, j, buf = c, i + 1, []
            while j < n and text[j] != q:
                if q == '"' and text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2]); j += 2
                else:
                    buf.append(text[j]); j += 1
            toks.append(("dq" if q == '"' else "sq", "".join(buf))); i = j + 1
            continue
        j, buf = i, []  # unquoted run
        while j < n and not text[j].isspace():
            if text[j] == "\\" and j + 1 < n:
                buf.append(text[j + 1]); j += 2
            else:
                buf.append(text[j]); j += 1
        toks.append(("raw", "".join(buf))); i = j
    return toks


def _decode(kind, raw):
    if kind == "ansi":
        return _ansi_c_decode(raw)
    if kind == "dq":
        return raw.replace('\\"', '"').replace("\\\\", "\\")
    return raw  # 'sq' (bash single-quote: literal) and 'raw'


def parse_curl(text):
    """Return {url, headers(dict), cookie(str), bearer(str|None)} from a cURL
    command. The request URL is the first non-flag argument; cookies come from
    -b/--cookie or a Cookie header; bearer from the Authorization header."""
    toks = [_decode(k, v) for k, v in _tokenize(text)]
    if toks and toks[0] == "curl":
        toks = toks[1:]
    url, headers, cookie = None, {}, None
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-H", "--header") and i + 1 < len(toks):
            name, _, val = toks[i + 1].partition(":")
            headers[name.strip()] = val.strip()
            i += 2
            continue
        if t in ("-b", "--cookie") and i + 1 < len(toks):
            cookie = toks[i + 1]
            i += 2
            continue
        if url is None and not t.startswith("-"):
            url = t
        i += 1
    if cookie is None:
        cookie = next((v for k, v in headers.items() if k.lower() == "cookie"), "")
    bearer = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    return {"url": url, "headers": headers, "cookie": cookie or "", "bearer": bearer}


def normalize_bearer(bearer):
    """Return a value suitable for an Authorization header (adds 'Bearer ')."""
    if not bearer:
        return None
    return bearer if bearer.lower().startswith("bearer") else "Bearer " + bearer


# ---------------- URL helpers ----------------

_DRIVE_ITEM = re.compile(r"/drives/([^/]+)/items/([^/?]+)", re.IGNORECASE)


def drive_item_from_url(url):
    """Pull (driveId, itemId) out of a .../drives/{id}/items/{id} URL, or (None, None)."""
    m = _DRIVE_ITEM.search(url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def host_site_from_url(url):
    """Return (origin, site) where origin is scheme://host and site is the
    leading '/personal/<x>' or '/sites/<x>' path segment ('' if neither)."""
    p = urlsplit(url or "")
    origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
    m = re.match(r"(/(?:personal|sites)/[^/]+)", p.path or "")
    return origin, (m.group(1) if m else "")
