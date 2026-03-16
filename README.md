# ytdlpchop

`ytdlpchop` is a free-only music identification and triage toolkit for difficult sources.

It has two entrypoints:

- [`bin/yt_audio_id.sh`](/home/keith/Documents/code/ytdlpchop/bin/yt_audio_id.sh) for the original Bash workflow around `yt-dlp`, `ffmpeg`, `fpcalc`, and AcoustID.
- [`bin/ytdlpchop`](/home/keith/Documents/code/ytdlpchop/bin/ytdlpchop) for the higher-level app workflow that fuses multiple signals into one report.

The app is meant for:

- YouTube uploads with missing or unreliable metadata
- Spotify track URLs used as a metadata source and preview-audio source
- local audio or video files
- sources that may be cataloged, uncataloged, channel-original, or AI-generated

## Requirements

Core required:

- `bash`
- `python3`
- `yt-dlp`
- `ffmpeg`
- `ffprobe`
- `fpcalc`

Optional, depending on features you enable:

- `curl` for AcoustID lookups and submissions
- `jq` for metadata parsing and normalized AcoustID summaries
- `gzip` for compressed AcoustID POST requests
- `pyacoustid` for the optional Python lookup backend
- `songrec` for Shazam-style recognition
- `tesseract` for OCR on video frames
- `whisper` for transcript extraction
- `demucs` or the cloned Demucs repo plus Python environment for stem separation
- `java` for Panako
- Python deps for `external/audfprint`

## Supported Sources

- YouTube URLs
- Spotify track URLs
- local files supported by `ffmpeg`

Spotify support is metadata-first:

- read public Spotify page metadata
- use the public preview audio when available
- search for the best matching YouTube source when you want a full public audio source
- carry the Spotify track ID and matched YouTube candidate into the final report

## Features

- Multi-profile clip scans such as `90:45,60:30,45:15`
- Chromaprint fingerprint generation with configurable algorithm
- AcoustID lookup, candidate export, and optional submission support
- SongRec/Shazam-style recognition on clips
- comment mining and timestamp harvesting from YouTube
- OCR against sampled video frames
- Demucs stem separation on excerpts
- Whisper transcript extraction from source audio and vocal stems
- MusicBrainz text search from transcript phrases
- Panako distortion-tolerant matching with per-run local storage
- Audfprint local matching
- local corpus reranking with stored fingerprints
- heuristic AI-audio artifact scoring
- provenance signal scanning in metadata and container fields
- source-level assessment and machine-readable scorecard
- Spotify track URL support using public page metadata, public preview audio when available, and automatic YouTube candidate search

## App Report Model

Each app run writes:

- `reports/summary.json`
- `reports/summary.md`

Important report sections:

- `assessment`: source-level verdict such as `recognized_cataloged_track` or `likely_ai_or_channel_original`
- `scorecard`: explicit evidence counts and booleans used for classification
- `metadata`: normalized source metadata plus Spotify bridge fields when relevant
- `clips`: per-window findings from AcoustID, SongRec, AI heuristics, Audfprint, Panako, corpus, and MusicBrainz
- `panako_store`: the source-audio indexing step used by Panako when enabled
- `transcripts`: Whisper output and transcript-derived MusicBrainz candidates
- `ocr`: extracted on-frame text

The scorecard now includes source-bridge evidence such as:

- `source_type`
- `analysis_audio_source`
- `spotify_track_id_present`
- `spotify_preview_url_present`
- `matched_youtube_candidate_present`
- `youtube_candidate_count`
- recognizer hit counts
- transcript and OCR counts
- provenance and AI heuristic counts

## Usage

Original Bash workflow:

```bash
bin/yt_audio_id.sh 'https://www.youtube.com/watch?v=rEYpDlFzgkk'
```

Higher-level app run on a YouTube source:

```bash
source .env
bin/ytdlpchop identify \
  --comments \
  --max-comments 40 \
  --songrec \
  --acoustid \
  --ai-heuristics \
  --whisper \
  --musicbrainz \
  'https://www.youtube.com/watch?v=rEYpDlFzgkk'
```

Dense run with more free tools enabled:

```bash
source .env
bin/ytdlpchop identify \
  --comments \
  --max-comments 40 \
  --focus-comments \
  --songrec \
  --acoustid \
  --ai-heuristics \
  --demucs \
  --demucs-python .venv-demucs/bin/python3 \
  --whisper \
  --musicbrainz \
  --panako \
  --audfprint \
  --full-source-fingerprint \
  -o out/full-free \
  'https://www.youtube.com/watch?v=rEYpDlFzgkk'
```

Local file smoke test with SongRec plus Panako:

```bash
bin/ytdlpchop identify \
  --songrec \
  --panako \
  --max-clips-per-profile 1 \
  -o out/panako-smoke \
  '/path/to/file.webm'
```

Spotify metadata bridge:

```bash
source .env
bin/ytdlpchop identify \
  --songrec \
  --acoustid \
  --ai-heuristics \
  --whisper \
  --musicbrainz \
  --spotify-youtube-search 5 \
  -o out/spotify \
  'https://open.spotify.com/track/1UEIQUuPESrpdCDHhKLL96'
```

AcoustID-focused Bash workflow examples:

```bash
source .env
bin/yt_audio_id.sh \
  --profiles 30:60,30:20,20:10 \
  -o out/dense \
  'https://www.youtube.com/watch?v=rEYpDlFzgkk'
```

```bash
source .env
bin/yt_audio_id.sh \
  --lookup-backend pyacoustid \
  -o out/py \
  'https://www.youtube.com/watch?v=rEYpDlFzgkk'
```

```bash
source .env
export ACOUSTID_USER_KEY='your_user_key'
bin/yt_audio_id.sh \
  --submit-source \
  --submit-track 'Track Title' \
  --submit-artist 'Artist Name' \
  --submit-album 'Album Title' \
  --submit-year 2026 \
  'https://www.youtube.com/watch?v=...'
```

## Output

Each run creates an output directory containing:

- `download/` original downloaded audio file
- `clips/` generated FLAC clips grouped by profile
- `fingerprints/` raw `fpcalc` output per clip grouped by profile
- `acoustid/` raw AcoustID responses per clip grouped by profile
- `clip-index.tsv` clip timing index across profiles
- `fingerprints.tsv` fingerprint index across profiles
- `acoustid-summary.tsv` best parsed result per clip
- `acoustid-all-candidates.tsv` every parsed AcoustID candidate per clip
- `acoustid-submission.json` source submission response when enabled
- `README.txt` run summary

App runs additionally write:

- `reports/summary.json` combined machine-readable report
- `reports/summary.md` readable summary with best match, scorecard, comments, and clip findings
- Spotify runs also record `metadata.spotify_track_id`, `metadata.analysis_audio_source`, `metadata.preview_url`, and `metadata.matched_youtube_candidate`
- Panako runs also record `panako_store` in the report and per-clip parsed Panako matches

## Current Limitations

- AcoustID is strongest for near-identical matches, not transformed or merely similar tracks.
- SongRec is often stronger than AcoustID for mainstream catalog music, but it is still not guaranteed on obscure or synthetic material.
- Spotify audio support depends on the public preview being exposed. When it is not, the app falls back to YouTube candidate search if enabled.
- AI detection remains heuristic. The score is evidence, not proof.

## Notes

- AcoustID is strongest when matching the original audio, not loosely similar songs or transformed mixes.
- Long atmospheric uploads, AI-generated tracks, and heavily altered material often return raw result IDs with no recording metadata.
- Source submission is best reserved for full known tracks; short clips from mixes are usually poor submission candidates.
- The source assessment is heuristic. Treat `recognized_cataloged_track` as strong, and the other labels as triage signals backed by the scorecard rather than proof.
