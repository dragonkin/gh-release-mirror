# gh-release-mirror

A small, dependency-free, self-hosted service that mirrors **GitHub Release assets** to your own server, so users on slow or unreliable networks can download builds at full speed behind a stable URL.

```
GitHub Releases  ──sync──▶  your VPS cache  ──HTTP──▶  fast, stable download
```

## Why

Many projects ship installers and binaries through GitHub Releases. From some networks those downloads are slow, rate-limited, or intermittently blocked. A tiny always-on box that mirrors the assets removes that dependency without changing the upstream project.

## Features

- Sync the latest (or all) release of one or more repositories via the GitHub API
- Cache assets on disk with **SHA256** sidecars for integrity checks
- Serve over HTTP with **Range** support and `Content-Disposition`
- Simple HTML index and a JSON API for listings
- **Zero third-party dependencies** — Python 3.9+ standard library only

## Quick start

```bash
git clone https://github.com/dragonkin/gh-release-mirror.git
cd gh-release-mirror
cp config.example.json config.json      # edit repos / port
python mirror.py sync --config config.json
python mirror.py serve --config config.json
# open http://localhost:8080
```

Or run both together:

```bash
python mirror.py sync-and-serve --config config.json
```

## Configuration

`config.json`:

```json
{
  "repos": ["dragonkin/gh-release-mirror"],
  "cache_dir": "./cache",
  "host": "0.0.0.0",
  "port": 8080,
  "github_token": null,
  "include_prereleases": false,
  "base_url": null
}
```

| key | meaning |
|---|---|
| `repos` | list of `owner/repo` to mirror (required) |
| `cache_dir` | where assets are stored (default `./cache`) |
| `host` / `port` | HTTP bind address (default `0.0.0.0:8080`) |
| `github_token` | optional token, raises API rate limits |
| `include_prereleases` | mirror the newest release even if pre-release |
| `base_url` | optional public prefix used for links in the index |

## HTTP API

| route | description |
|---|---|
| `GET /` | HTML index of mirrored assets |
| `GET /api/releases` | JSON manifests of all synced releases |
| `GET /files/<owner__repo>/<asset>` | download an asset (Range supported) |
| `GET /healthz` | `ok` |

## Deploy

See [`docs/DEPLOY.md`](docs/DEPLOY.md) for a systemd unit and an nginx reverse-proxy example.

## Roadmap

- [ ] Webhook / cron auto-sync on new upstream releases
- [ ] Per-repo retention policy (keep last N releases)
- [ ] Optional S3-compatible backend
- [ ] Prometheus metrics endpoint

## Infrastructure

Release artifacts for this project are mirrored and served with the help of
[BlueServers](https://blueservers.com), who provide a free VPS to non-commercial
open-source projects as part of their [Social Support Program](https://blueservers.com/free-dedicated-server).

## License

MIT © dragonkin — see [LICENSE](LICENSE).
