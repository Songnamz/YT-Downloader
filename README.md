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

## Other sites

The **Other sites** menu takes a link to any page yt-dlp can read: more than
1,800 sites ([full list](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)),
such as X/Twitter, Dailymotion, TikTok, Facebook, Instagram and Reddit. Many other
pages with a video player work too, as do direct `.mp4`/`.m3u8` links. The
server works out which video is on the page; quality, format and playlist
options work the same way as for YouTube.

The shortcut works for these too. Type your site in front of the full address,
including `https://`:

```
yt.example.com/https://www.dailymotion.com/video/x8j6s5k
```

The bookmarklet works on any page. Videos behind a login or protected with DRM
(Netflix, Disney+, Spotify...) can't be downloaded.

The server refuses links to private addresses (localhost, your LAN, cloud
metadata), so the app can't be used to reach machines on your network. Set
`ALLOW_ANY_SITE=0` to allow YouTube links only.

> **Why not `ytyoutube.com`?** That trick only works if you own the domain
> `ytyoutube.com`, and it was registered by someone else in 2016. If you ever
> buy a look-alike domain (e.g. `ytyoutube.net`) and point it at this app, paths
> like `/watch?v=...` already work on it. Note that domains containing
> "youtube" can get trademark complaints, which is why the default setup uses
> your own domain instead.

## Converter

The **Converter** menu converts files from your own device:

- **MP4 → MP3**: keeps only the sound (best MP3 quality, tags kept).
- **MP3 → MP4**: a video of the song's cover picture (or a black screen if it
  has none) with the sound, e.g. for apps that only accept videos.

MOV/MKV/WEBM videos also become MP3, and M4A/WAV/FLAC/OGG audio becomes MP4.
That's the **Auto** choice; set **Convert to** to **MP3** or **WAV** to get just
the sound of any file in that format (WAV is 16-bit PCM, RF64 past 4 GB).
Any other file type is accepted too (AVI, WMV, FLV, 3GP, TS, AMR, …, even no
extension): after the upload the server looks inside it and makes an MP3 if it
has a video picture, or an MP4 if it is sound only.

Pick or drop as many files as you like: they're converted one after another,
and each is saved as soon as it's ready. Turn off "Save each file as soon as
it's converted" and press **Download all as zip** to get them in a single
.zip instead (streamed as it's built, so it needs no extra disk space). While
the page is open it keeps the converted files on the server; they're deleted
`JOB_TTL_MINUTES` after it's closed.

There is no size limit. The browser uploads the file in 32 MB pieces, so
Cloudflare's 100 MB request limit doesn't apply, and a piece that fails
because of a connection problem is sent again. The only limit is free disk space:
an upload is refused if it would leave less than `MIN_FREE_GB` free. An
upload that stops sending for 30 minutes (for example, because the tab was closed) is deleted. Converted
files are deleted after `JOB_TTL_MINUTES`, like downloads.

## Login

The app asks for a password before anything else (the shortcut and bookmarklet
continue to the download after you log in). You stay logged in for 30 days
(`LOGIN_DAYS`). After 5 wrong tries an IP has to wait 15 minutes.

The password lives in a `.env` file next to `docker-compose.yml`, which is
never committed:

```bash
cp .env.example .env          # then edit APP_PASSWORD
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
docker compose up -d          # apply
```

Changing the password logs everyone out. Leave `APP_PASSWORD` empty to turn
the login off.

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
| `MAX_CONCURRENT` | 2 | downloads and conversions running at once (others wait in a queue) |
| `JOB_TTL_MINUTES` | 60 | finished files are deleted after this |
| `MIN_FREE_GB` | 5 | keep this much disk free: oldest finished files are deleted early, new downloads refused if still short |
| `MAX_HEIGHT` | 2160 | resolution cap |
| `CONCURRENT_FRAGMENTS` | 8 | parallel connections per download (1 = off) |
| `PLAYLIST_PARALLEL` | 3 | playlist videos downloaded at the same time |
| `AUTO_UPDATE` | 1 | update yt-dlp on container start |
| `ALLOW_ANY_SITE` | 1 | accept links from sites other than YouTube (0 = YouTube only) |

### "Sign in to confirm you're not a bot"

YouTube is stricter with server IPs. If downloads fail with that error,
export your YouTube cookies as `cookies.txt` (Netscape format, e.g. with the
"Get cookies.txt LOCALLY" browser extension), put it at
`~/apps/yt-downloader-web/data/cookies.txt`, and restart the container.
Use a spare Google account for this, since the cookies give access to it.

## Note

For personal use with content you have the right to download. YouTube's
terms of service restrict downloading outside its own offline feature.
