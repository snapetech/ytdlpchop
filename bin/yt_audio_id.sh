#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  yt_audio_id.sh [options] <youtube_url>

Downloads the best audio track from a YouTube URL, chops it into clips,
computes Chromaprint fingerprints with fpcalc, and optionally queries AcoustID.

Required tools:
  yt-dlp ffmpeg ffprobe fpcalc

Optional tools:
  curl    - for AcoustID lookups and submissions
  jq      - for metadata parsing and AcoustID summaries
  gzip    - for compressed AcoustID POST bodies
  python3 - for URL encoding and the optional pyacoustid backend

Options:
  -o, --outdir DIR            Output directory (default: ./yt-audio-id-<timestamp>)
  -l, --clip-length SECONDS   Length of each clip in seconds (default: 30)
  -s, --step SECONDS          Distance between clip start times (default: 60)
      --profiles LIST         Comma-separated clip:step pairs, e.g. 30:60,30:20,20:10
      --start SECONDS         Start offset in seconds (default: 0)
      --end SECONDS           End offset in seconds (default: full duration)
      --limit N               Maximum number of clips per profile (default: unlimited)
      --audio-format EXT      Download audio format/container hint for yt-dlp (default: bestaudio native)
      --algorithm NUM         fpcalc algorithm to use (default: 2)
      --lookup-backend MODE   Lookup backend: auto, curl, pyacoustid (default: auto)
      --full-source-fingerprint
                              Save a full-length source fingerprint with fpcalc -length 0
      --corpus-dir DIR        Local fingerprint corpus directory for second-pass reranking
      --corpus-top N          Number of local corpus matches to keep per clip (default: 5)
      --corpus-add-source LABEL
                              Add the full source fingerprint to the corpus under this label
      --keep-download         Keep the original downloaded container file (default: yes)
      --delete-download       Delete the original downloaded file after clip generation
      --no-acoustid           Skip AcoustID lookups even if ACOUSTID_API_KEY is set
      --submit-source         Submit the full downloaded source audio fingerprint to AcoustID
      --submit-track TITLE    Track title for source submission
      --submit-artist NAME    Track artist for source submission
      --submit-album TITLE    Album title for source submission
      --submit-album-artist   Album artist for source submission
      --submit-year YEAR      Release year for source submission
      --submit-trackno N      Track number for source submission
      --submit-discno N       Disc number for source submission
      --submit-mbid UUID      MusicBrainz recording ID for source submission
      --wait-submission       Poll submission status once after submitting the source fingerprint
  -h, --help                  Show this help

Environment:
  ACOUSTID_API_KEY            Optional AcoustID application API key for lookup
  ACOUSTID_USER_KEY           Optional AcoustID user API key for submissions

Examples:
  yt_audio_id.sh 'https://www.youtube.com/watch?v=rEYpDlFzgkk'
  ACOUSTID_API_KEY=your_key yt_audio_id.sh --profiles 30:60,30:20 'https://www.youtube.com/watch?v=rEYpDlFzgkk'
  ACOUSTID_API_KEY=your_key yt_audio_id.sh --lookup-backend pyacoustid 'https://www.youtube.com/watch?v=rEYpDlFzgkk'
USAGE
}

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_value() {
  local opt="$1"
  local val="${2-}"
  [[ -n "$val" ]] || die "$opt requires a value"
}

sanitize_name() {
  local s="$1"
  s="${s//\//-}"
  s="${s//:/-}"
  s="${s//$'\n'/ }"
  printf '%s' "$s"
}

format_hms() {
  local total="$1"
  printf '%02d:%02d:%02d' "$((total/3600))" "$(((total%3600)/60))" "$((total%60))"
}

py_eval() {
  python3 - "$@"
}

build_urlencoded_payload() {
  py_eval "$@" <<'PY'
import sys
from urllib.parse import urlencode

pairs = []
for arg in sys.argv[1:]:
    key, value = arg.split("=", 1)
    pairs.append((key, value))
sys.stdout.write(urlencode(pairs))
PY
}

