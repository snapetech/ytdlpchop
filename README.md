# ytdlpchop

`ytdlpchop` is a music identification app for messy sources:

- YouTube uploads with no track list
- long mixes and “war music” style compilations
- Spotify track URLs, using public metadata and preview audio
- local audio/video files
- sources that may be cataloged, uncataloged, channel-original, or AI-generated

This project is built around one goal: identify tracks from ugly real-world sources as accurately as possible. It uses evidence fusion because one recognizer is not enough:

- exact or near-exact audio recognition
- distortion-tolerant local matching
- transcript and metadata search
- comment and OCR clues
- corpus similarity
- AI-style forensic heuristics

The output is meant to answer three questions:

- what is this track, if it can be identified
- what are the best candidates, if it cannot be identified cleanly
- how strong is the evidence behind that conclusion

## What The App Actually Does

The main CLI is [`bin/ytdlpchop`](/home/keith/Documents/code/ytdlpchop/bin/ytdlpchop).

Given a source, it can:

- download or normalize the media
- chop it into overlapping windows
- fingerprint each window
- run multiple recognizers
- pull transcript and metadata clues
- score AI-like audio artifacts
- summarize all of that into one report

There is also an older Bash workflow at [`bin/yt_audio_id.sh`](/home/keith/Documents/code/ytdlpchop/bin/yt_audio_id.sh). It is still useful for raw AcoustID-oriented scanning, but the Python app is the real product now.

## Identification Strategy

`ytdlpchop` uses layers, not one silver bullet.

### 1. Direct audio recognizers

These are the strongest signals when they hit.

- `SongRec`:
  Shazam-style recognition. In practice this is often the best free recognizer here for mainstream catalog music.
- `AcoustID` + `Chromaprint`:
  Best for near-identical audio, duplicate-ish sources, and known fingerprints already present in the public AcoustID/MusicBrainz ecosystem.

If repeated windows agree, the app treats that as strong evidence.

### 2. Distortion-tolerant and local matching

These help when the source is altered, noisy, or not in public databases.

- `Panako`:
  local distortion-tolerant matcher with per-run local storage
- `audfprint`:
  local matching against a local database
- local corpus reranking:
  compare against fingerprints you have saved from known tracks, known misses, or channel-local material

These do not magically identify a song against the internet. They help you recognize reuse, repeats, and local families of audio.

### 3. Transcript and metadata search

These are weaker than direct audio hits, but still useful.

- `Whisper`:
  transcribe vocals or source excerpts
- `MusicBrainz` search:
  search recordings from transcript phrases
- Spotify metadata bridge:
  use the public Spotify page and preview audio
- YouTube metadata:
  titles, descriptions, chapters, comment signals
- OCR:
  scrape on-screen text from video frames

This layer is for candidate generation and corroboration, not blind trust.

### 4. AI/original-content classification

When no catalog match exists, the app does not stop. It tries to classify what kind of source it is dealing with.

It currently scores:

- audio artifact heuristics from the analyzed clips
- provenance-like keywords in metadata or text
- comment patterns like “what is the song at 8:40?” or “playlist please”
- lack of embedded track metadata
- presence or absence of recognizer hits

This does not prove AI generation. It is a classification layer that helps separate:

- recognized catalog tracks
- likely uncataloged/original material
- likely AI/channel-original material
- mixed cases that still need manual review

## Source Types

### YouTube

The app can use:

- media download
- video metadata
- comments
- OCR
- audio recognition

This is the richest source type for identification work.

### Spotify

`yt-dlp` does not download Spotify audio. Spotify is handled differently:

- parse the public track page
- extract public metadata
- use the public preview URL when present
- optionally search YouTube for a corresponding public upload

That means Spotify support is excellent for metadata and decent for preview-based recognition, but it is limited by what Spotify exposes publicly.

### Local files

If `ffmpeg` can read it, the app can analyze it.

This is the cleanest path when you already have the media locally.

## Verdicts

The app emits a source-level `assessment` after combining all evidence.

Current labels:

- `recognized_cataloged_track`
- `candidate_match_found`
- `likely_uncataloged_or_original`
- `likely_ai_or_channel_original`
- `needs_manual_review`

How to read them:

- `recognized_cataloged_track`:
  strong result, usually driven by repeated direct recognizer hits
- `candidate_match_found`:
  weak candidate, not confirmed enough to treat as solved
- `likely_uncataloged_or_original`:
  probably real music, but not cleanly identified in public systems
- `likely_ai_or_channel_original`:
  no direct recognizer hits plus supporting signals that this may be synthetic or house-made
- `needs_manual_review`:
  mixed evidence, no strong automatic answer yet

The app is intentionally conservative now: transcript-only MusicBrainz candidates are not allowed to masquerade as true IDs without some corroboration.

## Scorecard

Every run writes a machine-readable `scorecard` in `reports/summary.json`.

Important fields include:

- `source_type`
- `analysis_audio_source`
- `embedded_metadata_present`
- `songrec_hit_count`
- `songrec_distinct_match_count`
- `acoustid_recording_hit_count`
- `raw_acoustid_hit_count`
- `panako_hit_count`
- `corpus_match_count`
- `transcript_hit_count`
- `musicbrainz_candidate_count`
- `playlist_request_count`
- `ai_comment_mentions`
- `timestamp_comment_count`
- `mean_ai_artifact_score`
- `max_ai_artifact_score`

