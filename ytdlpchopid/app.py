#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


TIMESTAMP_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")
SPOTIFY_TRACK_RE = re.compile(r"https?://open\.spotify\.com/track/([A-Za-z0-9]+)")
OG_META_RE = re.compile(
    r'<meta\s+(?:property|name)=["\'](?P<key>[^"\']+)["\']\s+content=["\'](?P<value>[^"\']*)["\']',
    re.IGNORECASE,
)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
    )


def command_exists(name: str) -> bool:
    return subprocess.run(["bash", "-lc", f"command -v {shlex.quote(name)} >/dev/null 2>&1"]).returncode == 0


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitize_name(value: str) -> str:
    return value.replace("/", "-").replace(":", "-").replace("\n", " ")


def parse_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def format_hms(total: int) -> str:
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def parse_timestamp(text: str) -> int | None:
    match = TIMESTAMP_RE.search(text)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def parse_profiles(spec: str) -> list[tuple[int, int]]:
    profiles = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        clip_length, step = raw.split(":", 1)
        profiles.append((int(clip_length), int(step)))
    if not profiles:
        raise ValueError("no profiles parsed")
    return profiles


@dataclass
class SourceAssets:
    source_type: str
    source: str
    audio_path: Path
    video_path: Path | None
    info_json: Path | None
    metadata: dict[str, Any]
    duration: int
    title: str


def ffprobe_duration(path: Path) -> int:
    proc = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            str(path),
        ]
    )
    return int(float(proc.stdout.strip()))


def ffprobe_json(path: Path) -> dict[str, Any]:
    proc = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ]
    )
    return json.loads(proc.stdout)


def resolve_downloaded_file(directory: Path, exts: tuple[str, ...] = ()) -> Path:
    files = sorted(p for p in directory.iterdir() if p.is_file() and not p.name.endswith(".part"))
    if exts:
        files = [p for p in files if p.suffix.lower() in exts]
    if not files:
        raise FileNotFoundError(f"no downloaded file found in {directory}")
    return files[0]


def fetch_url_text(url: str) -> str:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_url_bytes(url: str) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def with_query_param(url: str, key: str, value: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query[key] = [value]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query, doseq=True),
            parsed.fragment,
        )
    )


def extract_og_meta(html_text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for match in OG_META_RE.finditer(html_text):
        meta[match.group("key").strip().lower()] = html.unescape(match.group("value"))
    return meta


def regex_group(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    return html.unescape(match.group(1).strip())


def search_youtube_candidates(query: str, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    proc = run(["yt-dlp", "--dump-single-json", f"ytsearch{limit}:{query}"])
    data = json.loads(proc.stdout)
    candidates = []
    for entry in data.get("entries") or []:
        candidates.append(
            {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "channel": entry.get("channel"),
                "uploader": entry.get("uploader"),
                "duration": entry.get("duration"),
                "track": entry.get("track"),
                "artist": entry.get("artist"),
                "album": entry.get("album"),
                "release_date": entry.get("release_date"),
                "description": entry.get("description"),
                "webpage_url": entry.get("webpage_url"),
            }
        )
    return candidates


def fetch_spotify_assets(
    url: str,
    outdir: Path,
    youtube_search_limit: int,
    need_video: bool,
    need_comments: bool,
    max_comments: int,
) -> SourceAssets:
    ensure_dir(outdir / "download")
    html_text = fetch_url_text(url)
    meta = extract_og_meta(html_text)
    track_id_match = SPOTIFY_TRACK_RE.search(url)
    track_id = track_id_match.group(1) if track_id_match else None

    title = meta.get("og:title") or regex_group(html_text, r'"name":"([^"]+)"') or "spotify-track"
    desc_parts = [part.strip() for part in (meta.get("og:description") or "").split("·") if part.strip()]
    artist = (
        regex_group(html_text, r'"byArtist":\{"items":\[\{"name":"([^"]+)"')
        or regex_group(html_text, r'"artist":\{"profile":{"name":"([^"]+)"')
        or (desc_parts[0] if len(desc_parts) >= 1 else None)
        or regex_group(meta.get("og:description", ""), r"a song by (.+?) on spotify")
    )
    album = (
        regex_group(html_text, r'"albumOfTrack":\{"name":"([^"]+)"')
        or regex_group(html_text, r'"inAlbum":\{"name":"([^"]+)"')
        or (desc_parts[1] if len(desc_parts) >= 2 else None)
    )
    release_date = regex_group(html_text, r'"releaseDate":\{"isoString":"([^"]+)"') or regex_group(
        html_text, r'"datePublished":"([^"]+)"'
    )
    preview_url = meta.get("og:audio")
    youtube_candidates: list[dict[str, Any]] = []
    if artist and title and youtube_search_limit > 0:
        youtube_candidates = search_youtube_candidates(f"{title} {artist}", youtube_search_limit)

    spotify_metadata: dict[str, Any] = {
        "spotify_track_id": track_id,
        "title": title,
        "track": title,
        "artist": artist,
        "album": album,
        "release_date": release_date,
        "preview_url": preview_url,
        "page_url": url,
        "youtube_candidates": youtube_candidates,
        "matched_youtube_candidate": youtube_candidates[0] if youtube_candidates else None,
        "og_meta": meta,
    }

    if preview_url:
        preview_path = outdir / "download" / "preview.mp3"
        preview_path.write_bytes(fetch_url_bytes(preview_url))
        spotify_metadata["analysis_audio_source"] = "spotify_preview"
        return SourceAssets("spotify", url, preview_path, None, None, spotify_metadata, ffprobe_duration(preview_path), title)

    if youtube_candidates and youtube_candidates[0].get("webpage_url"):
        yt_assets = fetch_youtube_assets(
            youtube_candidates[0]["webpage_url"],
            outdir,
            need_video,
            need_comments,
            max_comments,
        )
        merged_metadata = dict(yt_assets.metadata)
        merged_metadata.update({key: value for key, value in spotify_metadata.items() if value})
        merged_metadata["spotify_source"] = spotify_metadata
        merged_metadata["analysis_audio_source"] = "youtube_candidate"
        return SourceAssets("spotify+youtube", url, yt_assets.audio_path, yt_assets.video_path, yt_assets.info_json, merged_metadata, yt_assets.duration, title)

    raise RuntimeError("Spotify track page does not expose a preview URL and no matching YouTube candidate was found")


def fetch_youtube_assets(url: str, outdir: Path, need_video: bool, need_comments: bool, max_comments: int) -> SourceAssets:
    ensure_dir(outdir / "download")
    info_json = outdir / "video.info.json"
    info = json.loads(run(["yt-dlp", "--dump-single-json", url]).stdout)
    info_json.write_text(json.dumps(info, ensure_ascii=True, indent=2), encoding="utf-8")

    audio_tpl = str(outdir / "download" / "%(title)s [%(id)s].%(ext)s")
    run(["yt-dlp", "-f", "bestaudio", "-o", audio_tpl, url], capture=False)
    audio_path = resolve_downloaded_file(outdir / "download")

    video_path = None
    if need_video:
        ensure_dir(outdir / "video")
        video_tpl = str(outdir / "video" / "%(title)s [%(id)s].%(ext)s")
        run(
            [
                "yt-dlp",
                "-f",
                "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
                "-o",
                video_tpl,
                url,
            ],
            capture=False,
        )
        video_path = resolve_downloaded_file(outdir / "video")

    if need_comments:
        comments_dir = outdir / "comments"
        ensure_dir(comments_dir)
        comments_tpl = str(comments_dir / "%(id)s.%(ext)s")
        run(
            [
                "yt-dlp",
                "--skip-download",
                "--write-comments",
                "--write-info-json",
                "--extractor-args",
                f"youtube:max_comments={max_comments}",
                "-o",
                comments_tpl,
                url,
            ],
            capture=False,
        )

    duration = int(info.get("duration") or ffprobe_duration(audio_path))
    title = info.get("title") or audio_path.stem
    return SourceAssets("youtube", url, audio_path, video_path, info_json, info, duration, title)


def local_file_assets(path_str: str, outdir: Path) -> SourceAssets:
    path = Path(path_str).expanduser().resolve()
    probe = ffprobe_json(path)
    metadata = {"local_path": str(path), "ffprobe": probe}
    duration = ffprobe_duration(path)
    return SourceAssets("local", str(path), path, path, None, metadata, duration, path.stem)


def extract_clip(source: Path, start: int, duration: int, out_path: Path) -> None:
    ensure_dir(out_path.parent)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            str(source),
            "-vn",
            "-map_metadata",
            "-1",
            "-c:a",
            "flac",
            str(out_path),
        ],
        capture=False,
    )


def extract_pcm_mono(path: Path, sample_rate: int = 16000) -> np.ndarray:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    samples = np.frombuffer(proc.stdout, dtype=np.int16)
    if samples.size == 0:
        return np.array([], dtype=np.float32)
    return samples.astype(np.float32) / 32768.0


def fpcalc(path: Path, length: int, algorithm: int) -> tuple[float, str, str]:
    proc = run(["fpcalc", "-length", str(length), "-algorithm", str(algorithm), str(path)])
    text = proc.stdout
    duration = 0.0
    fingerprint = ""
    for line in text.splitlines():
      if line.startswith("DURATION="):
            duration = float(line.split("=", 1)[1])
      elif line.startswith("FINGERPRINT="):
            fingerprint = line.split("=", 1)[1]
    if not fingerprint:
        raise RuntimeError(f"missing fingerprint for {path}")
    return duration, fingerprint, text


def fpcalc_full(path: Path, algorithm: int) -> tuple[float, str, str]:
    proc = run(["fpcalc", "-length", "0", "-algorithm", str(algorithm), str(path)])
    text = proc.stdout
    duration = 0.0
    fingerprint = ""
    for line in text.splitlines():
        if line.startswith("DURATION="):
            duration = float(line.split("=", 1)[1])
        elif line.startswith("FINGERPRINT="):
            fingerprint = line.split("=", 1)[1]
    return duration, fingerprint, text


def acoustid_lookup(api_key: str, duration: float, fingerprint: str, out_path: Path) -> dict[str, Any]:
    ensure_dir(out_path.parent)
    run(
        [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "scripts" / "acoustid_lookup.py"),
            "--api-key",
            api_key,
            "--duration",
            str(int(duration)),
            "--fingerprint",
            fingerprint,
            "--backend",
            "auto",
            "--output",
            str(out_path),
        ],
        capture=False,
    )
    return parse_json_file(out_path)