ensure_dir() {
  mkdir -p "$1"
}

write_gzip_payload() {
  local payload="$1"
  local gz_path="$2"
  printf '%s' "$payload" | gzip -c > "$gz_path"
}

have_pyacoustid() {
  py_eval <<'PY' >/dev/null 2>&1
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("acoustid") else 1)
PY
}

perform_lookup_curl() {
  local duration="$1"
  local fingerprint="$2"
  local json_path="$3"
  local payload_gz
  local payload

  payload="$(build_urlencoded_payload \
    "client=${ACOUSTID_API_KEY}" \
    "duration=${duration}" \
    "fingerprint=${fingerprint}" \
    "meta=recordings+recordingids+releasegroups+releasegroupids+releases+tracks+sources" \
    "format=json")"
  payload_gz="$(mktemp)"
  write_gzip_payload "$payload" "$payload_gz"
  curl -sS -X POST 'https://api.acoustid.org/v2/lookup' \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    -H 'Content-Encoding: gzip' \
    --data-binary "@${payload_gz}" \
    > "$json_path"
  rm -f -- "$payload_gz"
}

perform_lookup_pyacoustid() {
  local duration="$1"
  local fingerprint="$2"
  local json_path="$3"

  python3 "$SCRIPT_DIR/../scripts/acoustid_lookup.py" \
    --api-key "$ACOUSTID_API_KEY" \
    --duration "$duration" \
    --fingerprint "$fingerprint" \
    --backend pyacoustid \
    --output "$json_path"
}

perform_lookup() {
  local duration="$1"
  local fingerprint="$2"
  local json_path="$3"
  local backend="$LOOKUP_BACKEND"

  case "$backend" in
    auto)
      if have_pyacoustid; then
        perform_lookup_pyacoustid "$duration" "$fingerprint" "$json_path"
      else
        perform_lookup_curl "$duration" "$fingerprint" "$json_path"
      fi
      ;;
    curl)
      perform_lookup_curl "$duration" "$fingerprint" "$json_path"
      ;;
    pyacoustid)
      have_pyacoustid || die "--lookup-backend pyacoustid requested, but the acoustid Python module is not installed"
      perform_lookup_pyacoustid "$duration" "$fingerprint" "$json_path"
      ;;
    *)
      die "Unknown --lookup-backend: $backend"
      ;;
  esac
}