Spotify runs also include:

- `spotify_track_id_present`
- `spotify_preview_url_present`
- `matched_youtube_candidate_present`
- `youtube_candidate_count`

Use the scorecard when you want to understand why the app identified something, refused to overclaim, or surfaced only a weak candidate.

## What Works Well

- mainstream catalog tracks
- official uploads and topic-channel uploads
- public Spotify tracks with exposed preview audio
- repeated audio across a local corpus
- sources where comments or OCR provide useful clues

## What Fails Often

- long ambient mixes with no known public registration
- AI-generated or channel-original music with no public fingerprint coverage
- heavily transformed uploads
- obscure music with no MusicBrainz/AcoustID presence

This is normal. It reflects the public ecosystem, not just the app.

## Tooling

Core required:

- `python3`
- `bash`
- `yt-dlp`
- `ffmpeg`
- `ffprobe`
- `fpcalc`

Useful optionals:

- `songrec`
- `curl`
- `jq`
- `gzip`
- `whisper`
- `tesseract`
- Java for `Panako`

Extra integrated source trees under [`external/`](/home/keith/Documents/code/ytdlpchop/external):

- [`external/Panako`](/home/keith/Documents/code/ytdlpchop/external/Panako)
- [`external/audfprint`](/home/keith/Documents/code/ytdlpchop/external/audfprint)
- [`external/demucs`](/home/keith/Documents/code/ytdlpchop/external/demucs)
- [`external/essentia`](/home/keith/Documents/code/ytdlpchop/external/essentia)

Not every vendored tool is equally wired into the app yet:

- `Panako` is integrated and exercised
- `audfprint` is integrated in a basic local-run form
- `Demucs` is used for stem separation
- `Essentia` is vendored for future deeper MIR work and local experiments

## Secrets

Local secrets go in [`.env`](/home/keith/Documents/code/ytdlpchop/.env), which is gitignored.

A safe template lives in [`.env.example`](/home/keith/Documents/code/ytdlpchop/.env.example).

Current env vars of interest:

- `ACOUSTID_API_KEY`
- `ACOUSTID_USER_KEY`

## Typical Runs

Minimal YouTube identification:

```bash
bin/ytdlpchop identify \
  --songrec \
  --acoustid \
  -o out/basic \
  'https://www.youtube.com/watch?v=...'
```

Real YouTube identification run:

```bash
source .env
bin/ytdlpchop identify \
  --comments \
  --max-comments 40 \
  --focus-comments \
  --songrec \
  --acoustid \
  --ai-heuristics \
  --whisper \
  --musicbrainz \
  --panako \
  --audfprint \
  -o out/investigation \
  'https://www.youtube.com/watch?v=...'
```

Heavier run with stem separation:

```bash
source .env
bin/ytdlpchop identify \
  --comments \
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
  'https://www.youtube.com/watch?v=...'
```

Spotify bridge run:

```bash
source .env
bin/ytdlpchop identify \
  --songrec \
  --acoustid \
  --ai-heuristics \
  --whisper \
  --musicbrainz \
  --panako \
  --spotify-youtube-search 5 \
  -o out/spotify \
  'https://open.spotify.com/track/1UEIQUuPESrpdCDHhKLL96'
```

Local file run:

```bash
bin/ytdlpchop identify \
  --songrec \
  --panako \
  --max-clips-per-profile 1 \
  -o out/local \
  '/path/to/file.webm'
```

Original AcoustID-heavy Bash workflow:

```bash
source .env
bin/yt_audio_id.sh \
  --profiles 30:60,30:20,20:10 \
  -o out/dense \
  'https://www.youtube.com/watch?v=...'
```

## Output Layout

App runs write:

- `reports/summary.json`
- `reports/summary.md`

And usually also:

- `download/`
- `clips/`
- `fingerprints/`
- `acoustid/`
- `comments/`
- transcript and excerpt artifacts under `reports/`

The machine-readable report is the source of truth. The Markdown summary is the readable explanation of how the app got to its answer.

## Real-World Interpretation

This app already demonstrated three different behaviors:

- a known YouTube music video was identified cleanly as a catalog track
- a Spotify track was identified from public metadata, preview audio, and matched YouTube source
- a “Viking battle songs” style YouTube upload resisted both SongRec and AcoustID, which pushed the app toward `likely_ai_or_channel_original`

That is the right mental model for this project:

- sometimes you get a precise song ID
- sometimes you only get a candidate
- sometimes the most honest answer is “this looks like uncataloged or AI/channel-original material”

## Limitations

- `AcoustID` is not a general “recognize anything that sounds similar” engine.
- `SongRec` is strong but still not universal.
- Spotify support depends on public metadata and public preview availability.
- AI scoring is heuristic, not proof.
- Public databases are incomplete. Some misses are real misses, not bugs.

## Roadmap Direction

The repo already has the shape needed for deeper MIR and evidence-driven identification work:

- stronger local corpus workflows
- better `audfprint` indexing and querying
- deeper `Essentia` features such as melody, cover similarity, and embeddings
- improved OCR and frame targeting
- better explanation and ranking of candidate evidence

If you care about one sentence summary:

`ytdlpchop` is a free-first audio identification engine that tries to name the track, rank the candidates, and show its evidence instead of bluffing.
