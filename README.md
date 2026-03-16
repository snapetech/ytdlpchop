# ytdlpchopid

`ytdlpchopid` is an audio identification and evidence-scoring engine.

It does two separate jobs on purpose:

- identify the track, if it can
- score how much synthetic-style / AI-mediated evidence is present, if identity is weak or absent

Those are not the same question, and this repo no longer treats them as one question.

## What This Repo Is Actually About

This project is for ugly real-world sources:

- YouTube uploads with no chapters or tracklist
- long mixes and themed “mystery music” uploads
- local files with missing or junk metadata
- Spotify track URLs where public metadata and preview audio are available
- sources that may be cataloged, uncataloged, channel-original, AI-generated, or some mix of those

The important design choice is this:

- `identity` is handled as a matching problem
- `synthetic-likelihood` is handled as a forensic evidence problem

So the app now emits separate outputs for both.

## Core Output Model

Every analyzed file can produce:

- `identity_score`
- `synthetic_score`
- `confidence_score`
- `known_family_score`
- `family_label`
- `quality_class`
- `top_evidence_for`
- `top_evidence_against`
- `lane_scores`
- `lane_confidences`
- `perturbation_stability`
- `identity_assessment`
- `synthetic_assessment`

This is the shape to keep in mind:

- `identity_score`:
  how much evidence says this is a known recording or known reused source
- `synthetic_score`:
  how much synthetic-style evidence is present in the analyzed audio
- `confidence_score`:
  how much trust to place in that synthetic-style read after perturbation checks and source-quality penalties
- `known_family_score`:
  how much evidence points to a known synthetic family or architecture pattern

The machine-readable source of truth is:

- [`reports/summary.json`](/home/keith/Documents/code/ytdlpchopid/reports/summary.json)

The human-readable explanation is:

- [`reports/summary.md`](/home/keith/Documents/code/ytdlpchopid/reports/summary.md)

## Two Independent Decisions

The app now makes two top-level decisions.

### 1. Identity

Identity answers:

- what track is this?
- how strong is the recording match?
- are there only weak candidates or no credible match at all?

Current identity labels:

- `recognized_cataloged_track`
- `candidate_match_found`
- `likely_uncataloged_or_original`
- `possibly_ai_or_channel_original`
- `needs_manual_review`

### 2. Synthetic Likelihood

Synthetic likelihood answers:

- how much forensic evidence points toward synthetic-style or AI-mediated characteristics?
- does that evidence survive mild perturbation?
- is it concentrated in one brittle lane or supported by multiple independent lanes?

Current synthetic labels:

- `low_signal`
- `mixed_or_inconclusive`
- `moderate_suspicion`
- `strong_suspicion`
- `insufficient_evidence`

This split matters. A file can easily be:

- high identity, low synthetic signal
- low identity, mixed or inconclusive synthetic signal
- low identity, moderate synthetic suspicion
- high identity and still synthetic-relevant if it exactly matches a known AI reference item

## How Identity Works

Identity is built from matching and candidate evidence, not from AI heuristics.

Primary identity inputs:

- `SongRec`
- `AcoustID` + `Chromaprint`
- `Panako`
- `audfprint`
- transcript-derived `MusicBrainz` candidates
- source metadata and platform metadata

Interpretation:

- repeated `SongRec` agreement across windows is the strongest current free identity signal in this repo
- `AcoustID` is useful for near-identical public matches, not broad “sounds similar” matching
- `Panako` and `audfprint` help more as local reuse tools than public internet recognizers

Identity should be read as:

- exact or well-supported match
- weak candidate
- unresolved

## How Synthetic Scoring Works

Synthetic scoring is now a lane-based matrix, not one raw detector.

It should be read as a suspicion model, not a proof engine.

The app builds a `forensic_matrix` and then derives:

- `synthetic_score`
- `confidence_score`
- `known_family_score`
- `family_label`

### Lane Overview

The current matrix uses these lanes:

