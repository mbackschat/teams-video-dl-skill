# teams-video-dl — Teams Video Downloader

A coding-agent plugin that downloads a **Microsoft Teams / SharePoint / Microsoft Stream meeting recording** — both the **video** and its **transcript with speaker names** — starting from the `stream.aspx` page URL.

It works on the SEA-encrypted DASH stream that the Stream web player uses, and on the SharePoint "vroom" transcript API. It is built for non-technical users: the skill walks you through a one-time browser capture to obtain the login tokens that the recording is locked behind.

## What you get

For a recording you choose to save as `My_Meeting`:

| File | Contents |
| --- | --- |
| `My_Meeting.mp4` | the muxed video |
| `My_Meeting.srt`, `My_Meeting.vtt` | subtitles **with speaker names** — same basename as the video so players auto-load them |
| `My_Meeting.txt` | a readable, speaker-grouped transcript |
| `My_Meeting.raw.json` | the raw Stream transcript JSON |

## Requirements

- [`uv`](https://docs.astral.sh/uv/) — every script runs via `uv run --with …` (no manual dependency installs).
- [`ffmpeg`](https://ffmpeg.org/) — used to mux the video and audio tracks. macOS: `brew install ffmpeg`.
- A Chromium-based browser (Chrome or Edge) where you are signed in to the recording.

## Install

### Claude Code

```text
/plugin marketplace add <your-github-user>/teams-video-dl
/plugin install teams-video-dl@teams-video-dl
```

Replace `<your-github-user>` with the account hosting this repository.

### OpenAI Codex

Codex uses the same shared skill under `skills/teams-video-dl/`; the Codex-specific packaging only adds `.codex-plugin/plugin.json` and `.agents/plugins/marketplace.json`.

For local testing from this checkout:

```text
codex plugin marketplace add /path/to/teams-video-dl
codex plugin add teams-video-dl@teams-video-dl
```

Replace `/path/to/teams-video-dl` with this repository path. You can also open `/plugins` in Codex and install **Teams Video Downloader** from the `teams-video-dl` marketplace.

## Use

Just ask, e.g.:

```text
/teams-video-dl:teams-video-dl https://<tenant>-my.sharepoint.com/.../stream.aspx?id=...
```

or simply *"download this Teams recording with its transcript: <url>"*. In Codex, ask the same thing directly or invoke the installed plugin/skill from the prompt UI. The skill then:

1. Explains and guides a one-time **HAR export** from your browser's DevTools (this captures the DASH manifest, the decryption key, your session cookies, and the transcript access token all at once).
2. Extracts those inputs from the HAR (`har_extract.py`) into a destination-local temp folder.
3. Downloads the transcript with speaker attribution (`sp_transcript_dl.py`).
4. Downloads, decrypts, and muxes the video (`sp_stream_dl.py`).
5. Verifies the output and deletes the destination-local temp folder after success.

Tokens are short-lived (~1 hour), so do the capture right before running.

## Temporary workspace

The destination folder is treated as the place for final files only. During a run, the skill creates `$DEST_DIR/tmp` and keeps all scratch data there: the moved HAR export, HAR-derived files (`out.mpd`, `key.bin`, `video.curl`, `transcript.curl`, `basename.txt`), the local `uv` cache, downloaded segment caches, and intermediate track files.

The temp folder is deleted only after a successful transcript and video download. If a run fails or is interrupted, the folder is left in place so the video segment cache can resume instead of starting over.

## How it works

See [docs/how-it-works.md](docs/how-it-works.md) for the technical details: the two authentication realms, the SEA `aes128-cbc` + PKCS#7 decryption, the `?format=json` trick that yields the speaker-attributed transcript (versus the speaker-stripped WebVTT), and the UTF-8 decoding gotcha.

## Privacy & security

- All processing is local. The HAR and the extracted `*.curl` files contain your login tokens and session cookies; the skill keeps them in `$DEST_DIR/tmp` and deletes that folder after a successful run. Nothing is ever uploaded.
- The scripts read credentials from files and never print them.
- This repository contains no recordings, tokens, tenant URLs, or personal data — everything is derived at runtime from your own capture.

## Layout

```text
.
├── .claude-plugin/
│   ├── plugin.json
│   └── marketplace.json
├── .codex-plugin/
│   └── plugin.json
├── .agents/
│   └── plugins/
│       └── marketplace.json
├── skills/
│   └── teams-video-dl/
│       ├── SKILL.md
│       └── scripts/
│           ├── har_extract.py        # parse one HAR -> all download inputs
│           ├── sp_transcript_dl.py   # transcript (with speakers) + SRT/VTT/TXT
│           ├── sp_stream_dl.py        # SEA-DASH video: download, decrypt, mux
│           └── sp_curl.py             # shared "Copy as cURL" parser
├── tests/
├── docs/how-it-works.md
├── README.md
└── LICENSE
```

## Development

```text
uv run --with pytest --with requests --with cryptography pytest tests/ -q
```

## Disclaimer

For downloading recordings you are authorized to access. Respect your organization's policies and applicable law.

## License

MIT — see [LICENSE](LICENSE).
