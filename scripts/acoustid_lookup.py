#!/usr/bin/env python3
import argparse
import gzip
import json
import sys
import urllib.parse
import urllib.request


def lookup_http(api_key: str, duration: int, fingerprint: str) -> dict:
    payload = urllib.parse.urlencode(
        [
            ("client", api_key),
            ("duration", str(duration)),
            ("fingerprint", fingerprint),
            ("meta", "recordings+recordingids+releasegroups+releasegroupids+releases+tracks+sources"),
            ("format", "json"),
        ]
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.acoustid.org/v2/lookup",
        data=gzip.compress(payload),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Encoding": "gzip",
            "Accept-Encoding": "gzip",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
      data = resp.read()
      if resp.headers.get("Content-Encoding") == "gzip":
          data = gzip.decompress(data)
      return json.loads(data.decode("utf-8"))


def lookup_pyacoustid(api_key: str, duration: int, fingerprint: str) -> dict:
    import acoustid  # type: ignore

    return acoustid.lookup(
        api_key,
        fingerprint,
        duration,
        meta=[
            "recordings",
            "recordingids",
            "releasegroups",
            "releasegroupids",
            "releases",
            "tracks",
            "sources",
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--duration", required=True, type=int)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--backend", choices=["auto", "pyacoustid", "http"], default="auto")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.backend == "pyacoustid":
        data = lookup_pyacoustid(args.api_key, args.duration, args.fingerprint)
    elif args.backend == "http":
        data = lookup_http(args.api_key, args.duration, args.fingerprint)
    else:
        try:
            import acoustid  # type: ignore  # noqa: F401
            data = lookup_pyacoustid(args.api_key, args.duration, args.fingerprint)
        except ImportError:
            data = lookup_http(args.api_key, args.duration, args.fingerprint)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