- provenance
- spectral artifacts
- descriptor priors
- structure
- lyrics and speech
- generator family
- confidence / perturbation stability

Each lane contributes evidence differently.

## Lane 1: Provenance

This is the strongest positive lane when present and mostly neutral when absent.

Current behavior:

- scans metadata and container-level text for provenance-like signals
- attempts explicit C2PA detection when tooling is available
- stores any C2PA-related findings under the provenance lane

What this lane means:

- valid provenance can strongly support a synthetic or toolchain-aware conclusion
- missing provenance does not prove anything

## Lane 2: Spectral Artifacts

This is the strongest current waveform-only synthetic-style lane in the repo.

What it looks at:

- averaged high-band spectral peaks
- peak count
- peak prominence
- spacing regularity
- persistence across the analyzed excerpt
- band prominence buckets
- weak family hints from artifact shape

The app currently stores things like:

- `peak_count`
- `peak_density`
- `mean_peak_prominence`
- `peak_persistence`
- `spacing_regularity`
- `dominant_spacing_hz`
- `band_prominence`

This lane is useful, but it is not treated as universal truth. If it fires alone, confidence is capped.

## Lane 3: Descriptor Priors

This is a weak lane by design.

It looks at features such as:

- spectral centroid
- spectral flux
- pitch salience proxy
- duration-based suspicion

This lane exists because some generator families cluster differently on broad audio descriptors, but it is intentionally low-weighted and should not be treated as proof on its own.

## Lane 4: Structure

This is the “music behavior” lane rather than the “spectrogram artifact” lane.

It looks at:

- novelty behavior
- repetition density
- section regularity
- transition sharpness
- tail realism
- microtiming rigidity

This is meant to catch cases where the file is not artifact-loud but still behaves in an overly templated or mechanically structured way.

## Lane 5: Lyrics and Speech

This is the main robustness add-on to artifact analysis.

It uses transcript-derived evidence such as:

- token count
- lexical repetition
- line repetition
- repeated n-gram ratio
- bracket-token ratio
- source / vocals overlap
- speech feature proxy when stems are available

This lane matters because some synthetic signals survive better in lyric and speech behavior than in codec- or frequency-dependent artifact cues.

## Lane 6: Generator Family

This lane is for family hints, not forced attribution.

Current outputs:

- `known`
- `unknown`
- `none`

And a separate `known_family_score`.

This lane is allowed to say:

- there is no family evidence
- this looks like an unknown synthetic family
- this resembles a known architecture-style artifact family

It is not allowed to bluff a precise family label when the evidence is weak.

## Confidence And Perturbation Stability

This is one of the most important parts of the system.

The repo now runs mild perturbation probes against the analyzed excerpt, such as:

- low-pass filtering
- resampling
- mild pitch shift

The point is not to “fix” the file. The point is to ask:

- does the synthetic evidence survive mild changes?
- or is it fragile and likely dependent on one brittle cue?

That shows up as:

- `perturbation_stability`
- `confidence_score`
- `quality_class`
- `notes`

Current quality classes include things like:

- `clean_full_track`
- `clean_excerpt`
- `masked`
- `heavily_transcoded`

Current confidence logic is intentionally conservative:

- one strong lane is not enough for a high-confidence synthetic suspicion call
- artifact-only suspicion gets capped
- strong identity evidence suppresses synthetic overclaiming

## Why This Matters

A normal music recognizer README would stop at “we matched the song.”

This repo should not stop there, because many of the interesting cases are exactly the ones where:

- no public database match exists
- the upload may be channel-original
- the upload may be AI-generated
- the upload may be a transformed derivative
- the evidence is mixed and quality-limited

That is why the repo needs to explain not just the ID result, but the evidence structure.

## What The CLI Does

The main CLI is:

- [`bin/ytdlpchopid`](/home/keith/Documents/code/ytdlpchopid/bin/ytdlpchopid)

The old Bash workflow is still present:

- [`bin/yt_audio_id.sh`](/home/keith/Documents/code/ytdlpchopid/bin/yt_audio_id.sh)