def songrec_lookup(audio_path: Path) -> dict[str, Any]:
    proc = run(["songrec", "audio-file-to-recognized-song", str(audio_path)])
    return json.loads(proc.stdout)


def corpus_rerank(query_fp_path: Path, corpus_dir: Path, top: int) -> list[dict[str, Any]]:
    proc = run(
        [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "scripts" / "corpus_compare.py"),
            "--query",
            str(query_fp_path),
            "--corpus-dir",
            str(corpus_dir),
            "--top",
            str(top),
        ]
    )
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        rows.append(
            {
                "score": float(parts[0]),
                "label": parts[1],
                "fingerprint_path": parts[2],
                "meta_path": parts[3],
            }
        )
    return rows


def extract_comments_findings(comments_info_path: Path) -> tuple[list[dict[str, Any]], list[int]]:
    data = parse_json_file(comments_info_path)
    findings = []
    timestamps = []
    for comment in data.get("comments") or []:
        text = comment.get("text") or ""
        lowered = text.lower()
        if any(token in lowered for token in ("playlist", "track", "song", "what is", "name of", "ai", "cd")):
            findings.append(
                {
                    "author": comment.get("author"),
                    "text": text,
                    "parent": comment.get("parent"),
                }
            )
        ts = parse_timestamp(text)
        if ts is not None:
            timestamps.append(ts)
    return findings, sorted(set(timestamps))


def ocr_frame(video_path: Path, timestamp: int, image_path: Path) -> str:
    ensure_dir(image_path.parent)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(image_path),
        ],
        capture=False,
    )
    proc = run(["tesseract", str(image_path), "stdout", "-l", "eng"])
    return proc.stdout.strip()


def collect_evenly_spaced_timestamps(duration: int, count: int) -> list[int]:
    if count <= 0:
        return []
    step = max(duration // (count + 1), 1)
    return [step * (idx + 1) for idx in range(count)]


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    denom = float(np.linalg.norm(vec_a) * np.linalg.norm(vec_b))
    if denom <= 1e-9:
        return 0.0
    return float(np.dot(vec_a, vec_b) / denom)


def local_minimum_baseline(values: np.ndarray, radius: int) -> np.ndarray:
    if values.size == 0:
        return values
    baseline = np.empty_like(values)
    for idx in range(values.size):
        start = max(0, idx - radius)
        stop = min(values.size, idx + radius + 1)
        baseline[idx] = values[start:stop].min()
    return baseline


def frame_signal(samples: np.ndarray, frame_size: int, hop: int) -> np.ndarray | None:
    if samples.size < frame_size:
        return None
    window = np.hanning(frame_size).astype(np.float32)
    frames = []
    for start in range(0, samples.size - frame_size + 1, hop):
        frames.append(samples[start : start + frame_size] * window)
    if not frames:
        return None
    return np.stack(frames)


def compute_log_spectrum(frames: np.ndarray) -> np.ndarray:
    spectrum = np.abs(np.fft.rfft(frames, axis=1))
    return np.log1p(spectrum)


def dominant_autocorr_strength(samples: np.ndarray, sample_rate: int, min_hz: float = 80.0, max_hz: float = 1000.0) -> float:
    if samples.size < sample_rate:
        return 0.0
    usable = samples[: min(samples.size, sample_rate * 8)]
    usable = usable - usable.mean()
    corr = np.correlate(usable, usable, mode="full")[usable.size - 1 :]
    if corr.size == 0 or corr[0] <= 1e-9:
        return 0.0
    min_lag = max(1, int(sample_rate / max_hz))
    max_lag = min(corr.size - 1, int(sample_rate / min_hz))
    if max_lag <= min_lag:
        return 0.0
    return float(np.max(corr[min_lag : max_lag + 1]) / corr[0])


def render_ffmpeg_filter(source_path: Path, out_path: Path, filter_chain: str, sample_rate: int) -> Path | None:
    ensure_dir(out_path.parent)
    try:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_path),
                "-vn",
                "-af",
                filter_chain,
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                str(out_path),
            ],
            capture=False,
        )
    except subprocess.CalledProcessError:
        return None
    return out_path if out_path.exists() else None


