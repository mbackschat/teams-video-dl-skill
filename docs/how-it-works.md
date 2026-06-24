# How it works

Technical notes on how a Microsoft Stream-on-SharePoint recording and its transcript are retrieved. All identifiers below are placeholders (`<tenant>`, `<user>`, `b!<driveId>`, `01<itemId>`); nothing tenant-specific is hardcoded — the scripts derive everything from the capture at runtime.

## The two products

A recording exposes two things on the same SharePoint host:

1. **Video** — an MPEG-DASH stream, **SEA-encrypted** (`aes128-cbc`), described by a manifest (`videomanifest`). The media is a server-side **transcode** (often low frame rate), which is the only quality the player exposes.
2. **Transcript** — served by the SharePoint "vroom" API. Two representations of the *same* transcript exist; only one has speaker names (see below).

## Authentication realms

Three different secrets are involved, which is why a single HAR capture is the easiest way to collect them:

- **Segment/manifest host** `https://<tenant>-my.sharepoint.com` — needs your **SPO session cookies** (`FedAuth`/`rtFa`). The signed segment URLs also carry a short-lived `P1` token inside the manifest.
- **Key host** `https://<region>-mediap.svc.ms/transform/VideoProtectionKey?...` — needs an `X-SPOPacToken` (~1 h PAC token). We sidestep it by capturing the resulting **16-byte key once**; the key value itself does not expire.
- **Transcript** — needs **both** an `Authorization: Bearer` access token (~1 h) **and** the SPO cookies. Cookies alone → `401 unauthenticated`.

Because the `FedAuth`/`rtFa` cookies and the in-memory MSAL Bearer cannot be read by page JavaScript (HttpOnly / in-memory), they must be captured off real requests — via a HAR export or DevTools "Copy as cURL".

Chrome/Edge may export a sanitized HAR unless DevTools is allowed to include sensitive data. The setting lives inside DevTools, not in the macOS save dialog: open DevTools Settings with the gear icon or `F1`, go to Preferences, search for `HAR` or scroll to Network, then enable "Allow to generate HAR with sensitive data". Localized builds may translate the label, but it should mention HAR and sensitive data.

## Video: download, decrypt, mux (`sp_stream_dl.py`)

1. Parse the DASH manifest (`out.mpd`): `BaseURL`, per-track `SegmentTemplate` (init + media segments via a `SegmentTimeline`), the `keyUriTemplate`, and the `IV`.
2. Fetch each segment with the session cookies.
3. **Decrypt** with AES-128-CBC using the captured key and the manifest IV, then **strip PKCS#7 padding**. This padding is the subtle part: the plaintext is *always* PKCS#7-padded, and a block-aligned segment gets a **full extra 16-byte `0x10` block** — so every segment has 1–16 trailing pad bytes that must be removed. (Forgetting this corrupts the stream: e.g. an init of 771 bytes pads with 13 bytes of `0x0D`, which looks like stray `\r\r\r\r` before the first box.)
4. Concatenate `init + segments` per track, then mux video+audio with `ffmpeg -c copy -movflags +faststart`.

Segments are cached per track; re-running resumes and skips what's already downloaded (useful when a ~1 h cookie expires mid-download — recapture and re-run).

The guided workflow passes `--work-dir "$TMP_DIR/stream"` so segment caches and intermediate track files (`video.mp4`, `audio.mp4`) stay under the destination-local temp folder instead of cluttering the final output directory.

## Transcript: the `?format=json` trick (`sp_transcript_dl.py`)

Discovery lists the transcript:

```text
GET {origin}{site}/_api/v2.1/drives/{driveId}/items/{itemId}
      ?select=media/transcripts,audioTracks&$expand=media/transcripts,media/audioTracks
```

The content endpoint serves **two representations of the same transcript**:

| Request | Result |
| --- | --- |
| `…/transcripts/{id}/content` (default) | `text/vtt` — **WebVTT with no speaker tags** |
| `…/transcripts/{id}/content?format=json` | `application/json` — **Stream Transcript JSON with `speakerDisplayName`** |
| `…/content` + `Accept: application/json` | `406 notAcceptable` (default content type is `text/vtt`) |

So the key is to append **`?format=json`**. The JSON shape:

```json
{
  "$schema": "http://stream.office.com/schemas/transcript.json",
  "type": "Transcript",
  "entries": [
    {
      "text": "…",
      "speakerDisplayName": "Speaker Name",
      "speakerId": "<aadObjectId>@<tenantId>",
      "startOffset": "00:00:03.7167320",
      "endOffset": "00:00:05.5567320"
    }
  ]
}
```

`parse_teams_json()` turns `entries[]` into cues; `parse_vtt()` remains as a fallback if the content is ever served as VTT.

### Encoding gotcha

SharePoint returns the transcript **without a charset** in the content type. Python `requests`' `response.text` then defaults to ISO-8859-1, which mojibakes every non-ASCII character (e.g. German umlauts) and turns a UTF-8 BOM into `ï»¿`. The fix: decode the raw **bytes as `utf-8-sig`** (and strip a leading BOM before sniffing WebVTT vs JSON).

### If `?format=json` ever stops working

The authoritative speaker-attributed source is the Microsoft Graph `callTranscript` API (`/users/{id}/onlineMeetings/{meetingId}/transcripts/{id}/content?$format=text/vtt` returns `<v Speaker>` VTT; `/metadataContent` returns JSON with `speakerName`). That path requires `OnlineMeetingTranscript.Read.All` (admin-consented) plus mapping the recording to its `onlineMeetingId` via the meeting join URL — much heavier than the `?format=json` shortcut.

## One capture, all inputs (`har_extract.py`)

From a single HAR export it locates, by request-URL pattern: the `videomanifest` request (→ `out.mpd`), the `VideoProtectionKey` request (→ `key.bin`, preferring the HAR's own response body, else a re-fetch), and an `_api` request carrying a Bearer token (→ a synthetic `transcript.curl` plus the `driveId`/`itemId`). It also writes the segment cookie to `video.curl` and suggests an output basename from the recording's filename.

Some SharePoint transcode sessions do not show a standalone `VideoProtectionKey` request in the HAR. In that case `har_extract.py` parses the key URL, drive ID, and item ID from the MPD, re-fetches the key with the captured `X-SPOPacToken`, and can build `transcript.curl` from a SharePoint media request that carries `X-Authorization`. See [sharepoint-transcode-findings.md](sharepoint-transcode-findings.md) for the sanitized notes from that capture shape.

## Temporary workspace

The guided workflow treats the destination directory as final-output-only and creates `$DEST_DIR/tmp` for scratch files. The moved HAR export, `har_extract.py` outputs, local `uv` cache, downloaded media segments, and intermediate track files all live there. The folder is deleted only after the transcript and video downloads both succeed; failed or interrupted runs leave it in place so the segment cache can resume.

## Output naming

The transcript sidecars (`.srt`/`.vtt`) deliberately use the **same basename** as the video so players (VLC, etc.) auto-load them as external subtitles.

When `har_extract.py` suggests a basename from a Teams recording filename, it keeps the recording date from the `-YYYYMMDD_HHMMSS-...` suffix as `YYYY-MM-DD` to avoid collisions across repeated meetings.