But the Python CLI is the real app now.

Typical full run:

```bash
source .env
bin/ytdlpchopid identify \
  --comments \
  --focus-comments \
  --songrec \
  --acoustid \
  --ai-heuristics \
  --forensic-matrix \
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
bin/ytdlpchopid identify \
  --comments \
  --songrec \
  --acoustid \
  --ai-heuristics \
  --forensic-matrix \
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
bin/ytdlpchopid identify \
  --songrec \
  --acoustid \
  --ai-heuristics \
  --forensic-matrix \
  --whisper \
  --musicbrainz \
  --spotify-youtube-search 5 \
  -o out/spotify \
  'https://open.spotify.com/track/...'
```

## Source Types

### YouTube

The richest source type in the repo.

Possible evidence:

- downloaded media
- metadata
- comments
- OCR
- clip-based audio recognition
- transcript clues
- forensic matrix scoring

### Spotify

Spotify is treated as metadata-first.

The app can use:

- public page metadata
- public preview audio when available
- optional YouTube candidate search

This makes Spotify useful for canonical track identity even though it is not a normal downloadable audio source for `yt-dlp`.

### Local Files

If `ffmpeg` can decode it, the app can score it.

This is the cleanest path for forensic work because it avoids site extraction issues.

## Report Anatomy

Important machine-readable sections:

- `identity_assessment`
- `synthetic_assessment`
- `forensic_matrix`
- `scorecard`
- `clips`
- `transcripts`
- `ocr`

The top report fields worth reading first are:

- `identity_score`
- `synthetic_score`
- `confidence_score`
- `known_family_score`
- `family_label`
- `top_evidence_for`
- `top_evidence_against`

If you are debugging why the app made a call, the most useful blocks are:

- `lane_scores`
- `lane_confidences`
- `forensic_matrix.confidence_lane`
- `forensic_matrix.spectral_artifact_lane`
- `forensic_matrix.lyrics_speech_lane`
- `forensic_matrix.structural_lane`

## What The Current System Is Good At

- identifying mainstream catalog tracks
- proving when a Spotify page and public audio source agree
- separating well-supported identity cases from unresolved cases
- surfacing synthetic-style artifact evidence without automatically overclaiming
- preserving evidence for later threshold tuning

## What It Is Still Weak At

- long atmospheric mixes with no public registration
- hard synthetic claims when only one lane fires
- family attribution for unknown generators
- fully reliable lyrics/speech scoring from noisy or short ASR excerpts
- deep MIR features that are only vendored today and not fully wired yet

## What Is Already Vendored Or Integrated

Core tools:

- `python3`
- `bash`
- `yt-dlp`
- `ffmpeg`
- `ffprobe`
- `fpcalc`

Optional but important:

- `songrec`
- `curl`
- `jq`
- `gzip`
- `whisper`
- `tesseract`
- Java for `Panako`

Integrated source trees:

- [`external/Panako`](/home/keith/Documents/code/ytdlpchopid/external/Panako)
- [`external/audfprint`](/home/keith/Documents/code/ytdlpchopid/external/audfprint)
- [`external/demucs`](/home/keith/Documents/code/ytdlpchopid/external/demucs)
- [`external/essentia`](/home/keith/Documents/code/ytdlpchopid/external/essentia)

Current status:

- `Panako` is integrated
- `audfprint` is wired in a basic local form
- `Demucs` is used for stem separation
- `Essentia` is vendored for future deeper MIR and forensic work

## Secrets

Local secrets live in:

- [`.env`](/home/keith/Documents/code/ytdlpchopid/.env)

Template:

- [`.env.example`](/home/keith/Documents/code/ytdlpchopid/.env.example)

Current env vars of interest:

- `ACOUSTID_API_KEY`
- `ACOUSTID_USER_KEY`

## Bottom Line

This repo is no longer just “a tool that tries to name songs.”

It is an identification system with a second, explicit evidence model for synthetic-likelihood. The README should make that obvious, because that is now the real shape of the app.