append_lookup_rows() {
  local profile_label="$1"
  local clip_name="$2"
  local start="$3"
  local json_path="$4"

  if ! have_cmd jq; then
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$profile_label" "$clip_name" "$start" '' '' '' '' '' '' "$json_path" \
      >> "$ACOUSTID_TSV"
    return
  fi

  local summary_line
  summary_line="$(jq -r --arg profile "$profile_label" --arg clip "$clip_name" --arg start "$start" --arg json "$json_path" '
    if .status != "ok" or (.results | length == 0) then
      [$profile, $clip, $start, "", "", "", "", "", "", $json] | @tsv
    else
      .results
      | sort_by(.score) | reverse | .[0] as $r
      | ($r.recordings[0] // {}) as $rec
      | [
          $profile,
          $clip,
          $start,
          ($r.score // ""),
          ($r.id // ""),
          (($rec.artists // [] | map(.name) | join(", ")) // ""),
          ($rec.title // ""),
          ($rec.id // ""),
          (($rec.releasegroups // [] | map(.title) | join(" | ")) // ""),
          $json
        ]
      | @tsv
    end
  ' "$json_path" 2>/dev/null || true)"
  [[ -n "$summary_line" ]] || summary_line="$(printf '%s\t%s\t%s\t\t\t\t\t\t\t%s' "$profile_label" "$clip_name" "$start" "$json_path")"
  printf '%s\n' "$summary_line" >> "$ACOUSTID_TSV"

  jq -r --arg profile "$profile_label" --arg clip "$clip_name" --arg start "$start" --arg json "$json_path" '
    if .status != "ok" or (.results | length == 0) then
      [$profile, $clip, $start, "", "", "", "", "", "", $json] | @tsv
    else
      .results[]
      | if (.recordings | type) == "array" and (.recordings | length) > 0 then
          . as $r
          | .recordings[]
          | [
              $profile,
              $clip,
              $start,
              ($r.score // ""),
              ($r.id // ""),
              ((.artists // [] | map(.name) | join(", ")) // ""),
              (.title // ""),
              (.id // ""),
              ((.releasegroups // [] | map(.title) | join(" | ")) // ""),
              $json
            ]
          | @tsv
        else
          [$profile, $clip, $start, (.score // ""), (.id // ""), "", "", "", "", $json] | @tsv
        end
    end
  ' "$json_path" >> "$ACOUSTID_ALL_TSV"
}

submit_source_fingerprint() {
  (( SUBMIT_SOURCE == 1 )) || return 0
  [[ -n "${ACOUSTID_API_KEY:-}" ]] || die "--submit-source requires ACOUSTID_API_KEY"
  [[ -n "${ACOUSTID_USER_KEY:-}" ]] || die "--submit-source requires ACOUSTID_USER_KEY"
  have_cmd curl || die "--submit-source requires curl"
  have_cmd gzip || die "--submit-source requires gzip"

  local source_fp_text source_fp_duration source_fingerprint payload payload_gz submit_json

  log "Generating full-source fingerprint for submission"
  source_fp_text="$OUTDIR/source.fp.txt"
  fpcalc "$SOURCE_AUDIO" > "$source_fp_text"
  source_fp_duration="$(awk -F= '/^DURATION=/{print $2}' "$source_fp_text" | head -n1)"
  source_fingerprint="$(sed -n 's/^FINGERPRINT=//p' "$source_fp_text" | head -n1)"
  [[ -n "$source_fp_duration" && -n "$source_fingerprint" ]] || die "Could not fingerprint source audio for submission"

  payload="$(build_urlencoded_payload \
    "client=${ACOUSTID_API_KEY}" \
    "user=${ACOUSTID_USER_KEY}" \
    "format=json" \
    "duration.0=${source_fp_duration}" \
    "fingerprint.0=${source_fingerprint}" \
    "fileformat.0=${SOURCE_AUDIO##*.}" \
    "track.0=${SUBMIT_TRACK}" \
    "artist.0=${SUBMIT_ARTIST}" \
    "album.0=${SUBMIT_ALBUM}" \
    "albumartist.0=${SUBMIT_ALBUM_ARTIST}" \
    "year.0=${SUBMIT_YEAR}" \
    "trackno.0=${SUBMIT_TRACKNO}" \
    "discno.0=${SUBMIT_DISCNO}" \
    "mbid.0=${SUBMIT_MBID}")"
  payload_gz="$(mktemp)"
  write_gzip_payload "$payload" "$payload_gz"
  submit_json="$OUTDIR/acoustid-submission.json"

  log "Submitting full-source fingerprint to AcoustID"
  curl -sS -X POST 'https://api.acoustid.org/v2/submit' \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    -H 'Content-Encoding: gzip' \
    --data-binary "@${payload_gz}" \
    > "$submit_json"
  rm -f -- "$payload_gz"

  if (( WAIT_SUBMISSION == 1 )) && have_cmd jq; then
    local submission_id status_json
    submission_id="$(jq -r '.submissions[0].id // empty' "$submit_json" 2>/dev/null || true)"
    if [[ -n "$submission_id" ]]; then
      status_json="$OUTDIR/acoustid-submission-status.json"
      curl -sS -G 'https://api.acoustid.org/v2/submission_status' \
        --data-urlencode "client=${ACOUSTID_API_KEY}" \
        --data-urlencode "id=${submission_id}" \
        --data-urlencode 'format=json' \
        > "$status_json"
    fi
  fi
}

generate_full_source_fingerprint() {
  local full_fp_path="$OUTDIR/source.full.fp.txt"
  log "Generating full-source fingerprint"
  fpcalc -length 0 -algorithm "$FPCALC_ALGORITHM" "$SOURCE_AUDIO" > "$full_fp_path"
  FULL_SOURCE_FP_PATH="$full_fp_path"
}

add_source_to_corpus() {
  [[ -n "$CORPUS_DIR" ]] || return 0
  [[ -n "$CORPUS_ADD_SOURCE_LABEL" ]] || return 0
  [[ -n "$FULL_SOURCE_FP_PATH" ]] || return 0

  ensure_dir "$CORPUS_DIR"
  local base_name meta_path corpus_fp_path
  base_name="$(sanitize_name "$CORPUS_ADD_SOURCE_LABEL")"
  corpus_fp_path="$CORPUS_DIR/${base_name}.fp.txt"
  meta_path="$CORPUS_DIR/${base_name}.meta.json"

  cp -- "$FULL_SOURCE_FP_PATH" "$corpus_fp_path"
  cat > "$meta_path" <<EOF
{"label":"$CORPUS_ADD_SOURCE_LABEL","source_url":"$URL","video_id":"$VIDEO_ID","video_title":"$VIDEO_TITLE_RAW","created_at":"$(date --iso-8601=seconds)"}
EOF
}

append_corpus_matches() {
  local profile_label="$1"
  local clip_name="$2"
  local start="$3"
  local fp_text="$4"
  local result_path="$OUTDIR/corpus-match.tmp.tsv"

  [[ -n "$CORPUS_DIR" ]] || return 0
  [[ -d "$CORPUS_DIR" ]] || return 0

  python3 "$SCRIPT_DIR/../scripts/corpus_compare.py" \
    --query "$fp_text" \
    --corpus-dir "$CORPUS_DIR" \
    --top "$CORPUS_TOP" \
    > "$result_path"

  if [[ -s "$result_path" ]]; then
    awk -F '\t' -v profile="$profile_label" -v clip="$clip_name" -v start="$start" 'NF {print profile "\t" clip "\t" start "\t" $0}' "$result_path" >> "$CORPUS_TSV"
  fi
  rm -f -- "$result_path"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTDIR="./yt-audio-id-$(date '+%Y%m%d-%H%M%S')"
CLIP_LENGTH=30
STEP=60
PROFILES_SPEC=""
START_AT=0
END_AT=""
LIMIT=""
AUDIO_FORMAT=""
FPCALC_ALGORITHM=2
LOOKUP_BACKEND="auto"
FULL_SOURCE_FINGERPRINT=0
FULL_SOURCE_FP_PATH=""
CORPUS_DIR=""
CORPUS_TOP=5
CORPUS_ADD_SOURCE_LABEL=""
KEEP_DOWNLOAD=1
USE_ACOUSTID=1
SUBMIT_SOURCE=0
SUBMIT_TRACK=""
SUBMIT_ARTIST=""
SUBMIT_ALBUM=""
SUBMIT_ALBUM_ARTIST=""
SUBMIT_YEAR=""
SUBMIT_TRACKNO=""
SUBMIT_DISCNO=""
SUBMIT_MBID=""
WAIT_SUBMISSION=0
URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--outdir)
      require_value "$1" "${2-}"
      OUTDIR="$2"
      shift 2
      ;;
    -l|--clip-length)
      require_value "$1" "${2-}"
      CLIP_LENGTH="$2"
      shift 2
      ;;
    -s|--step)
      require_value "$1" "${2-}"
      STEP="$2"
      shift 2
      ;;
    --profiles)
      require_value "$1" "${2-}"
      PROFILES_SPEC="$2"
      shift 2
      ;;
    --start)
      require_value "$1" "${2-}"
      START_AT="$2"
      shift 2
      ;;
    --end)
      require_value "$1" "${2-}"
      END_AT="$2"
      shift 2
      ;;
    --limit)
      require_value "$1" "${2-}"
      LIMIT="$2"
      shift 2
      ;;
    --audio-format)
      require_value "$1" "${2-}"
      AUDIO_FORMAT="$2"
      shift 2
      ;;
    --algorithm)
      require_value "$1" "${2-}"
      FPCALC_ALGORITHM="$2"
      shift 2
      ;;
    --lookup-backend)
      require_value "$1" "${2-}"
      LOOKUP_BACKEND="$2"
      shift 2
      ;;
    --full-source-fingerprint)
      FULL_SOURCE_FINGERPRINT=1
      shift
      ;;
    --corpus-dir)
      require_value "$1" "${2-}"
      CORPUS_DIR="$2"
      shift 2
      ;;
    --corpus-top)
      require_value "$1" "${2-}"
      CORPUS_TOP="$2"
      shift 2
      ;;
    --corpus-add-source)
      require_value "$1" "${2-}"
      CORPUS_ADD_SOURCE_LABEL="$2"
      shift 2
      ;;
    --keep-download)
      KEEP_DOWNLOAD=1
      shift
      ;;
    --delete-download)
      KEEP_DOWNLOAD=0
      shift
      ;;
    --no-acoustid)
      USE_ACOUSTID=0
      shift
      ;;
    --submit-source)
      SUBMIT_SOURCE=1
      shift
      ;;
    --submit-track)
      require_value "$1" "${2-}"
      SUBMIT_TRACK="$2"
      shift 2
      ;;
    --submit-artist)
      require_value "$1" "${2-}"
      SUBMIT_ARTIST="$2"
      shift 2
      ;;
    --submit-album)
      require_value "$1" "${2-}"
      SUBMIT_ALBUM="$2"
      shift 2
      ;;
    --submit-album-artist)
      require_value "$1" "${2-}"
      SUBMIT_ALBUM_ARTIST="$2"
      shift 2
      ;;
    --submit-year)
      require_value "$1" "${2-}"
      SUBMIT_YEAR="$2"
      shift 2
      ;;
    --submit-trackno)
      require_value "$1" "${2-}"
      SUBMIT_TRACKNO="$2"
      shift 2
      ;;
    --submit-discno)
      require_value "$1" "${2-}"
      SUBMIT_DISCNO="$2"
      shift 2
      ;;
    --submit-mbid)
      require_value "$1" "${2-}"
      SUBMIT_MBID="$2"
      shift 2
      ;;
    --wait-submission)
      WAIT_SUBMISSION=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      die "Unknown option: $1"
      ;;
    *)
      if [[ -z "$URL" ]]; then
        URL="$1"
      else
        die "Unexpected extra argument: $1"
      fi
      shift
      ;;
  esac
