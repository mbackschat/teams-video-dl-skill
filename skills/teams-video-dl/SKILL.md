---
name: teams-video-dl
description: Download a Microsoft Teams / SharePoint / Stream meeting recording (video) AND its speaker-attributed transcript from a stream.aspx URL. Use when the user wants to save, archive, or download a Teams meeting recording, a SharePoint/OneDrive "Recordings" video, or a Microsoft Stream video, especially when they also want the transcript/subtitles with speaker names. Guides the user through a one-time browser capture (HAR export) for the required login tokens.
metadata:
  argument-hint: "[stream.aspx URL]"
---

# teams-video-dl — download a Teams/SharePoint recording + transcript

Download a Microsoft Stream-on-SharePoint meeting recording (the SEA-encrypted DASH video) **and** its transcript with **speaker attribution**, from a `stream.aspx` URL.
The user is assumed to be non-technical — guide them gently and never assume they know what DevTools is.

## What you produce

For a recording the user wants saved as `<BASE>` (e.g. `My_Meeting`):
- `<BASE>.mp4` — the muxed video.
- `<BASE>.srt`, `<BASE>.vtt` — subtitles **with speaker names**. Same basename as the video on purpose, so video players (VLC, etc.) auto-load them as sidecars.
- `<BASE>.txt` — readable speaker-grouped transcript.
- `<BASE>.raw.json` — the raw Stream transcript JSON (keep or delete).

## Why a browser capture is needed (say this plainly to the user)

The recording is DRM-protected and locked to the user's login. None of it is reachable without secrets that live only in their signed-in browser session: a DASH manifest, a 16-byte decryption key (from a separate key server), the SharePoint session cookies, and a ~1-hour access token for the transcript. You **cannot** get these for them. The least painful way to capture all of them at once is a single **HAR export** (a recording of the browser's network traffic). Tokens expire in ~1 hour, so capture and run promptly.

## Prerequisites (check once, install if missing)

- `uv` (the user prefers it) — all scripts run via `uv run --with …`.
- `ffmpeg` — used to mux video+audio. Check with `ffmpeg -version`; if missing, suggest `brew install ffmpeg` (macOS).

Before running commands, set `SKILL_DIR` to the absolute path of this skill directory (the directory containing this `SKILL.md`). Some agents expose that path directly; otherwise resolve it from the selected skill file path.

```
SKILL_DIR="<absolute path to the directory containing this SKILL.md>"
```

`$SKILL_DIR/scripts/` holds: `har_extract.py`, `sp_stream_dl.py`, `sp_transcript_dl.py`, `sp_curl.py`.

## Step 1 — get the URL and a destination

Ask for the `stream.aspx` URL (the page URL when viewing the recording) if not given, and where to save the final files (default: the current directory). Treat that folder as the final output folder, not as scratch space.

Create one destination-local temp folder for this workflow. All HAR-derived files, the moved HAR export, the `uv` cache, segment caches, and intermediate track files must live under this folder. If a previous run failed, keep the folder so the video download can resume; delete it only after the transcript and video have both succeeded.

```
DEST_DIR="<absolute path to the final output directory>"
TMP_DIR="$DEST_DIR/tmp"
mkdir -p "$DEST_DIR" "$TMP_DIR"
```

## Step 2 — guide the HAR capture (newbie-friendly)

Give the user these exact steps. Use Chrome or Edge.

1. Open the recording's page (the `stream.aspx` URL) and make sure it plays / you're logged in.
2. Open DevTools: press **F12** (Windows) or **⌥⌘I** (Mac). A panel opens.
3. Click the **Network** tab at the top of that panel.
4. Enable HAR export with credentials:
   - Open DevTools Settings: click the **gear** icon or press **F1** while DevTools is focused.
   - In **Preferences**, search for `HAR` or scroll to the **Network** section.
   - Tick **Allow to generate HAR with sensitive data**. In German Chrome/Edge this may be translated with **HAR** and **sensible Daten**. This setting is in DevTools itself, not in the macOS file save dialog.
   - Close DevTools Settings.
5. Tick **Preserve log**. Click the **🚫 (clear)** button to empty the list.
6. **Reload** the page (**⌘R** / **Ctrl+R**). Click **Play** and let the video run ~10 seconds.
7. Open the **transcript / closed-captions panel** in the player (so the transcript loads).
8. Click the **⤓ Export HAR…** button (a down-arrow in the Network toolbar). On macOS there is usually no extra option in the save dialog; the sensitive/sanitized behavior is controlled by the DevTools setting above. If the tooltip still says "HAR (sanitized)" / "HAR (bereinigt)", export it anyway and we will check whether it contains the required request headers and bodies.
9. Tell me the full path to the saved `.har` file. If the browser lets you choose the save location easily, save it inside `tmp`; otherwise save it anywhere and we will move it there before reading it.

Reassure them: the HAR is only used locally; it does contain login tokens, so we'll keep it in the temp folder and delete that folder at the end of a successful run. Nothing is uploaded anywhere.

