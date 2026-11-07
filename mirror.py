#!/usr/bin/env python3
"""
gh-release-mirror
=================
A small, dependency-free, self-hosted service that mirrors GitHub release
assets to your own server so users in regions with slow or unreliable
access to GitHub can download builds at full speed.

Why this exists
---------------
Release artifacts (installers, binaries, models) are often hosted on GitHub
Releases, which can be very slow or intermittently unreachable from some
networks. A small always-on box that mirrors those assets behind a stable
URL makes distribution reliable without changing the upstream project.

Features
--------
* Sync the latest (or all) release of one or more repositories via GitHub API
* Cache assets on disk with SHA256 sidecars for integrity checks
* Serve over HTTP with Range support and Content-Disposition
* Simple HTML index and a JSON API for listings
* Zero third-party dependencies (Python 3.9+ standard library only)

Usage
-----
  python mirror.py sync            --config config.json
  python mirror.py serve           --config config.json
  python mirror.py sync-and-serve  --config config.json

Author : dragonkin
License: MIT
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

__version__ = "1.0.0"
USER_AGENT = f"gh-release-mirror/{__version__}"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_config(path: str) -> dict:
    """Load and validate a JSON config file, filling sensible defaults."""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    if not isinstance(cfg.get("repos"), list) or not cfg["repos"]:
        raise ValueError("config.repos must be a non-empty list of 'owner/repo'")

    cfg.setdefault("cache_dir", "./cache")
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("port", 8080)
    cfg.setdefault("github_token", None)
    cfg.setdefault("include_prereleases", False)
    cfg.setdefault("base_url", None)  # optional public URL prefix for index links

    for repo in cfg["repos"]:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
            raise ValueError(f"invalid repo spec: {repo!r} (expected 'owner/repo')")
    return cfg


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
def _opener(token: str | None):
    handlers = []
    if token:
        handlers.append(_AuthHeader(token))
    return urllib.request.build_opener(*handlers)


class _AuthHeader(urllib.request.BaseHandler):
    """Attach a bearer token to outbound GitHub requests."""

    def __init__(self, token: str):
        self.token = token

    def http_request(self, req):
        req.add_header("Authorization", f"Bearer {self.token}")
        return req

    https_request = http_request


def gh_json(url: str, token: str | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with _opener(token).open(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_release(repo: str, token: str | None, include_prereleases: bool) -> dict:
    base = f"https://api.github.com/repos/{repo}/releases"
    if include_prereleases:
        releases = gh_json(base, token)
        if not releases:
            raise RuntimeError(f"no releases for {repo}")
        return releases[0]
    return gh_json(f"{base}/latest", token)


def sha256_of(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    name = os.path.basename(name)
    return re.sub(r"[^A-Za-z0-9._+-]", "_", name) or "asset"


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def sync_repo(repo: str, cfg: dict, verbose: bool = True) -> list[dict]:
    """Download all assets of the newest release of *repo* into the cache."""
    owner, name = repo.split("/", 1)
    rel = latest_release(repo, cfg.get("github_token"), cfg.get("include_prereleases", False))
    dest_dir = os.path.join(cfg["cache_dir"], f"{owner}__{name}")
    os.makedirs(dest_dir, exist_ok=True)

    assets = []
    for asset in rel.get("assets", []):
        fname = _safe_name(asset["name"])
        target = os.path.join(dest_dir, fname)
        size = int(asset.get("size", 0))
        url = asset["browser_download_url"]

        if os.path.exists(target) and os.path.getsize(target) == size:
            digest = sha256_of(target)
            if verbose:
                print(f"  = cached {repo}/{fname} ({size} bytes)")
            assets.append({"repo": repo, "name": fname, "size": size,
                           "sha256": digest, "cached": True})
            continue

        if verbose:
            print(f"  ↓ {repo}/{fname} ({size} bytes)")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        tmp = target + ".part"
        with _opener(cfg.get("github_token")).open(req, timeout=300) as resp, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out, length=1 << 20)
        os.replace(tmp, target)

        digest = sha256_of(target)
        with open(target + ".sha256", "w", encoding="utf-8") as fh:
            fh.write(f"{digest}  {fname}\n")
        assets.append({"repo": repo, "name": fname, "size": size,
                       "sha256": digest, "cached": False})

    manifest = {"repo": repo, "tag": rel.get("tag_name"),
                "published": rel.get("published_at"), "assets": assets,
                "synced_at": int(time.time())}
    with open(os.path.join(dest_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    return assets


def sync_all(cfg: dict) -> None:
    print(f"[sync] {len(cfg['repos'])} repo(s) -> {cfg['cache_dir']}")
    for repo in cfg["repos"]:
        try:
            sync_repo(repo, cfg)
        except Exception as exc:  # noqa: BLE001 - keep going across repos
            print(f"  ! {repo}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# HTTP service
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = f"gh-release-mirror/{__version__}"
    cfg: dict = {}

    # -- helpers ---------------------------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=300")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _index(self) -> bytes:
        rows = []
        root = self.cfg["cache_dir"]
        base = self.cfg.get("base_url") or ""
        if os.path.isdir(root):
            for d in sorted(os.listdir(root)):
                mpath = os.path.join(root, d, "manifest.json")
                if not os.path.exists(mpath):
                    continue
                with open(mpath, encoding="utf-8") as fh:
                    man = json.load(fh)
                for a in man["assets"]:
                    href = f"{base}/files/{d}/{urllib.parse.quote(a['name'])}"
                    rows.append(
                        f"<tr><td>{html.escape(man['repo'])}</td>"
                        f"<td><a href='{href}'>{html.escape(a['name'])}</a></td>"
                        f"<td>{a['size']}</td>"
                        f"<td><code>{a['sha256'][:16]}…</code></td></tr>")
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>gh-release-mirror</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;margin:40px;color:#1b1b1b}}
table{{border-collapse:collapse;width:100%;max-width:900px}}
th,td{{text-align:left;padding:8px 12px;border-bottom:1px solid #e3e3e3}}
th{{background:#f6f8fa}}code{{background:#f2f2f2;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>gh-release-mirror</h1>
<p>Mirrored release assets. <a href="/api/releases">JSON API</a>.</p>
<table><thead><tr><th>Repo</th><th>Asset</th><th>Size</th><th>SHA256</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=4>No assets synced yet.</td></tr>'}</tbody>
</table></body></html>"""
        return body.encode("utf-8")

    def _api(self) -> bytes:
        out = []
        root = self.cfg["cache_dir"]
        if os.path.isdir(root):
            for d in sorted(os.listdir(root)):
                mpath = os.path.join(root, d, "manifest.json")
                if os.path.exists(mpath):
                    with open(mpath, encoding="utf-8") as fh:
                        out.append(json.load(fh))
        return json.dumps(out, indent=2, ensure_ascii=False).encode("utf-8")

    # -- routes ----------------------------------------------------------- #
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):  # noqa: N802 - http.server API
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self._send(200, self._index(), "text/html; charset=utf-8")
        if path == "/api/releases":
            return self._send(200, self._api(), "application/json; charset=utf-8")
        if path.startswith("/files/"):
            return self._serve_file(path[len("/files/"):])
        if path == "/healthz":
            return self._send(200, b"ok\n", "text/plain; charset=utf-8")
        return self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def _serve_file(self, rel: str):
        # prevent path traversal
        parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
        if len(parts) != 2:
            return self._send(400, b"bad path\n", "text/plain; charset=utf-8")
        d, name = parts
        full = os.path.join(self.cfg["cache_dir"], d, name)
        if not os.path.isfile(full):
            return self._send(404, b"file not found\n", "text/plain; charset=utf-8")

        size = os.path.getsize(full)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-", 1)
                start = int(a) if a else 0
                end = int(b) if b else size - 1
            except ValueError:
                start, end = 0, size - 1

        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("[http] %s\n" % (fmt % args))


def serve(cfg: dict) -> None:
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    print(f"[serve] http://{cfg['host']}:{cfg['port']}  (cache: {cfg['cache_dir']})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] stopped")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gh-release-mirror",
                                 description="Mirror GitHub release assets to your server.")
    ap.add_argument("command", choices=["sync", "serve", "sync-and-serve", "version"])
    ap.add_argument("--config", default="config.json", help="path to config.json")
    args = ap.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0

    cfg = load_config(args.config)

    if args.command == "sync":
        sync_all(cfg)
    elif args.command == "serve":
        serve(cfg)
    else:
        threading.Thread(target=sync_all, args=(cfg,), daemon=True).start()
        serve(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