done

if [[ -z "$URL" && $# -gt 0 ]]; then
  URL="$1"
fi
[[ -n "$URL" ]] || {
  usage
  exit 1
}

[[ "$CLIP_LENGTH" =~ ^[0-9]+$ ]] || die "--clip-length must be an integer"
[[ "$STEP" =~ ^[0-9]+$ ]] || die "--step must be an integer"
[[ "$START_AT" =~ ^[0-9]+$ ]] || die "--start must be an integer"
[[ -z "$END_AT" || "$END_AT" =~ ^[0-9]+$ ]] || die "--end must be an integer"
[[ -z "$LIMIT" || "$LIMIT" =~ ^[0-9]+$ ]] || die "--limit must be an integer"
[[ "$FPCALC_ALGORITHM" =~ ^[1-5]$ ]] || die "--algorithm must be an integer from 1 to 5"
[[ "$CORPUS_TOP" =~ ^[0-9]+$ ]] || die "--corpus-top must be an integer"
(( CLIP_LENGTH > 0 )) || die "--clip-length must be > 0"
(( STEP > 0 )) || die "--step must be > 0"

need_cmd yt-dlp
need_cmd ffmpeg
need_cmd ffprobe
need_cmd fpcalc
have_cmd python3 || die "python3 is required"
if (( USE_ACOUSTID == 1 )) && [[ -n "${ACOUSTID_API_KEY:-}" ]]; then
  have_cmd curl || die "ACOUSTID_API_KEY is set but curl is missing"
  have_cmd gzip || die "ACOUSTID_API_KEY is set but gzip is missing"
fi

declare -a PROFILE_LENGTHS=()
declare -a PROFILE_STEPS=()
declare -a PROFILE_LABELS=()

if [[ -n "$PROFILES_SPEC" ]]; then
  IFS=',' read -r -a raw_profiles <<< "$PROFILES_SPEC"
  (( ${#raw_profiles[@]} > 0 )) || die "--profiles must not be empty"
  for raw_profile in "${raw_profiles[@]}"; do
    [[ "$raw_profile" =~ ^([0-9]+):([0-9]+)$ ]] || die "Invalid profile '$raw_profile' (expected clip:step)"
    PROFILE_LENGTHS+=("${BASH_REMATCH[1]}")
    PROFILE_STEPS+=("${BASH_REMATCH[2]}")
    PROFILE_LABELS+=("l${BASH_REMATCH[1]}_s${BASH_REMATCH[2]}")
  done
else
  PROFILE_LENGTHS+=("$CLIP_LENGTH")
  PROFILE_STEPS+=("$STEP")
  PROFILE_LABELS+=("l${CLIP_LENGTH}_s${STEP}")
fi

mkdir -p "$OUTDIR" "$OUTDIR/download" "$OUTDIR/clips" "$OUTDIR/fingerprints" "$OUTDIR/acoustid"
if [[ -n "$CORPUS_DIR" ]]; then
  ensure_dir "$CORPUS_DIR"
fi
for profile_label in "${PROFILE_LABELS[@]}"; do
  mkdir -p "$OUTDIR/clips/$profile_label" "$OUTDIR/fingerprints/$profile_label" "$OUTDIR/acoustid/$profile_label"
done

log "Fetching metadata"
VIDEO_JSON="$OUTDIR/video.info.json"
yt-dlp --dump-single-json "$URL" > "$VIDEO_JSON"

VIDEO_ID=""
VIDEO_TITLE_RAW=""
if have_cmd jq; then
  VIDEO_ID="$(jq -r '.id // empty' "$VIDEO_JSON" 2>/dev/null || true)"
  VIDEO_TITLE_RAW="$(jq -r '.title // empty' "$VIDEO_JSON" 2>/dev/null || true)"
fi
if [[ -z "$VIDEO_ID" || -z "$VIDEO_TITLE_RAW" ]]; then
  VIDEO_ID="$(grep -o '"id": *"[^"]*"' "$VIDEO_JSON" | head -n1 | sed 's/.*"id": *"\([^"]*\)"/\1/')"
  VIDEO_TITLE_RAW="$(grep -o '"title": *"[^"]*"' "$VIDEO_JSON" | head -n1 | sed 's/.*"title": *"\([^"]*\)"/\1/')"
fi
[[ -n "$VIDEO_ID" ]] || VIDEO_ID="unknownid"
[[ -n "$VIDEO_TITLE_RAW" ]] || VIDEO_TITLE_RAW="youtube-audio"
VIDEO_TITLE="$(sanitize_name "$VIDEO_TITLE_RAW")"

log "Downloading best audio track"
DOWNLOAD_TEMPLATE="$OUTDIR/download/%(title)s [%(id)s].%(ext)s"
if [[ -n "$AUDIO_FORMAT" ]]; then
  yt-dlp -f bestaudio --extract-audio --audio-format "$AUDIO_FORMAT" -o "$DOWNLOAD_TEMPLATE" "$URL"
else
  yt-dlp -f bestaudio -o "$DOWNLOAD_TEMPLATE" "$URL"
fi

mapfile -t downloaded_files < <(find "$OUTDIR/download" -maxdepth 1 -type f ! -name '*.part' | sort)
(( ${#downloaded_files[@]} > 0 )) || die "No downloaded audio file found"
SOURCE_AUDIO="${downloaded_files[0]}"

log "Using source audio: $(basename "$SOURCE_AUDIO")"
TOTAL_DURATION_RAW="$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$SOURCE_AUDIO")"
TOTAL_DURATION="${TOTAL_DURATION_RAW%.*}"
[[ "$TOTAL_DURATION" =~ ^[0-9]+$ ]] || die "Could not determine audio duration"

if [[ -z "$END_AT" ]]; then
  END_AT="$TOTAL_DURATION"
fi
(( END_AT > START_AT )) || die "--end must be greater than --start"
(( START_AT < TOTAL_DURATION )) || die "--start must be before total duration"
if (( END_AT > TOTAL_DURATION )); then
  END_AT="$TOTAL_DURATION"
fi

log "Audio duration: ${TOTAL_DURATION}s ($(format_hms "$TOTAL_DURATION"))"
log "Clipping from $(format_hms "$START_AT") to $(format_hms "$END_AT")"

CLIP_INDEX_TSV="$OUTDIR/clip-index.tsv"
FINGERPRINTS_TSV="$OUTDIR/fingerprints.tsv"
ACOUSTID_TSV="$OUTDIR/acoustid-summary.tsv"
ACOUSTID_ALL_TSV="$OUTDIR/acoustid-all-candidates.tsv"
CORPUS_TSV="$OUTDIR/corpus-rerank.tsv"

printf 'profile\tclip\tstart_seconds\tstart_hms\tend_seconds\tend_hms\tclip_path\n' > "$CLIP_INDEX_TSV"
printf 'profile\tclip\tstart_seconds\tduration\tfingerprint_file\n' > "$FINGERPRINTS_TSV"
printf 'profile\tclip\tstart_seconds\tscore\tacoustid_id\tartist\ttitle\trecording_id\treleasegroup\tjson_file\n' > "$ACOUSTID_TSV"
printf 'profile\tclip\tstart_seconds\tscore\tacoustid_id\tartist\ttitle\trecording_id\treleasegroup\tjson_file\n' > "$ACOUSTID_ALL_TSV"
printf 'profile\tclip\tstart_seconds\tlocal_score\tcorpus_label\tcorpus_fp_path\tcorpus_meta_path\n' > "$CORPUS_TSV"

total_clip_count=0
for idx in "${!PROFILE_LABELS[@]}"; do
  profile_label="${PROFILE_LABELS[$idx]}"
  profile_clip_length="${PROFILE_LENGTHS[$idx]}"
  profile_step="${PROFILE_STEPS[$idx]}"
  profile_clip_count=0

  log "Profile ${profile_label}: clip_length=${profile_clip_length}s step=${profile_step}s"

  for (( start=START_AT; start<END_AT; start+=profile_step )); do
    if [[ -n "$LIMIT" ]] && (( profile_clip_count >= LIMIT )); then
      break
    fi

    remaining=$(( END_AT - start ))
    (( remaining > 0 )) || break

    duration="$profile_clip_length"
    if (( duration > remaining )); then
      duration="$remaining"
    fi
    if (( duration < 10 )); then
      break
    fi

    clip_name="${profile_label}__clip_$(printf '%04d' "$profile_clip_count")_$(format_hms "$start" | tr ':' '-')_${duration}s.flac"
    clip_path="$OUTDIR/clips/$profile_label/$clip_name"

    log "Creating $clip_name"
    ffmpeg -hide_banner -loglevel error -y \
      -ss "$start" -t "$duration" -i "$SOURCE_AUDIO" \
      -vn -map_metadata -1 -c:a flac \
      "$clip_path"

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$profile_label" "$clip_name" "$start" "$(format_hms "$start")" "$((start+duration))" "$(format_hms "$((start+duration))")" "$clip_path" \
      >> "$CLIP_INDEX_TSV"

    fp_text="$OUTDIR/fingerprints/$profile_label/${clip_name%.flac}.fp.txt"
    fpcalc -length "$duration" -algorithm "$FPCALC_ALGORITHM" "$clip_path" > "$fp_text"

    fp_duration="$(awk -F= '/^DURATION=/{print $2}' "$fp_text" | head -n1)"
    fingerprint="$(sed -n 's/^FINGERPRINT=//p' "$fp_text" | head -n1)"
    [[ -n "$fp_duration" ]] || die "fpcalc did not return a duration for $clip_name"
    [[ -n "$fingerprint" ]] || die "fpcalc did not return a fingerprint for $clip_name"

    printf '%s\t%s\t%s\t%s\t%s\n' "$profile_label" "$clip_name" "$start" "$fp_duration" "$fp_text" >> "$FINGERPRINTS_TSV"
    append_corpus_matches "$profile_label" "$clip_name" "$start" "$fp_text"

    if (( USE_ACOUSTID == 1 )) && [[ -n "${ACOUSTID_API_KEY:-}" ]]; then
      json_path="$OUTDIR/acoustid/$profile_label/${clip_name%.flac}.json"
      log "Querying AcoustID for $clip_name"
      perform_lookup "$fp_duration" "$fingerprint" "$json_path"
      append_lookup_rows "$profile_label" "$clip_name" "$start" "$json_path"
      sleep 0.4
    fi

    (( profile_clip_count+=1 ))
    (( total_clip_count+=1 ))
  done
done

if (( total_clip_count == 0 )); then
  die "No clips were generated. Check your --start/--end/--clip-length settings."
fi

if (( FULL_SOURCE_FINGERPRINT == 1 || SUBMIT_SOURCE == 1 || ${#CORPUS_ADD_SOURCE_LABEL} > 0 )); then
  generate_full_source_fingerprint
fi

submit_source_fingerprint
add_source_to_corpus

if (( KEEP_DOWNLOAD == 0 )); then
  log "Deleting original downloaded file"
  rm -f -- "$SOURCE_AUDIO"
fi

README_PATH="$OUTDIR/README.txt"
cat > "$README_PATH" <<EOF
Source URL: $URL
Video title: $VIDEO_TITLE_RAW
Video ID: $VIDEO_ID
Sanitized title: $VIDEO_TITLE
Source audio: $SOURCE_AUDIO
Output directory: $OUTDIR
Profiles: ${PROFILE_LABELS[*]}
Clips generated: $total_clip_count
Lookup backend: $LOOKUP_BACKEND
fpcalc algorithm: $FPCALC_ALGORITHM

Files:
- clip-index.tsv             clip timing index across all profiles
- fingerprints.tsv           fingerprint index across all profiles
- acoustid-summary.tsv       top parsed AcoustID result per clip, including raw AcoustID result IDs
- acoustid-all-candidates.tsv all parsed AcoustID candidates per clip
- corpus-rerank.tsv          local second-pass corpus matches per clip
- clips/                     generated FLAC clips, grouped by profile
- fingerprints/              raw fpcalc output per clip, grouped by profile
- acoustid/                  raw AcoustID JSON responses per clip, grouped by profile
- acoustid-submission.json   source submission response when --submit-source is used
- source.full.fp.txt         full-length source fingerprint when enabled

Notes:
- AcoustID is designed for matching original audio, not generic "similar sounding" music.
- If acoustid-summary.tsv is mostly empty, the clips were likely not recognized or the source is uncataloged.
- Submitting fingerprints is most useful for full known tracks; clips from long mixes are usually poor submission candidates.
EOF

log "Done"
log "Output: $OUTDIR"
log "Clip index: $CLIP_INDEX_TSV"
log "Fingerprints: $FINGERPRINTS_TSV"
if (( USE_ACOUSTID == 1 )) && [[ -n "${ACOUSTID_API_KEY:-}" ]]; then
  log "AcoustID summary: $ACOUSTID_TSV"
  log "AcoustID candidates: $ACOUSTID_ALL_TSV"
else
  log "AcoustID lookup skipped"
fi
if [[ -n "$CORPUS_DIR" ]]; then
  log "Local corpus rerank: $CORPUS_TSV"
fi
