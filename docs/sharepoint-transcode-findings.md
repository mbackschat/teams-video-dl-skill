# SharePoint transcode findings

These notes document a real Stream-on-SharePoint capture shape using only placeholders. Do not add tenant names, recording names, token values, cookie values, HAR excerpts, or transcript content to this file.

## Observed capture shape

Some browser sessions do not expose a separate `VideoProtectionKey` request in the exported HAR. The playable key URL still exists in the DASH MPD as the `sea:CryptoPeriod keyUriTemplate`, and the request that fetched the MPD can carry the required `X-SPOPacToken` header.

The segment traffic can use `oneDrive.transcode` URLs under the SharePoint `_api/v2.1/drives/{driveId}/items/{itemId}/...` path. Those segment requests need the SharePoint session cookies plus the signed URL token embedded in the MPD-derived URL.

Do not send PAC or Bearer-style auth headers to the segment URLs. In this shape, segment requests succeeded with cookies while the same requests could fail when `Authorization`, `X-Authorization`, or `X-SPOPacToken` were carried across from browser requests.

The transcript bearer can appear as `X-Authorization` on a SharePoint media/transcode request instead of a plain `Authorization` header on a transcript discovery request. It is still a Bearer value; `har_extract.py` rewrites it into a synthetic `authorization:` header in `transcript.curl`.

## Tool behavior

`har_extract.py` now treats the MPD as the source of truth when the HAR has no visible key request. It parses `BaseURL`, `keyUriTemplate`, `driveId`, and `itemId`, then re-fetches the key URL with only `X-SPOPacToken` if the direct key request is absent.

`video.curl` remains a temporary credential-bearing file. It should contain only the media cookie plus `X-SPOPacToken` when available; it should not preserve unrelated browser auth headers.

`sp_stream_dl.py` now chooses headers per URL. `VideoProtectionKey` requests keep the captured auth headers, while SharePoint segment requests strip `Authorization`, `X-Authorization`, and `X-SPOPacToken` and rely on the cookie and signed segment URL.

`transcript.curl` can be generated from either a direct `_api/v2.x/drives/.../items/...` Bearer request or, as a fallback, from a SharePoint media request with `X-Authorization` plus the drive/item IDs parsed from the MPD.

## Privacy notes

HAR files, `video.curl`, `transcript.curl`, and `out.mpd` can contain live or recently live credentials, cookies, signed URLs, tenant identifiers, user identifiers, or recording metadata. Keep examples synthetic and delete runtime artifacts after verification.

For browser capture, the needed data may be omitted unless DevTools is configured to include sensitive HAR data. In Chrome/Edge, this is a DevTools preference under Network, reachable through the gear icon or `F1`; it is not a choice in the macOS save dialog.

The guided workflow keeps these runtime artifacts inside `$DEST_DIR/tmp`, together with the local `uv` cache and downloaded segment/intermediate media files, and deletes that folder only after a successful transcript and video download.