def analyze_spectral_artifact_lane(samples: np.ndarray, sample_rate: int) -> dict[str, Any] | None:
    usable = samples[: min(samples.size, sample_rate * 60)]
    frames = frame_signal(usable, 4096, 1024)
    if frames is None:
        return None
    spectrum = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(4096, d=1.0 / sample_rate)
    high_hz = min(16000.0, (sample_rate / 2.0) - 100.0)
    if high_hz <= 5200.0:
        return None
    band_mask = (freqs >= 5000.0) & (freqs <= high_hz)
    band_freqs = freqs[band_mask]
    band_spectrum = spectrum[:, band_mask]
    if band_spectrum.shape[1] < 32:
        return None

    avg_spectrum = band_spectrum.mean(axis=0)
    baseline = local_minimum_baseline(avg_spectrum, 10)
    residual = np.maximum(avg_spectrum - baseline, 0.0)
    threshold = float(residual.mean() + (1.75 * residual.std()))
    prominent = np.where(residual > threshold)[0]
    if prominent.size:
        prominences = residual[prominent]
        persistent_hits = []
        for idx in prominent:
            per_frame_threshold = float(band_spectrum[:, idx].mean() + (1.25 * band_spectrum[:, idx].std()))
            persistent_hits.append(float(np.mean(band_spectrum[:, idx] > per_frame_threshold)))
        persistence = float(np.mean(persistent_hits))
        mean_prominence = float(np.mean(prominences))
    else:
        persistence = 0.0
        mean_prominence = 0.0

    spacings = np.diff(band_freqs[prominent]) if prominent.size >= 2 else np.array([], dtype=np.float32)
    dominant_spacing_hz = 0.0
    spacing_regularity = 0.0
    if spacings.size:
        rounded = np.round(spacings / 10.0) * 10.0
        unique, counts = np.unique(rounded, return_counts=True)
        dominant_idx = int(np.argmax(counts))
        dominant_spacing_hz = float(unique[dominant_idx])
        spacing_regularity = float(counts[dominant_idx] / len(spacings))

    peak_density = float(prominent.size / max(band_spectrum.shape[1], 1))
    residual_ratio = float(residual.mean() / (baseline.mean() + 1e-9))
    band_prominence = {}
    for low_hz, high_hz, label in ((0.0, 5000.0, "0_5k"), (5000.0, 10000.0, "5_10k"), (10000.0, high_hz, "10_16k")):
        mask = (freqs >= low_hz) & (freqs < high_hz)
        if np.any(mask):
            sub = spectrum[:, mask].mean(axis=0)
            sub_base = local_minimum_baseline(sub, min(10, max(2, sub.size // 32)))
            band_prominence[label] = round(float(np.maximum(sub - sub_base, 0.0).mean()), 4)
    score = clamp(
        (persistence * 0.40)
        + (spacing_regularity * 0.25)
        + min(0.20, mean_prominence * 0.12)
        + min(0.15, peak_density * 10.0)
        + min(0.10, residual_ratio * 0.6)
    )
    family_hint = "none"
    if score >= 0.62 and spacing_regularity >= 0.45 and prominent.size >= 18:
        family_hint = "encodec_like_upsampler_signature"
    elif score >= 0.48 and prominent.size >= 12:
        family_hint = "unknown_synthetic_family"
    return {
        "score": round(score, 4),
        "peak_count": int(prominent.size),
        "peak_density": round(peak_density, 6),
        "mean_peak_prominence": round(mean_prominence, 4),
        "peak_persistence": round(persistence, 4),
        "spacing_regularity": round(spacing_regularity, 4),
        "dominant_spacing_hz": round(dominant_spacing_hz, 2),
        "residual_ratio": round(residual_ratio, 4),
        "family_hint": family_hint,
        "band_prominence": band_prominence,
        "analysis_band_hz": [5000, int(high_hz)],
    }


def analyze_short_window_profile(samples: np.ndarray, sample_rate: int, window_seconds: int = 5, hop_seconds: int = 1) -> dict[str, Any] | None:
    window = sample_rate * window_seconds
    hop = sample_rate * hop_seconds
    if samples.size < window:
        return None
    scores: list[float] = []
    for start in range(0, samples.size - window + 1, hop):
        lane = analyze_spectral_artifact_lane(samples[start : start + window], sample_rate)
        if lane:
            scores.append(float(lane["score"]))
    if not scores:
        return None
    arr = np.array(scores, dtype=np.float32)
    return {
        "window_seconds": window_seconds,
        "hop_seconds": hop_seconds,
        "window_count": int(arr.size),
        "mean_score": round(float(arr.mean()), 4),
        "max_score": round(float(arr.max()), 4),
        "active_window_ratio": round(float(np.mean(arr >= 0.4)), 4),
    }


def analyze_distributional_lane(samples: np.ndarray, sample_rate: int, duration: int) -> dict[str, Any] | None:
    usable = samples[: min(samples.size, sample_rate * 90)]
    frames = frame_signal(usable, 4096, 1024)
    if frames is None:
        return None
    spectrum = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(4096, d=1.0 / sample_rate)
    energy = spectrum.sum(axis=1) + 1e-9
    centroids = (spectrum * freqs).sum(axis=1) / energy
    norm_centroid = float(np.mean(centroids) / (sample_rate / 2.0))
    spectral_diff = np.diff(np.log1p(spectrum), axis=0)
    flux = np.maximum(spectral_diff, 0.0).sum(axis=1)
    pitch_salience = dominant_autocorr_strength(usable, sample_rate)
    duration_suspicion = 0.0
    if 120 <= duration <= 360:
        duration_suspicion = 0.35
        if duration % 5 == 0:
            duration_suspicion += 0.15
    score = clamp(
        max(0.0, 0.34 - norm_centroid) * 1.7
        + max(0.0, 0.36 - pitch_salience) * 1.2
        + min(0.15, float(np.mean(flux)) * 0.015)
        + min(0.10, duration_suspicion)
    )
    return {
        "score": round(score, 4),
        "spectral_centroid_normalized": round(norm_centroid, 4),
        "spectral_centroid_hz": round(float(np.mean(centroids)), 2),
        "spectral_flux_mean": round(float(np.mean(flux)), 4),
        "pitch_salience_proxy": round(pitch_salience, 4),
        "duration_suspicion": round(duration_suspicion, 4),
    }


def analyze_structure_lane(samples: np.ndarray, sample_rate: int) -> dict[str, Any] | None:
    usable = samples[: min(samples.size, sample_rate * 120)]
    frames = frame_signal(usable, 4096, 1024)
    if frames is None:
        return None
    log_spectrum = compute_log_spectrum(frames)
    novelty = np.maximum(np.diff(log_spectrum, axis=0), 0.0).sum(axis=1)
    if novelty.size < 8:
        return None
    novelty = novelty / (np.max(novelty) + 1e-9)
    threshold = float(novelty.mean() + novelty.std())
    peak_positions = [idx for idx in range(1, novelty.size - 1) if novelty[idx] > threshold and novelty[idx] > novelty[idx - 1] and novelty[idx] > novelty[idx + 1]]
    intervals = np.diff(peak_positions) if len(peak_positions) >= 2 else np.array([], dtype=np.float32)
    section_regularity = 0.0
    microtiming_rigidity = 0.0
    if intervals.size:
        cv = float(np.std(intervals) / (np.mean(intervals) + 1e-9))
        section_regularity = clamp(1.0 - min(cv, 1.0))
        microtiming_rigidity = clamp(1.0 - min(cv * 1.2, 1.0))
    centered = novelty - novelty.mean()
    autocorr = np.correlate(centered, centered, mode="full")[centered.size - 1 :]
    repetition_density = 0.0
    if autocorr.size > 8 and autocorr[0] > 1e-9:
        repetition_density = float(np.max(autocorr[4:]) / autocorr[0])
    rms = np.sqrt(np.mean(frames**2, axis=1))
    tail_window = min(max(2, sample_rate // 1024), rms.size // 3 if rms.size >= 3 else 1)
    tail_realism = 0.0
    if tail_window >= 1 and rms.size >= tail_window * 2:
        before_tail = float(np.mean(rms[-(tail_window * 2) : -tail_window]))
        tail = float(np.mean(rms[-tail_window:]))
        tail_realism = clamp(1.0 - min(1.0, tail / (before_tail + 1e-9)))
    transition_sharpness = clamp(float(np.percentile(novelty, 95) / (np.mean(novelty) + 1e-9)) / 6.0)
    score = clamp(
        (section_regularity * 0.28)
        + (repetition_density * 0.28)
        + (tail_realism * 0.22)
        + (transition_sharpness * 0.12)
        + (microtiming_rigidity * 0.10)
    )
    return {
        "score": round(score, 4),
        "section_regularity": round(section_regularity, 4),
        "repetition_density": round(repetition_density, 4),
        "tail_realism": round(tail_realism, 4),
        "transition_sharpness": round(transition_sharpness, 4),
        "microtiming_rigidity": round(microtiming_rigidity, 4),
        "novelty_peak_count": int(len(peak_positions)),
    }


def analyze_lyrics_lane(transcripts: list[dict[str, Any]], stems: dict[str, str] | None) -> dict[str, Any]:
    text_items = [(item.get("label") or "source", item.get("text") or "") for item in transcripts if item.get("text")]
    if not text_items:
        return {
            "score": 0.0,
            "token_count": 0,
            "lexical_repetition": 0.0,
            "line_repetition": 0.0,
            "bracket_token_ratio": 0.0,
            "source_vocals_overlap": 0.0,
            "speech_feature_score": 0.0,
        }
    all_text = " ".join(text for _, text in text_items)
    tokens = re.findall(r"[a-zA-Z']+", all_text.lower())
    token_count = len(tokens)
    counts = Counter(tokens)
    lexical_repetition = 0.0
    if token_count:
        lexical_repetition = float(1.0 - (len(counts) / token_count))
    lines = [line.strip().lower() for _, text in text_items for line in re.split(r"[\n\r]+", text) if line.strip()]
    line_counts = Counter(lines)
    repeated_lines = sum(count for _, count in line_counts.items() if count > 1)
    line_repetition = float(repeated_lines / max(len(lines), 1))
    repeated_ngram_ratio = 0.0
    if token_count >= 6:
        ngrams = [" ".join(tokens[idx : idx + 3]) for idx in range(len(tokens) - 2)]
        ngram_counts = Counter(ngrams)
        repeated_ngrams = sum(count for _, count in ngram_counts.items() if count > 1)
        repeated_ngram_ratio = float(repeated_ngrams / max(len(ngrams), 1))
    bracket_tokens = re.findall(r"\[[^\]]+\]", all_text)
    bracket_token_ratio = float(len(bracket_tokens) / max(token_count, 1))
    overlap = 0.0
    source_text = " ".join(text for label, text in text_items if label == "source")
    vocals_text = " ".join(text for label, text in text_items if label == "vocals")
    if source_text and vocals_text:
        source_set = set(re.findall(r"[a-zA-Z']+", source_text.lower()))
        vocals_set = set(re.findall(r"[a-zA-Z']+", vocals_text.lower()))
        overlap = float(len(source_set & vocals_set) / max(len(source_set | vocals_set), 1))
    speech_feature_score = 0.0
    if stems and stems.get("vocals"):
        vocals_samples = extract_pcm_mono(Path(stems["vocals"]), 16000)
        if vocals_samples.size:
            speech_feature_score = clamp(float(np.sqrt(np.mean(vocals_samples**2))) * 8.0)
    score = clamp(
        (lexical_repetition * 0.25)
        + (line_repetition * 0.20)
        + (repeated_ngram_ratio * 0.25)
        + min(0.15, bracket_token_ratio * 4.0)
        + max(0.0, 0.55 - overlap) * 0.20
        + (speech_feature_score * 0.05)
    )
    return {
        "score": round(score, 4),
        "token_count": token_count,
        "lexical_repetition": round(lexical_repetition, 4),
        "line_repetition": round(line_repetition, 4),
        "repeated_ngram_ratio": round(repeated_ngram_ratio, 4),
        "bracket_token_ratio": round(bracket_token_ratio, 4),
        "source_vocals_overlap": round(overlap, 4),
        "speech_feature_score": round(speech_feature_score, 4),
    }


def detect_c2pa_provenance(source_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tool_available": command_exists("c2patool"),
        "manifest_hint": False,
        "verified": False,
        "validation_state": None,
        "active_manifest": None,
    }
    if result["tool_available"]:
        try:
            proc = run(["c2patool", str(source_path)])
            output = proc.stdout.strip()
            try:
                data = json.loads(output)
                manifests = data.get("active_manifest") or data.get("manifests") or data
                validation_state = recursive_text_values(data)
                result["manifest_hint"] = True
                result["active_manifest"] = manifests
                result["validation_state"] = next((text for text in validation_state if "valid" in text.lower()), None)
                result["verified"] = bool(result["validation_state"])
                return result
            except json.JSONDecodeError:
                lowered = output.lower()
                result["manifest_hint"] = "c2pa" in lowered or "content credentials" in lowered
                result["verified"] = "valid" in lowered
                result["validation_state"] = "Valid" if result["verified"] else None
                return result
        except Exception:
            pass
    try:
        raw = source_path.read_bytes()[: 1024 * 1024]
        lowered = raw.lower()
        result["manifest_hint"] = b"c2pa" in lowered or b"content credentials" in lowered
    except Exception:
        pass
    return result


def build_identity_lane(scorecard: dict[str, Any], songrec_top: dict[str, Any] | None, acoustid_top: dict[str, Any] | None, musicbrainz_top: dict[str, Any] | None) -> dict[str, Any]:
    score = 0.0
    evidence: list[str] = []
    if songrec_top and scorecard["songrec_hit_count"] >= 2:
        score = 25.0
        evidence.append("Repeated SongRec/Shazam matches")
    elif acoustid_top and acoustid_top.get("recording_id"):
        score = 18.0
        evidence.append("AcoustID recording match")
    else:
        score += min(8.0, scorecard["panako_hit_count"] * 2.0)
        score += min(5.0, scorecard["corpus_match_count"] * 1.5)
        if musicbrainz_top and scorecard["musicbrainz_candidate_count"] > 0:
            score += 4.0
            evidence.append("MusicBrainz candidates")
        if scorecard["raw_acoustid_hit_count"] > 0:
            score += 3.0
            evidence.append("Raw AcoustID hits")
        if scorecard["panako_hit_count"] > 0:
            evidence.append("Panako local matches")
        if scorecard["corpus_match_count"] > 0:
            evidence.append("Local corpus matches")
    return {
        "score": round(min(25.0, score), 2),
        "label": "exact_or_strong" if score >= 18.0 else "weak_or_candidate" if score >= 6.0 else "none",
        "evidence": evidence,
    }


def build_generator_family_lane(provenance: dict[str, Any], spectral_lane: dict[str, Any] | None) -> dict[str, Any]:
    known_signals = [signal for signal in provenance.get("provenance_signals") or [] if signal in {"suno", "udio", "synthid"}]
    if known_signals:
        family = known_signals[0]
        return {
            "score": 10.0,
            "label": "known_family",
            "family": family,
            "evidence": [f"Metadata/provenance mentions {family}"],
        }
    if spectral_lane and spectral_lane.get("family_hint") == "encodec_like_upsampler_signature":
        return {
            "score": 7.5,
            "label": "known_architecture_hint",
            "family": "encodec_like_upsampler_signature",
            "evidence": ["High-frequency artifact spacing resembles an upsampler/deconvolution family"],
        }
    if spectral_lane and spectral_lane.get("family_hint") == "unknown_synthetic_family":
        return {
            "score": 5.0,
            "label": "unknown_synthetic_family",
            "family": "unknown_synthetic_family",
            "evidence": ["Persistent high-frequency artifact family detected without a known label"],
        }
    return {
        "score": 0.0,
        "label": "none",
        "family": None,
        "evidence": [],
    }


def analyze_confidence_lane(
    source_excerpt: Path,
    excerpt_seconds: int,
    scorecard: dict[str, Any],
    spectral_lane: dict[str, Any] | None,
    outdir: Path,
) -> dict[str, Any]:
    multiplier = 1.0 if excerpt_seconds >= 60 else 0.90 if excerpt_seconds > 20 else 0.75
    penalties: list[str] = []
    if excerpt_seconds <= 20:
        penalties.append("short_excerpt")
    elif excerpt_seconds < 60:
        penalties.append("clean_excerpt")
    else:
        penalties.append("clean_full_track")

    perturbation_scores: dict[str, float] = {}
    perturbation_stability = 1.0
    if spectral_lane:
        base_score = spectral_lane.get("score") or 0.0
        perturb_root = outdir / "reports" / "perturbations"
        filters = {
            "lowpass_9k": "lowpass=f=9000",
            "resample_22k": "aresample=22050",
            "pitch_up_1st": "asetrate=32000*1.059463,aresample=32000",
        }
        diffs = []
        for label, filter_chain in filters.items():
            perturbed = render_ffmpeg_filter(source_excerpt, perturb_root / f"{source_excerpt.stem}_{label}.flac", filter_chain, 32000)
            if not perturbed:
                continue
            perturbed_samples = extract_pcm_mono(perturbed, 32000)
            perturbed_lane = analyze_spectral_artifact_lane(perturbed_samples, 32000)
            if not perturbed_lane:
                continue
            perturbation_scores[label] = round(float(perturbed_lane["score"]), 4)
            diffs.append(abs(float(perturbed_lane["score"]) - base_score))
        if diffs:
            perturbation_stability = clamp(1.0 - (float(np.mean(diffs)) / max(base_score, 0.2)))
            if perturbation_stability < 0.55:
                multiplier = min(multiplier, 0.50)
                penalties.append("artifact_fragile_under_perturbation")
            elif perturbation_stability < 0.75:
                multiplier = min(multiplier, 0.75)
                penalties.append("artifact_somewhat_fragile")

    if scorecard["songrec_hit_count"] == 0 and scorecard["acoustid_recording_hit_count"] == 0 and scorecard["transcript_hit_count"] == 0:
        multiplier = min(multiplier, 0.60)
        penalties.append("limited_multi_lane_support")

    confidence_class = "high" if multiplier >= 0.9 else "medium" if multiplier >= 0.72 else "low"
    return {
        "multiplier": round(multiplier, 4),
        "class": confidence_class,
        "penalties": penalties,
        "perturbation_stability": round(perturbation_stability, 4),
        "perturbation_scores": perturbation_scores,
    }


def build_forensic_matrix(
    assets: SourceAssets,
    source_excerpt: Path,
    excerpt_seconds: int,
    ffprobe_data: dict[str, Any],
    provenance: dict[str, Any],
    transcripts: list[dict[str, Any]],
    stems: dict[str, str] | None,
    scorecard: dict[str, Any],
    songrec_top: dict[str, Any] | None,
    acoustid_top: dict[str, Any] | None,
    musicbrainz_top: dict[str, Any] | None,
    outdir: Path,
) -> dict[str, Any] | None:
    samples = extract_pcm_mono(source_excerpt, 32000)
    if samples.size < 32000 * 8:
        return None
    spectral_lane = analyze_spectral_artifact_lane(samples, 32000)
    distribution_lane = analyze_distributional_lane(samples, 32000, assets.duration)
    structure_lane = analyze_structure_lane(samples, 32000)
    short_window_profile = analyze_short_window_profile(samples, 32000)
    lyrics_lane = analyze_lyrics_lane(transcripts, stems)
    c2pa = detect_c2pa_provenance(assets.audio_path)
    provenance_lane = {
        "score": 0.0,
        "label": "neutral",
        "signals": provenance.get("provenance_signals") or [],
        "c2pa": c2pa,
    }
    if c2pa.get("verified"):
        provenance_lane["score"] = 25.0
        provenance_lane["label"] = "verified_provenance"
    elif c2pa.get("manifest_hint") or provenance.get("provenance_signal_count"):
        provenance_lane["score"] = 10.0
        provenance_lane["label"] = "provenance_hints"
    generator_family_lane = build_generator_family_lane(provenance, spectral_lane)
    identity_lane = build_identity_lane(scorecard, songrec_top, acoustid_top, musicbrainz_top)
    confidence_lane = analyze_confidence_lane(source_excerpt, excerpt_seconds, scorecard, spectral_lane, outdir)
    lane_scores = {
        "provenance": round(float(provenance_lane["score"]), 2),
        "spectral_artifacts": round(25.0 * float((spectral_lane or {}).get("score", 0.0)), 2),
        "descriptor_priors": round(8.0 * float((distribution_lane or {}).get("score", 0.0)), 2),
        "structure": round(15.0 * float((structure_lane or {}).get("score", 0.0)), 2),
        "lyrics_speech": round(22.0 * float((lyrics_lane or {}).get("score", 0.0)), 2),
        "generator_family": round(float(generator_family_lane["score"]), 2),
    }
    if short_window_profile and short_window_profile["active_window_ratio"] < 0.2 and lane_scores["spectral_artifacts"] < 12.0:
        confidence_lane["multiplier"] = min(confidence_lane["multiplier"], 0.60)
        confidence_lane["class"] = "low" if confidence_lane["multiplier"] < 0.72 else confidence_lane["class"]
        confidence_lane["penalties"].append("weak_short_window_support")
    lane_confidences = {
        "provenance": 100 if provenance_lane["label"] == "verified_provenance" else 55 if provenance_lane["label"] == "provenance_hints" else 25,
        "spectral_artifacts": int(round(100 * float((spectral_lane or {}).get("peak_persistence", 0.0)))),
        "descriptor_priors": 40,
        "structure": 60 if structure_lane else 20,
        "lyrics_speech": 75 if lyrics_lane.get("token_count") else 20,
        "generator_family": 85 if generator_family_lane["label"] == "known_family" else 60 if generator_family_lane["label"] != "none" else 20,
    }

    raw_score = (
        float(provenance_lane["score"])
        + (25.0 * float((spectral_lane or {}).get("score", 0.0)))
        + (8.0 * float((distribution_lane or {}).get("score", 0.0)))
        + (15.0 * float((structure_lane or {}).get("score", 0.0)))
        + (22.0 * float((lyrics_lane or {}).get("score", 0.0)))
        + float(generator_family_lane["score"])
    )
    strong_lane_count = sum(
        1
        for value in (
            lane_scores["provenance"],
            lane_scores["spectral_artifacts"],
            lane_scores["structure"],
            lane_scores["lyrics_speech"],
            lane_scores["generator_family"],
        )
        if value >= 12.0
    )
    confidence_cap = 100 if provenance_lane["label"] == "verified_provenance" or strong_lane_count >= 3 else 80 if strong_lane_count >= 2 else 55 if strong_lane_count >= 1 else 35
    adjusted_score = clamp((raw_score / 100.0) * confidence_lane["multiplier"]) * 100.0
    if lane_scores["spectral_artifacts"] >= 17.5 and strong_lane_count <= 1:
        adjusted_score = min(adjusted_score, 75.0)
    confidence_score = min(confidence_cap, int(round(confidence_lane["multiplier"] * 100)))
    synthetic_label = (
        "high_confidence_synthetic"
        if adjusted_score >= 80.0
        else "strong_synthetic_evidence"
        if adjusted_score >= 65.0
        else "probable_synthetic_or_ai_mediated"
        if adjusted_score >= 45.0
        else "weak_suspicion_or_inconclusive"
        if adjusted_score >= 25.0
        else "no_meaningful_synthetic_evidence"
    )
    if scorecard["songrec_hit_count"] >= 2 and identity_lane["score"] >= 25.0:
        adjusted_score = min(adjusted_score, 22.0)
        synthetic_label = "low_synthetic_likelihood_due_to_strong_identity_match"

    top_evidence_for = []
    if provenance_lane["label"] != "neutral":
        top_evidence_for.append(f"Provenance lane: {provenance_lane['label']}")
    if spectral_lane and lane_scores["spectral_artifacts"] >= 10.0:
        top_evidence_for.append(
            f"Spectral artifacts: {spectral_lane['peak_count']} persistent high-band peaks, regularity={spectral_lane['spacing_regularity']}"
        )
    if lyrics_lane.get("score", 0.0) >= 0.35:
        top_evidence_for.append(
            f"Lyrics/speech lane: repetition={lyrics_lane['lexical_repetition']}, overlap={lyrics_lane['source_vocals_overlap']}"
        )
    if structure_lane and lane_scores["structure"] >= 7.5:
        top_evidence_for.append(
            f"Structure lane: repetition={structure_lane['repetition_density']}, tail_realism={structure_lane['tail_realism']}"
        )
    if generator_family_lane["label"] != "none":
        top_evidence_for.append(f"Generator family: {generator_family_lane['family']}")

    top_evidence_against = []
    if identity_lane["score"] >= 18.0:
        top_evidence_against.append(f"Strong identity evidence: {identity_lane['evidence'][:2]}")
    if confidence_lane["perturbation_stability"] < 0.6:
        top_evidence_against.append("Artifact lane is fragile under mild perturbations")
    if lane_scores["lyrics_speech"] < 4.0:
        top_evidence_against.append("Lyrics/speech lane is weak or unavailable")
    if lane_scores["structure"] < 4.0:
        top_evidence_against.append("Structure lane is weak on the analyzed excerpt")

    quality_class = "clean_full_track" if excerpt_seconds >= assets.duration - 2 else "clean_excerpt"
    if "artifact_fragile_under_perturbation" in confidence_lane["penalties"]:
        quality_class = "heavily_transcoded"
    elif lyrics_lane.get("speech_feature_score", 0.0) >= 0.3 and lyrics_lane.get("source_vocals_overlap", 1.0) < 0.4:
        quality_class = "masked"
    elif excerpt_seconds < 20:
        quality_class = "clean_excerpt"

    return {
        "identity_score": int(round(identity_lane["score"] * 4.0)),
        "synthetic_score": int(round(adjusted_score)),
        "confidence_score": confidence_score,
        "known_family_score": int(round(min(100.0, generator_family_lane["score"] * 10.0))),
        "family_label": "known" if generator_family_lane["label"] in {"known_family", "known_architecture_hint"} else "unknown" if generator_family_lane["label"] == "unknown_synthetic_family" else "none",
        "quality_class": quality_class,
        "top_evidence_for": top_evidence_for[:5],
        "top_evidence_against": top_evidence_against[:5],
        "lane_scores": lane_scores,
        "lane_confidences": lane_confidences,
        "perturbation_stability": confidence_lane["perturbation_stability"],
        "notes": confidence_lane["penalties"],
        "analysis_excerpt_path": str(source_excerpt),
        "analysis_excerpt_seconds": excerpt_seconds,
        "identity_lane": identity_lane,
        "provenance_lane": provenance_lane,
        "spectral_artifact_lane": spectral_lane,
        "distributional_acoustics_lane": distribution_lane,
        "structural_lane": structure_lane,
        "short_window_profile": short_window_profile,
        "lyrics_speech_lane": lyrics_lane,
        "generator_family_lane": generator_family_lane,
        "confidence_lane": confidence_lane,
        "synthetic_likelihood": {
            "raw_score": round(raw_score, 2),
            "adjusted_score": round(adjusted_score, 2),
            "label": synthetic_label,
        },
    }


def shell_join_command(template: str, replacements: dict[str, str]) -> list[str]:
    rendered = template.format(**replacements)
    return ["bash", "-lc", rendered]


def run_external_engine(template: str, replacements: dict[str, str]) -> dict[str, Any]:
    proc = run(shell_join_command(template, replacements))
    return {
        "command": template,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def run_shell_capture(command: str, cwd: Path | None = None) -> dict[str, Any]:
    proc = run(["bash", "-lc", command], cwd=cwd)
    return {
        "command": command,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def locate_panako_jar(panako_root: str) -> Path | None:
    libs_dir = Path(panako_root).resolve() / "build" / "libs"
    if not libs_dir.exists():
        return None
    jars = sorted(libs_dir.glob("*.jar"))
    return jars[-1] if jars else None


def panako_base_command(jar_path: Path, reports_dir: Path, strategy: str) -> list[str]:
    panako_dir = reports_dir / "panako"
    ensure_dir(panako_dir)
    return [
        "java",
        "--add-opens",
        "java.base/java.nio=ALL-UNNAMED",
        f"-Djava.util.prefs.userRoot={str(panako_dir / 'prefs')}",
        "-jar",
        str(jar_path),
        f"STRATEGY={strategy}",
        f"PANAKO_LMDB_FOLDER={str(panako_dir / 'panako_db')}",
        f"PANAKO_CACHE_FOLDER={str(panako_dir / 'panako_cache')}",
        f"OLAF_LMDB_FOLDER={str(panako_dir / 'olaf_db')}",
        f"OLAF_CACHE_FOLDER={str(panako_dir / 'olaf_cache')}",
    ]


def panako_store_source(jar_path: Path, reports_dir: Path, strategy: str, source_audio: Path) -> dict[str, Any]:
    cmd = panako_base_command(jar_path, reports_dir, strategy) + ["store", str(source_audio)]
    proc = run(cmd)
    return {
        "command": shlex.join(cmd),
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def parse_panako_results(stdout: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or not re.match(r"^\d+\s*;\s*\d+\s*;", line):
            continue
        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 13:
            continue
        try:
            results.append(
                {
                    "index": int(parts[0]),
                    "total": int(parts[1]),
                    "query_path": parts[2],
                    "query_start_seconds": float(parts[3]),
                    "query_stop_seconds": float(parts[4]),
                    "match_path": parts[5],
                    "match_id": parts[6],
                    "match_start_seconds": float(parts[7]),
                    "match_stop_seconds": float(parts[8]),
                    "match_score": float(parts[9]),
                    "time_factor": parts[10],
                    "frequency_factor": parts[11],
                    "seconds_with_match_ratio": float(parts[12]),
                }
            )
        except ValueError:
            continue
    return results


def panako_query_clip(jar_path: Path, reports_dir: Path, strategy: str, clip_path: Path) -> dict[str, Any]:
    cmd = panako_base_command(jar_path, reports_dir, strategy) + ["query", str(clip_path)]
    proc = run(cmd)
    results = parse_panako_results(proc.stdout)
    return {
        "command": shlex.join(cmd),
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "results": results,
        "top_result": results[0] if results else None,
    }


def score_ai_audio_artifacts(audio_path: Path, sample_rate: int = 32000) -> dict[str, Any] | None:
    samples = extract_pcm_mono(audio_path, sample_rate)
    spectral_lane = analyze_spectral_artifact_lane(samples, sample_rate)
    if not spectral_lane:
        return None
    score = float(spectral_lane["score"])
    label = "high" if score >= 0.65 else "medium" if score >= 0.4 else "low"
    return {
        "artifact_score": round(score, 4),
        "artifact_label": label,
        "peak_count": spectral_lane["peak_count"],
        "peak_density": spectral_lane["peak_density"],
        "periodicity_strength": spectral_lane["spacing_regularity"],
        "dominant_spacing_hz": spectral_lane["dominant_spacing_hz"],
        "residual_ratio": spectral_lane["residual_ratio"],
        "mean_peak_prominence": spectral_lane["mean_peak_prominence"],
        "peak_persistence": spectral_lane["peak_persistence"],
        "family_hint": spectral_lane["family_hint"],
        "sample_rate": sample_rate,
        "analysis_seconds": round(min(samples.size, sample_rate * 60) / sample_rate, 2),
    }


def recursive_text_values(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        texts.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            texts.extend(recursive_text_values(item))
    elif isinstance(value, list):
        for item in value:
            texts.extend(recursive_text_values(item))
    return texts


def scan_provenance_signals(metadata: dict[str, Any], ffprobe_data: dict[str, Any]) -> dict[str, Any]:
    token_patterns = {
        "c2pa": re.compile(r"\bc2pa\b"),
        "content credentials": re.compile(r"\bcontent credentials\b"),
        "contentcredentials": re.compile(r"\bcontentcredentials\b"),
        "synthid": re.compile(r"\bsynthid\b"),
        "generated with ai": re.compile(r"\bgenerated with ai\b"),
        "ai-generated": re.compile(r"\bai-generated\b"),
        "ai generated": re.compile(r"\bai generated\b"),
        "suno": re.compile(r"\bsuno\b"),
        "udio": re.compile(r"\budio\b"),
    }
    texts = [text.lower() for text in recursive_text_values(metadata) + recursive_text_values(ffprobe_data)]
    matched = sorted({label for label, pattern in token_patterns.items() for text in texts if pattern.search(text)})
    return {
        "provenance_signal_count": len(matched),
        "provenance_signals": matched,
    }


def build_focus_starts(duration: int, comment_timestamps: list[int], profiles: list[tuple[int, int]]) -> set[tuple[int, int, int]]:
    focused: set[tuple[int, int, int]] = set()
    offsets = (-20, 0, 20)
    for clip_length, step in profiles:
        for ts in comment_timestamps:
            for offset in offsets:
                start = max(0, min(duration - clip_length, ts + offset))
                if start < 0:
                    continue
                focused.add((clip_length, step, start))
    return focused


def choose_demucs_excerpt_start(comment_timestamps: list[int], duration: int, excerpt_seconds: int) -> int:
    if comment_timestamps:
        target = max(0, comment_timestamps[0] - min(20, excerpt_seconds // 4))
        return min(target, max(0, duration - excerpt_seconds))
    return 0


def prepare_analysis_excerpt(
    source_path: Path,
    outdir: Path,
    prefix: str,
    duration: int,
    comment_timestamps: list[int],
    excerpt_seconds: int,
) -> tuple[Path, int]:
    if excerpt_seconds <= 0 or duration <= excerpt_seconds:
        return source_path, 0
    excerpt_root = outdir / "reports" / "excerpts"
    ensure_dir(excerpt_root)
    excerpt_start = choose_demucs_excerpt_start(comment_timestamps, duration, excerpt_seconds)
    excerpt_path = excerpt_root / f"{prefix}_{excerpt_start}_{excerpt_seconds}.flac"
    if not excerpt_path.exists():
        extract_clip(source_path, excerpt_start, excerpt_seconds, excerpt_path)
    return excerpt_path, excerpt_start


def separate_stems_demucs(
    source_path: Path,
    outdir: Path,
    demucs_root: str,
    demucs_python: str,
    duration: int,
    comment_timestamps: list[int],
    excerpt_seconds: int,
) -> dict[str, str] | None:
    stems_root = outdir / "reports" / "stems"
    ensure_dir(stems_root)
    excerpt_path, _ = prepare_analysis_excerpt(
        source_path,
        outdir,
        "demucs_excerpt",
        duration,
        comment_timestamps,
        excerpt_seconds,
    )
    demucs_repo = Path(demucs_root).resolve()
    if command_exists("demucs"):
        run(["demucs", "--two-stems=vocals", "-o", str(stems_root), str(excerpt_path)], capture=False)
    elif demucs_repo.exists():
        python_bin = Path(demucs_python) if demucs_python else Path(sys.executable)
        if not python_bin.exists():
            python_bin = Path(sys.executable)
        cmd = (
            f"PYTHONPATH={shlex.quote(str(demucs_repo))}:$PYTHONPATH "
            f"{shlex.quote(str(python_bin))} -m demucs.separate --two-stems=vocals -o {shlex.quote(str(stems_root))} {shlex.quote(str(excerpt_path))}"
        )
        run(["bash", "-lc", cmd], capture=False)
    else:
        return None
    stem_dir = stems_root / "htdemucs" / excerpt_path.stem
    if not stem_dir.exists():
        return None
    stems = {}
    for name in ("vocals.wav", "no_vocals.wav", "drums.wav", "bass.wav", "other.wav"):
        path = stem_dir / name
        if path.exists():
            stems[name.removesuffix(".wav")] = str(path)
    return stems or None


def whisper_transcribe(audio_path: Path, outdir: Path, model: str, language: str | None) -> dict[str, Any] | None:
    if not command_exists("whisper"):
        return None
    transcript_dir = outdir / "reports" / "transcripts"
    ensure_dir(transcript_dir)
    cmd = [
        "whisper",
        str(audio_path),
        "--model",
        model,
        "--output_dir",
        str(transcript_dir),
        "--output_format",
        "json",
        "--fp16",
        "False",
    ]
    if language:
        cmd.extend(["--language", language])
    run(cmd, capture=False)
    json_path = transcript_dir / f"{audio_path.stem}.json"
    if not json_path.exists():
        return None
    data = parse_json_file(json_path)
    text = (data.get("text") or "").strip()
    segments = data.get("segments") or []
    return {
        "text": text,
        "segment_count": len(segments),
        "language": data.get("language"),
        "json_path": str(json_path),
    }


def extract_search_phrases(text: str, limit: int) -> list[str]:
    phrases = []
    for raw in re.split(r"[\n\r]+", text):
        cleaned = " ".join(raw.split()).strip()
        if len(cleaned.split()) < 4:
            continue
        if cleaned not in phrases:
            phrases.append(cleaned[:120])
        if len(phrases) >= limit:
            break
    if not phrases and text.strip():
        words = text.strip().split()
        if len(words) >= 4:
            phrases.append(" ".join(words[: min(len(words), 10)]))
    return phrases[:limit]


def musicbrainz_recording_search(phrase: str, limit: int = 5) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"query": phrase, "fmt": "json", "limit": str(limit)})
    url = f"https://musicbrainz.org/ws/2/recording?{query}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ytdlpchopid/0.1 (https://example.invalid/contact)"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    results = []
    for recording in data.get("recordings") or []:
        results.append(
            {
                "id": recording.get("id"),
                "title": recording.get("title"),
                "artist_credit": ", ".join(
                    item.get("name", "")
                    for item in recording.get("artist-credit") or []
                    if item.get("name")
                ),
                "score": recording.get("score"),
                "disambiguation": recording.get("disambiguation"),
            }
        )
    return results


def top_acoustid_match(data: dict[str, Any]) -> dict[str, Any] | None:
    results = data.get("results") or []
    if not results:
        return None
    results = sorted(results, key=lambda item: item.get("score") or 0, reverse=True)
    best = results[0]
    recordings = best.get("recordings") or []
    rec = recordings[0] if recordings else {}
    return {
        "score": best.get("score"),
        "acoustid_id": best.get("id"),
        "artist": ", ".join(artist.get("name", "") for artist in rec.get("artists", []) if artist.get("name")),
        "title": rec.get("title"),
        "recording_id": rec.get("id"),
        "releasegroups": [rg.get("title") for rg in rec.get("releasegroups", []) if rg.get("title")],
    }


def summarize_songrec(data: dict[str, Any]) -> dict[str, Any] | None:
    track = data.get("track") or {}
    if not track:
        return None
    return {
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "isrc": track.get("isrc"),
        "shazam_key": track.get("key"),
        "shazam_url": track.get("url"),
        "match_count": len(data.get("matches") or []),
        "album": next(
            (
                item.get("text")
                for section in track.get("sections") or []
                for item in section.get("metadata") or []
                if item.get("title") == "Album"
            ),
            None,
        ),
    }


def aggregate_songrec(clips: list[dict[str, Any]]) -> dict[str, Any] | None:
    grouped: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {}
    for clip in clips:
        summary = clip.get("songrec_summary")
        if not summary:
            continue
        key = (summary.get("title"), summary.get("artist"), summary.get("isrc"))
        bucket = grouped.setdefault(
            key,
            {
                "title": summary.get("title"),
                "artist": summary.get("artist"),
                "isrc": summary.get("isrc"),
                "album": summary.get("album"),
                "shazam_key": summary.get("shazam_key"),
                "shazam_url": summary.get("shazam_url"),
                "clips": 0,
                "max_match_count": 0,
                "starts": [],
            },
        )
        bucket["clips"] += 1
        bucket["max_match_count"] = max(bucket["max_match_count"], summary.get("match_count") or 0)
        bucket["starts"].append(clip["start_seconds"])
    if not grouped:
        return None
    ranked = sorted(grouped.values(), key=lambda item: (item["clips"], item["max_match_count"]), reverse=True)
    return ranked[0]


def aggregate_acoustid(clips: list[dict[str, Any]]) -> dict[str, Any] | None:
    grouped: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {}
    raw_only_hits = 0
    for clip in clips:
        summary = clip.get("acoustid")
        if not summary:
            continue
        if summary.get("recording_id"):
            key = (summary.get("title"), summary.get("artist"), summary.get("recording_id"))
            bucket = grouped.setdefault(
                key,
                {
                    "title": summary.get("title"),
                    "artist": summary.get("artist"),
                    "recording_id": summary.get("recording_id"),
                    "clips": 0,
                    "best_score": 0.0,
                    "starts": [],
                },
            )
            bucket["clips"] += 1
            bucket["best_score"] = max(bucket["best_score"], summary.get("score") or 0.0)
            bucket["starts"].append(clip["start_seconds"])
        elif summary.get("acoustid_id"):
            raw_only_hits += 1
    if grouped:
        ranked = sorted(grouped.values(), key=lambda item: (item["clips"], item["best_score"]), reverse=True)
        return ranked[0]
    if raw_only_hits:
        return {"raw_only_hits": raw_only_hits}
    return None


def aggregate_musicbrainz(clips: list[dict[str, Any]], transcripts: list[dict[str, Any]]) -> dict[str, Any] | None:
    grouped: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for clip in clips:
        for candidate in clip.get("musicbrainz_candidates") or []:
            key = (candidate.get("title"), candidate.get("artist_credit"))
            bucket = grouped.setdefault(
                key,
                {
                    "title": candidate.get("title"),
                    "artist_credit": candidate.get("artist_credit"),
                    "best_score": 0,
                    "sources": 0,
                },
            )
            bucket["best_score"] = max(bucket["best_score"], candidate.get("score") or 0)
            bucket["sources"] += 1
    for transcript in transcripts:
        for candidate in transcript.get("musicbrainz_candidates") or []:
            key = (candidate.get("title"), candidate.get("artist_credit"))
            bucket = grouped.setdefault(
                key,
                {
                    "title": candidate.get("title"),
                    "artist_credit": candidate.get("artist_credit"),
                    "best_score": 0,
                    "sources": 0,
                },
            )
            bucket["best_score"] = max(bucket["best_score"], candidate.get("score") or 0)
            bucket["sources"] += 1
    if not grouped:
        return None
    ranked = sorted(grouped.values(), key=lambda item: (item["sources"], item["best_score"]), reverse=True)
    return ranked[0]


def build_scorecard(
    source_type: str,
    metadata: dict[str, Any],
    comments_findings: list[dict[str, Any]],
    clips: list[dict[str, Any]],
    ocr_records: list[dict[str, Any]],
    provenance: dict[str, Any],
    transcripts: list[dict[str, Any]],
) -> dict[str, Any]:
    rich_metadata_keys = [key for key in ("track", "artist", "album", "chapters") if metadata.get(key)]
    songrec_hits = [clip for clip in clips if clip.get("songrec_summary")]
    songrec_keys = {
        (
            clip["songrec_summary"].get("title"),
            clip["songrec_summary"].get("artist"),
            clip["songrec_summary"].get("isrc"),
        )
        for clip in songrec_hits
    }
    acoustid_hits = [clip for clip in clips if clip.get("acoustid")]
    acoustid_recording_hits = [clip for clip in acoustid_hits if clip["acoustid"].get("recording_id")]
    raw_acoustid_hits = [clip for clip in acoustid_hits if clip["acoustid"].get("acoustid_id")]
    lowered_comments = [(finding.get("text") or "").lower() for finding in comments_findings]
    playlist_request_count = sum(
        1
        for text in lowered_comments
        if any(token in text for token in ("playlist", "songs used", "what is the song", "song at minute"))
    )
    ai_comment_mentions = sum(1 for text in lowered_comments if "ai" in text)
    timestamp_comment_count = sum(1 for text in lowered_comments if parse_timestamp(text) is not None)
    corpus_match_count = sum(1 for clip in clips if clip.get("corpus_matches"))
    musicbrainz_candidate_count = sum(len(clip.get("musicbrainz_candidates") or []) for clip in clips)
    musicbrainz_candidate_count += sum(len(item.get("musicbrainz_candidates") or []) for item in transcripts)
    transcript_hit_count = sum(1 for item in transcripts if item.get("text"))
    panako_hit_count = sum(1 for clip in clips if (clip.get("panako") or {}).get("top_result"))
    ai_scores = [clip["ai_audio_heuristics"]["artifact_score"] for clip in clips if clip.get("ai_audio_heuristics")]
    high_ai_clips = [
        clip for clip in clips if (clip.get("ai_audio_heuristics") or {}).get("artifact_label") == "high"
    ]
    return {
        "source_type": source_type,
        "analysis_audio_source": metadata.get("analysis_audio_source"),
        "spotify_track_id_present": bool(metadata.get("spotify_track_id")),
        "spotify_preview_url_present": bool(metadata.get("preview_url")),
        "matched_youtube_candidate_present": bool(metadata.get("matched_youtube_candidate")),
        "youtube_candidate_count": len(metadata.get("youtube_candidates") or []),
        "embedded_metadata_present": bool(rich_metadata_keys),
        "embedded_metadata_keys": rich_metadata_keys,
        "comment_signal_count": len(comments_findings),
        "playlist_request_count": playlist_request_count,
        "ai_comment_mentions": ai_comment_mentions,
        "timestamp_comment_count": timestamp_comment_count,
        "songrec_hit_count": len(songrec_hits),
        "songrec_distinct_match_count": len(songrec_keys),
        "acoustid_hit_count": len(acoustid_hits),
        "acoustid_recording_hit_count": len(acoustid_recording_hits),
        "raw_acoustid_hit_count": len(raw_acoustid_hits),
        "panako_hit_count": panako_hit_count,
        "corpus_match_count": corpus_match_count,
        "transcript_hit_count": transcript_hit_count,
        "musicbrainz_candidate_count": musicbrainz_candidate_count,
        "provenance_signal_count": provenance["provenance_signal_count"],
        "provenance_signals": provenance["provenance_signals"],
        "ai_artifact_clip_count": len(ai_scores),
        "high_ai_artifact_clip_count": len(high_ai_clips),
        "mean_ai_artifact_score": round(sum(ai_scores) / len(ai_scores), 4) if ai_scores else 0.0,
        "max_ai_artifact_score": round(max(ai_scores), 4) if ai_scores else 0.0,
        "ocr_hit_count": len(ocr_records),
        "clip_count": len(clips),
    }


def classify_source(
    scorecard: dict[str, Any],
    songrec_top: dict[str, Any] | None,
    acoustid_top: dict[str, Any] | None,
    musicbrainz_top: dict[str, Any] | None,
) -> dict[str, Any]:
    has_metadata_or_audio_corroboration = (
        scorecard["embedded_metadata_present"]
        or scorecard["raw_acoustid_hit_count"] > 0
        or scorecard["songrec_hit_count"] > 0
    )

    if songrec_top and scorecard["songrec_hit_count"] >= 2:
        return {
            "label": "recognized_cataloged_track",
            "confidence": "high",
            "reason": "Repeated SongRec/Shazam matches across multiple windows.",
            "best_match": songrec_top,
        }

    if acoustid_top and scorecard["acoustid_recording_hit_count"] > 0 and acoustid_top.get("recording_id"):
        return {
            "label": "recognized_cataloged_track",
            "confidence": "medium",
            "reason": "AcoustID/MusicBrainz recording matches were found.",
            "best_match": acoustid_top,
        }

    if (
        musicbrainz_top
        and scorecard["musicbrainz_candidate_count"] > 0
        and has_metadata_or_audio_corroboration
    ):
        return {
            "label": "candidate_match_found",
            "confidence": "low",
            "reason": "MusicBrainz recording candidates were found, but only weak metadata or partial audio evidence supports them.",
            "best_match": musicbrainz_top,
        }

    if (
        not songrec_top
        and not scorecard["embedded_metadata_present"]
        and scorecard["playlist_request_count"] > 0
        and scorecard["ai_comment_mentions"] > 0
        and (
            scorecard["mean_ai_artifact_score"] >= 0.4
            or scorecard["high_ai_artifact_clip_count"] > 0
            or scorecard["provenance_signal_count"] > 0
        )
    ):
        return {
            "label": "likely_ai_or_channel_original",
            "confidence": "high" if scorecard["high_ai_artifact_clip_count"] >= 2 or scorecard["provenance_signal_count"] > 0 else "medium",
            "reason": "No recognizer hits, no embedded track metadata, comments request a tracklist, comments mention AI, and heuristic AI/provenance signals are present.",
            "best_match": None,
        }

    if (
        not songrec_top
        and not scorecard["embedded_metadata_present"]
        and (
            scorecard["playlist_request_count"] > 0
            or scorecard["raw_acoustid_hit_count"] > 0
        )
    ):
        return {
            "label": "likely_uncataloged_or_original",
            "confidence": "medium",
            "reason": "No recognized cataloged track, but there are weak public-database or comment signals suggesting a real source without published IDs.",
            "best_match": None,
        }

    return {
        "label": "needs_manual_review",
        "confidence": "low",
        "reason": "Signals are mixed or too weak to classify automatically.",
        "best_match": songrec_top or acoustid_top,
    }


def classify_synthetic(forensic_matrix: dict[str, Any] | None) -> dict[str, Any]:
    if not forensic_matrix:
        return {
            "label": "insufficient_evidence",
            "confidence": "low",
            "reason": "No forensic matrix could be computed from the available audio.",
        }
    synthetic_score = forensic_matrix["synthetic_score"]
    confidence_score = forensic_matrix["confidence_score"]
    label = (
        "strong"
        if synthetic_score >= 60
        else "probable"
        if synthetic_score >= 40
        else "inconclusive"
        if synthetic_score >= 20
        else "low"
    )
    confidence = "high" if confidence_score >= 70 else "medium" if confidence_score >= 35 else "low"
    reason = "; ".join(forensic_matrix.get("top_evidence_for")[:3]) or "No synthetic lane accumulated strong evidence."
    return {
        "label": label,
        "confidence": confidence,
        "reason": reason,
    }


def identify(args: argparse.Namespace) -> int:
    outdir = Path(args.outdir).resolve()
    ensure_dir(outdir)
    ensure_dir(outdir / "clips")
    ensure_dir(outdir / "fingerprints")
    ensure_dir(outdir / "reports")

    if SPOTIFY_TRACK_RE.search(args.source):
        assets = fetch_spotify_assets(
            args.source,
            outdir,
            args.spotify_youtube_search,
            args.ocr,
            args.comments,
            args.max_comments,
        )
    elif args.source.startswith("http://") or args.source.startswith("https://"):
        assets = fetch_youtube_assets(args.source, outdir, args.ocr, args.comments, args.max_comments)
    else:
        assets = local_file_assets(args.source, outdir)

    comments_findings: list[dict[str, Any]] = []
    comment_timestamps: list[int] = []
    comments_json = outdir / "comments" / f"{assets.metadata.get('id', '')}.info.json"
    if args.comments and comments_json.exists():
        comments_findings, comment_timestamps = extract_comments_findings(comments_json)

    full_fp = None
    full_fp_path = None
    transcripts: list[dict[str, Any]] = []
    source_stems: dict[str, str] | None = None
    if args.full_source_fingerprint:
        duration, fingerprint, fp_text = fpcalc_full(assets.audio_path, args.algorithm)
        full_fp_path = outdir / "reports" / "source.full.fp.txt"
        full_fp_path.write_text(fp_text, encoding="utf-8")
        full_fp = {"duration": duration, "fingerprint": fingerprint, "path": str(full_fp_path)}
        if args.corpus_dir and args.corpus_add_source:
            corpus_dir = Path(args.corpus_dir).resolve()
            ensure_dir(corpus_dir)
            corpus_fp = corpus_dir / f"{sanitize_name(args.corpus_add_source)}.fp.txt"
            corpus_meta = corpus_dir / f"{sanitize_name(args.corpus_add_source)}.meta.json"
            corpus_fp.write_text(fp_text, encoding="utf-8")
            corpus_meta.write_text(
                json.dumps(
                    {
                        "label": args.corpus_add_source,
                        "source": assets.source,
                        "title": assets.title,
                    },
                    ensure_ascii=True,
                    indent=2,
                ),
                encoding="utf-8",
            )

    if args.demucs:
        source_stems = separate_stems_demucs(
            assets.audio_path,
            outdir,
            args.demucs_root,
            args.demucs_python,
            assets.duration,
            comment_timestamps,
            args.demucs_excerpt_seconds,
        )

    if args.whisper:
        whisper_source_audio, whisper_excerpt_start = prepare_analysis_excerpt(
            assets.audio_path,
            outdir,
            "whisper_excerpt",
            assets.duration,
            comment_timestamps,
            args.whisper_excerpt_seconds,
        )
        whisper_sources: list[tuple[str, Path]] = [("source", whisper_source_audio)]
        if source_stems and source_stems.get("vocals"):
            whisper_sources.insert(0, ("vocals", Path(source_stems["vocals"])))
        for label, audio_path in whisper_sources:
            transcript = whisper_transcribe(audio_path, outdir, args.whisper_model, args.whisper_language)
            if transcript:
                transcript["label"] = label
                transcript["audio_path"] = str(audio_path)
                if label == "source" and audio_path != assets.audio_path:
                    transcript["excerpt_start_seconds"] = whisper_excerpt_start
                    transcript["excerpt_duration_seconds"] = min(args.whisper_excerpt_seconds, assets.duration)
                if args.musicbrainz:
                    mb_candidates: list[dict[str, Any]] = []
                    for phrase in extract_search_phrases(transcript["text"], args.musicbrainz_phrase_limit):
                        try:
                            mb_candidates.extend(musicbrainz_recording_search(phrase, args.musicbrainz_limit))
                        except Exception as exc:
                            mb_candidates.append({"error": str(exc), "phrase": phrase})
                            break
                    transcript["musicbrainz_candidates"] = mb_candidates[: args.musicbrainz_limit]
                transcripts.append(transcript)

    matrix_excerpt, matrix_excerpt_start = prepare_analysis_excerpt(
        assets.audio_path,
        outdir,
        "forensic_excerpt",
        assets.duration,
        comment_timestamps,
        args.forensic_excerpt_seconds,
    )

    panako_jar: Path | None = None
    panako_store_info: dict[str, Any] | None = None
    if args.panako:
        panako_jar = locate_panako_jar(args.panako_root)
        if panako_jar:
            panako_store_info = panako_store_source(
                panako_jar,
                outdir / "reports",
                args.panako_strategy,
                assets.audio_path,
            )

    profiles = parse_profiles(args.profiles)
    focus_starts = build_focus_starts(assets.duration, comment_timestamps, profiles) if args.focus_comments else set()
    clip_records = []
    seen_starts = set()
    for clip_length, step in profiles:
        count = 0
        starts = list(range(0, assets.duration, step))
        if args.focus_comments:
            starts = sorted(set(starts + [start for pl, st, start in focus_starts if pl == clip_length and st == step]))
        for start in starts:
            if args.max_clips_per_profile and count >= args.max_clips_per_profile:
                break
            duration = min(clip_length, assets.duration - start)
            if duration < 15:
                break
            key = (clip_length, step, start)
            if key in seen_starts:
                continue
            seen_starts.add(key)
            count += 1
            clip_name = f"l{clip_length}_s{step}__{format_hms(start).replace(':', '-')}_{duration}s.flac"
            clip_path = outdir / "clips" / clip_name
            extract_clip(assets.audio_path, start, duration, clip_path)
            fp_duration, fingerprint, fp_text = fpcalc(clip_path, duration, args.algorithm)
            fp_path = outdir / "fingerprints" / f"{clip_name[:-5]}.fp.txt"
            fp_path.write_text(fp_text, encoding="utf-8")

            record: dict[str, Any] = {
                "profile": f"{clip_length}:{step}",
                "start_seconds": start,
                "duration": duration,
                "clip_path": str(clip_path),
                "fingerprint_path": str(fp_path),
            }

            if args.acoustid and os.environ.get("ACOUSTID_API_KEY"):
                acoustid_json = outdir / "reports" / "acoustid" / f"{clip_name[:-5]}.json"
                data = acoustid_lookup(os.environ["ACOUSTID_API_KEY"], fp_duration, fingerprint, acoustid_json)
                record["acoustid"] = top_acoustid_match(data)
                record["acoustid_json"] = str(acoustid_json)

            if args.songrec and command_exists("songrec"):
                songrec_data = songrec_lookup(clip_path)
                record["songrec"] = songrec_data
                record["songrec_summary"] = summarize_songrec(songrec_data)

            if args.ai_heuristics:
                record["ai_audio_heuristics"] = score_ai_audio_artifacts(clip_path)

            if panako_jar:
                record["panako"] = panako_query_clip(
                    panako_jar,
                    outdir / "reports",
                    args.panako_strategy,
                    clip_path,
                )

            if args.audfprint and Path(args.audfprint_root, "audfprint.py").exists():
                db_path = outdir / "reports" / "audfprint-db.pklz"
                script_path = Path(args.audfprint_root) / "audfprint.py"
                if not db_path.exists():
                    run_shell_capture(
                        f"python3 {shlex.quote(str(script_path))} new -d {shlex.quote(str(db_path))} {shlex.quote(str(assets.audio_path))}"
                    )
                record["audfprint"] = run_shell_capture(
                    f"python3 {shlex.quote(str(script_path))} match -d {shlex.quote(str(db_path))} {shlex.quote(str(clip_path))}"
                )

            if args.musicbrainz and args.whisper and transcripts:
                mb_candidates = []
                for transcript in transcripts:
                    for phrase in extract_search_phrases(transcript.get("text", ""), 1):
                        try:
                            mb_candidates.extend(musicbrainz_recording_search(phrase, args.musicbrainz_limit))
                        except Exception as exc:
                            mb_candidates.append({"error": str(exc), "phrase": phrase})
                            break
                if mb_candidates:
                    record["musicbrainz_candidates"] = mb_candidates[: args.musicbrainz_limit]

            if args.corpus_dir:
                corpus_dir = Path(args.corpus_dir).resolve()
                if corpus_dir.exists():
                    record["corpus_matches"] = corpus_rerank(fp_path, corpus_dir, args.corpus_top)

            clip_records.append(record)

    ocr_records = []
    if args.ocr and assets.video_path and command_exists("tesseract"):
        timestamps = sorted(set(comment_timestamps + collect_evenly_spaced_timestamps(assets.duration, args.ocr_samples)))
        for ts in timestamps[: args.ocr_limit]:
            image_path = outdir / "reports" / "frames" / f"{format_hms(ts).replace(':', '-')}.jpg"
            try:
                text = ocr_frame(assets.video_path, ts, image_path)
            except subprocess.CalledProcessError:
                continue
            if text:
                ocr_records.append({"timestamp": ts, "image_path": str(image_path), "text": text})

    ffprobe_data = ffprobe_json(assets.audio_path)
    provenance = scan_provenance_signals(assets.metadata, ffprobe_data)
    summary = {
        "source": assets.source,
        "source_type": assets.source_type,
        "title": assets.title,
        "duration": assets.duration,
        "metadata": assets.metadata,
        "ffprobe": ffprobe_data,
        "provenance": provenance,
        "panako_store": panako_store_info,
        "stems": source_stems,
        "transcripts": transcripts,
        "comments_findings": comments_findings,
        "comment_timestamps": comment_timestamps,
        "full_source_fingerprint": full_fp,
        "clips": clip_records,
        "ocr": ocr_records,
    }
    songrec_top = aggregate_songrec(clip_records)
    acoustid_top = aggregate_acoustid(clip_records)
    musicbrainz_top = aggregate_musicbrainz(clip_records, transcripts)
    summary["scorecard"] = build_scorecard(
        assets.source_type,
        assets.metadata,
        comments_findings,
        clip_records,
        ocr_records,
        provenance,
        transcripts,
    )
    forensic_matrix = None
    if args.forensic_matrix or args.ai_heuristics:
        forensic_matrix = build_forensic_matrix(
            assets,
            matrix_excerpt,
            min(args.forensic_excerpt_seconds, assets.duration) if matrix_excerpt != assets.audio_path else assets.duration,
            ffprobe_data,
            provenance,
            transcripts,
            source_stems,
            summary["scorecard"],
            songrec_top,
            acoustid_top,
            musicbrainz_top,
            outdir,
        )
        if forensic_matrix:
            forensic_matrix["analysis_excerpt_start_seconds"] = matrix_excerpt_start
            summary.update(
                {
                    "identity_score": forensic_matrix["identity_score"],
                    "synthetic_score": forensic_matrix["synthetic_score"],
                    "confidence_score": forensic_matrix["confidence_score"],
                    "known_family_score": forensic_matrix["known_family_score"],
                    "family_label": forensic_matrix["family_label"],
                    "quality_class": forensic_matrix["quality_class"],
                    "top_evidence_for": forensic_matrix["top_evidence_for"],
                    "top_evidence_against": forensic_matrix["top_evidence_against"],
                    "lane_scores": forensic_matrix["lane_scores"],
                    "lane_confidences": forensic_matrix["lane_confidences"],
                    "perturbation_stability": forensic_matrix["perturbation_stability"],
                    "notes": forensic_matrix["notes"],
                }
            )
    summary["forensic_matrix"] = forensic_matrix
    summary["identity_assessment"] = classify_source(summary["scorecard"], songrec_top, acoustid_top, musicbrainz_top)
    summary["synthetic_assessment"] = classify_synthetic(forensic_matrix)
    summary["assessment"] = summary["identity_assessment"]

    report_json = outdir / "reports" / "summary.json"
    report_json.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")

    lines = [
        f"Source: {assets.source}",
        f"Source Type: {assets.source_type}",
        f"Title: {assets.title}",
        f"Duration: {assets.duration}s",
        f"Identity: {summary['identity_assessment']['label']} ({summary['identity_assessment']['confidence']})",
        f"Identity Reason: {summary['identity_assessment']['reason']}",
        f"Synthetic Likelihood: {summary['synthetic_assessment']['label']} ({summary['synthetic_assessment']['confidence']})",
        f"Synthetic Reason: {summary['synthetic_assessment']['reason']}",
    ]
    if assets.metadata.get("spotify_track_id"):
        lines.append(f"Spotify Track ID: {assets.metadata.get('spotify_track_id')}")
    if assets.metadata.get("analysis_audio_source"):
        lines.append(f"Analysis Audio Source: {assets.metadata.get('analysis_audio_source')}")
    if assets.metadata.get("matched_youtube_candidate"):
        candidate = assets.metadata["matched_youtube_candidate"]
        lines.append(
            "Matched YouTube Candidate: "
            f"{candidate.get('title')} [{candidate.get('id')}] by {candidate.get('channel') or candidate.get('uploader')}"
        )
    if summary["identity_assessment"].get("best_match"):
        lines.append(f"Best Match: {summary['identity_assessment']['best_match']}")
    if forensic_matrix:
        lines.extend(
            [
                f"Identity Score: {forensic_matrix['identity_score']}",
                f"Synthetic Score: {forensic_matrix['synthetic_score']}",
                f"Confidence Score: {forensic_matrix['confidence_score']}",
                f"Known Family Score: {forensic_matrix['known_family_score']}",
                f"Family Label: {forensic_matrix['family_label']}",
                f"Quality Class: {forensic_matrix['quality_class']}",
                f"Perturbation Stability: {forensic_matrix['perturbation_stability']}",
                "Top Evidence For:",
            ]
        )
        for item in forensic_matrix.get("top_evidence_for") or []:
            lines.append(f"- {item}")
        lines.append("Top Evidence Against:")
        for item in forensic_matrix.get("top_evidence_against") or []:
            lines.append(f"- {item}")
        lines.append("Lane Scores:")
        for key, value in forensic_matrix.get("lane_scores", {}).items():
            lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "Scorecard:",
        ]
    )
    for key, value in summary["scorecard"].items():
        lines.append(f"- {key}: {value}")
    if transcripts:
        lines.extend(["", "Transcripts:"])
        for item in transcripts:
            lines.append(f"- {item.get('label')}: {item.get('text','')[:240]}")
    lines.extend(
        [
            "",
            "Comments Findings:",
        ]
    )
    for finding in comments_findings[:20]:
        lines.append(f"- {finding.get('author')}: {finding.get('text')}")
    lines.append("")
    lines.append("Clip Findings:")
    for record in clip_records:
        parts = [f"- {record['profile']} @ {format_hms(record['start_seconds'])}"]
        acoustid_data = record.get("acoustid")
        if acoustid_data:
            parts.append(f"AcoustID={acoustid_data}")
        songrec_summary = record.get("songrec_summary")
        if songrec_summary:
            parts.append(f"SongRec={songrec_summary}")
        ai_audio_heuristics = record.get("ai_audio_heuristics")
        if ai_audio_heuristics:
            parts.append(f"AIHeuristics={ai_audio_heuristics}")
        if record.get("panako"):
            panako_top = (record["panako"] or {}).get("top_result")
            if panako_top:
                parts.append(f"Panako={panako_top}")
            else:
                parts.append(f"Panako={record['panako'].get('stdout','')[:160]}")
        if record.get("audfprint"):
            parts.append(f"Audfprint={record['audfprint'].get('stdout','')[:160]}")
        if record.get("musicbrainz_candidates"):
            parts.append(f"MusicBrainz={record['musicbrainz_candidates'][0]}")
        corpus_matches = record.get("corpus_matches") or []
        if corpus_matches:
            parts.append(f"Corpus top={corpus_matches[0]}")
        lines.append(" | ".join(parts))
    if ocr_records:
        lines.append("")
        lines.append("OCR Findings:")
        for item in ocr_records:
            lines.append(f"- {format_hms(item['timestamp'])}: {item['text']}")

    report_md = outdir / "reports" / "summary.md"
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_json))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ytdlpchopid")
    subparsers = parser.add_subparsers(dest="command", required=True)

    identify_parser = subparsers.add_parser("identify", help="Run multi-strategy identification on a URL or local file")
    identify_parser.add_argument("source")
    identify_parser.add_argument("-o", "--outdir", default="out/app-run")
    identify_parser.add_argument("--spotify-youtube-search", type=int, default=5)
    identify_parser.add_argument("--profiles", default="90:45,60:30,45:15")
    identify_parser.add_argument("--max-clips-per-profile", type=int, default=12)
    identify_parser.add_argument("--algorithm", type=int, default=2)
    identify_parser.add_argument("--comments", action="store_true")
    identify_parser.add_argument("--max-comments", type=int, default=100)
    identify_parser.add_argument("--ocr", action="store_true")
    identify_parser.add_argument("--ocr-samples", type=int, default=8)
    identify_parser.add_argument("--ocr-limit", type=int, default=16)
    identify_parser.add_argument("--acoustid", action="store_true")
    identify_parser.add_argument("--songrec", action="store_true")
    identify_parser.add_argument("--ai-heuristics", action="store_true")
    identify_parser.add_argument("--demucs", action="store_true")
    identify_parser.add_argument("--demucs-root", default="external/demucs")
    identify_parser.add_argument("--demucs-python", default=".venv-demucs/bin/python3")
    identify_parser.add_argument("--demucs-excerpt-seconds", type=int, default=180)
    identify_parser.add_argument("--whisper", action="store_true")
    identify_parser.add_argument("--whisper-model", default="base")
    identify_parser.add_argument("--whisper-language")
    identify_parser.add_argument("--whisper-excerpt-seconds", type=int, default=180)
    identify_parser.add_argument("--musicbrainz", action="store_true")
    identify_parser.add_argument("--musicbrainz-limit", type=int, default=5)
    identify_parser.add_argument("--musicbrainz-phrase-limit", type=int, default=2)
    identify_parser.add_argument("--panako", action="store_true")
    identify_parser.add_argument("--panako-root", default="external/Panako")
    identify_parser.add_argument("--panako-strategy", default="panako")
    identify_parser.add_argument("--audfprint", action="store_true")
    identify_parser.add_argument("--audfprint-root", default="external/audfprint")
    identify_parser.add_argument("--full-source-fingerprint", action="store_true")
    identify_parser.add_argument("--focus-comments", action="store_true")
    identify_parser.add_argument("--forensic-matrix", action="store_true")
    identify_parser.add_argument("--forensic-excerpt-seconds", type=int, default=60)
    identify_parser.add_argument("--corpus-dir")
    identify_parser.add_argument("--corpus-top", type=int, default=5)
    identify_parser.add_argument("--corpus-add-source")
    identify_parser.set_defaults(func=identify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
