# YT Downloader (web)

Web version of the YT Downloader desktop app: a Flask front-end for
[yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg. The server downloads the
video, then your browser saves it (playlists come as one .zip).

## Shortcut from YouTube

On any YouTube page, put your site in front of the link and press Enter:

```
yt.example.com/https://www.youtube.com/watch?v=6QZVNjpkyLY
```

The app opens and starts downloading with your saved Quality/Format settings.
These forms all work: `/https://www.youtube.com/...`, `/youtube.com/...`,
`/youtu.be/ID`, `/watch?v=ID`, `/shorts/ID`, `/playlist?list=...`.

There's also a **bookmarklet** on the page: drag it to the bookmarks bar, then
click it while watching a video.

> **Why not `ytyoutube.com`?** That trick only works if you own the domain
> `ytyoutube.com`, and it was registered by someone else in 2016. If you ever
> buy a look-alike domain (e.g. `ytyoutube.net`) and point it at this app, paths
> like `/watch?v=...` already work on it. Note that domains containing
> "youtube" can get trademark complaints, which is why the default setup uses
> your own domain instead.

## Run locally (macOS)

```bash
brew install yt-dlp ffmpeg deno
python3 -m venv .venv && source .venv/bin/activate
pip install flask
python app.py            # http://localhost:8000
```

## Deploy on the server (Docker + Cloudflare Tunnel)

1. Copy the project to the server (from the Mac):
   ```bash
   rsync -av --exclude .venv --exclude jobs ~/Documents/YT-DownloaderWebApp/ \
     songnam@<server>:~/apps/yt-downloader-web/
   ```
2. On the server:
   ```bash
   cd ~/apps/yt-downloader-web
   docker compose up -d --build
   docker ps        # ytdownloader -> 127.0.0.1:8088, becomes (healthy)
   curl -s localhost:8088/healthz   # ok
   ```
3. Cloudflare dashboard → **Zero Trust → Networks → Tunnels** → your tunnel →
   **Public hostname → Add**: subdomain `yt`, your domain, service
   `HTTP` → `localhost:8088`.
   (If your tunnel uses a `config.yml` instead, add
   `- hostname: yt.example.com` / `service: http://localhost:8088` above the
   catch-all rule and restart cloudflared.)
4. **Recommended:** protect it with **Zero Trust → Access → Applications**
   (allow only your email). Otherwise anyone who finds the URL can use your
   server's bandwidth and IP. The shortcut still works after you've logged in once.

### Updating

- yt-dlp updates itself every time the container starts:
  `docker compose restart ytdownloader`.
- After code changes: copy again, then `docker compose up -d --build`.

### Settings (`docker-compose.yml`)

| Variable | Default | Meaning |
|---|---|---|
| `MAX_CONCURRENT` | 2 | downloads running at once (others wait in a queue) |
| `JOB_TTL_MINUTES` | 60 | finished files are deleted after this |
| `MAX_HEIGHT` | 2160 | resolution cap |
| `AUTO_UPDATE` | 1 | update yt-dlp on container start |

### "Sign in to confirm you're not a bot"

YouTube is stricter with server IPs. If downloads fail with that error,
export your YouTube cookies as `cookies.txt` (Netscape format, e.g. with the
"Get cookies.txt LOCALLY" browser extension), put it at
`~/apps/yt-downloader-web/data/cookies.txt`, and restart the container.
Use a spare Google account for this, since the cookies give access to it.

## Note

For personal use with content you have the right to download. YouTube's
terms of service restrict downloading outside its own offline feature.