## Step 3 — extract the inputs from the HAR

Move the HAR into the temp folder before reading it, so cleanup removes the credential-bearing capture too. If the user already saved it as `$TMP_DIR/capture.har`, skip the move.

```
HAR_PATH="<path-to.har>"
HAR_FILE="$TMP_DIR/capture.har"
mv "$HAR_PATH" "$HAR_FILE"
uv --cache-dir "$TMP_DIR/uv-cache" run --with requests "$SKILL_DIR/scripts/har_extract.py" "$HAR_FILE" --out-dir "$TMP_DIR"
```

This writes `out.mpd`, `key.bin`, `video.curl`, `transcript.curl`, and `basename.txt` inside `$TMP_DIR`.
If it prints **INCOMPLETE**, read which piece is missing and tell the user what to redo (usually: actually press Play, or open the transcript panel) and re-export the HAR.

Read `$TMP_DIR/basename.txt` for a suggested `<BASE>`; confirm it with the user (they may want a cleaner name). Use the same `<BASE>` for both downloads so the subtitles auto-load.

## Step 4 — download the transcript (fast; do this first)

```
uv --cache-dir "$TMP_DIR/uv-cache" run --with requests "$SKILL_DIR/scripts/sp_transcript_dl.py" \
    --from-curl "$TMP_DIR/transcript.curl" -o "$DEST_DIR/<BASE>"
```

Confirm it reports speakers (e.g. the discovery line shows `src= microsoft teams`) and that `$DEST_DIR/<BASE>.srt` contains `Name:` prefixes. If it 401s, the token expired → redo Step 2.

## Step 5 — download the video

First a quick probe (cheap, verifies the key + cookies decrypt cleanly):

```
uv --cache-dir "$TMP_DIR/uv-cache" run --with cryptography --with requests "$SKILL_DIR/scripts/sp_stream_dl.py" \
    "$TMP_DIR/out.mpd" --key "$TMP_DIR/key.bin" --from-curl "$TMP_DIR/video.curl" --probe
```

If it says `PROBE: OK`, download for real (this fetches hundreds of segments; it resumes if interrupted — just re-run the same command, optionally after a fresh HAR if cookies expired):

```
uv --cache-dir "$TMP_DIR/uv-cache" run --with cryptography --with requests "$SKILL_DIR/scripts/sp_stream_dl.py" \
    "$TMP_DIR/out.mpd" --key "$TMP_DIR/key.bin" --from-curl "$TMP_DIR/video.curl" \
    --full -o "$DEST_DIR/<BASE>.mp4" --work-dir "$TMP_DIR/stream"
```

## Step 6 — verify and clean up

- `ffprobe "$DEST_DIR/<BASE>.mp4"` — confirm a sane duration and a video+audio stream.
- Report what was produced (files + transcript speaker count / duration).
- After both the transcript and video are successful, delete the entire temp folder:

```
find "$TMP_DIR" -name .DS_Store -delete 2>/dev/null || true
rm -rf "$TMP_DIR"
if [ -d "$TMP_DIR" ]; then
  sleep 1
  find "$TMP_DIR" -name .DS_Store -delete 2>/dev/null || true
  rm -rf "$TMP_DIR"
fi
if [ -d "$TMP_DIR" ]; then
  echo "WARNING: temp cleanup incomplete; remove $TMP_DIR manually before sharing logs or the machine."
fi
```

Do not delete `$TMP_DIR` after a failed or interrupted video download; it contains the segment cache needed to resume and the local `uv` cache needed to rerun without re-downloading dependencies. On success, deleting it removes the HAR, `*.curl` credentials, `out.mpd`, `key.bin`, `uv-cache`, and segment/intermediate media files in one step.

## Fallback — no HAR (Copy as cURL)

If HAR export is unavailable, the user can instead **right-click → Copy → Copy as cURL** on specific Network requests and save each to a file. Then:
- the `videomanifest` request → save body to `$TMP_DIR/out.mpd` (run it with `curl -sL … -o "$TMP_DIR/out.mpd"`),
- any `_api/v2.1/...transcripts...` request → save as `$TMP_DIR/transcript.curl`, then run `sp_transcript_dl.py --from-curl "$TMP_DIR/transcript.curl" -o "$DEST_DIR/<BASE>"`,
- the segment cookie comes from the manifest request → save as `$TMP_DIR/video.curl`, then run `sp_stream_dl.py … --from-curl "$TMP_DIR/video.curl" --work-dir "$TMP_DIR/stream"`,
- the `VideoProtectionKey` request → run it to save the 16 bytes as `$TMP_DIR/key.bin`.

The HAR path is far easier for a newbie; prefer it.

## Notes

- Never print the user's tokens/cookies; the scripts read them from files and don't echo them.
- The transcript content type is `text/vtt` by default (no speakers); the script appends `?format=json` to get the speaker-attributed Stream Transcript JSON (the repo's `docs/how-it-works.md` explains the full mechanics).
- The streaming media is a transcode (often low fps); that's the only quality the player exposes.
