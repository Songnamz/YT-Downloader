"""YT Downloader (web) - a Flask front-end for yt-dlp + ffmpeg.

The server downloads with yt-dlp into a per-job folder, the browser polls
for progress, then fetches the finished file (or a zip for playlists).

"yt prefix" shortcuts - any of these open the app and start downloading:
  https://<this-host>/https://www.youtube.com/watch?v=ID   (prepend host)
  https://<this-host>/watch?v=ID                            (YouTube-shaped path)
  https://<this-host>/https://www.dailymotion.com/video/ID  (any site yt-dlp supports)

Converter: the browser uploads a local file of any type and size in chunks (so no single request
hits a proxy's size limit), ffmpeg converts it (video -> MP3, audio -> MP4)
and the browser fetches the result like a finished download.
"""

import errno
import hashlib
import hmac
import ipaddress
import json
import secrets
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from datetime import timedelta

from flask import (Flask, Response, abort, jsonify, redirect, request, send_file,
                   send_from_directory, session)

BASE = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("JOBS_DIR", BASE / "jobs"))
COOKIES = os.environ.get("COOKIES_FILE", "/data/cookies.txt")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
JOB_TTL = int(os.environ.get("JOB_TTL_MINUTES", "60")) * 60
# Keep at least this much disk free: old finished files are deleted early
# to make room, and new downloads are refused if that is not enough.
MIN_FREE = float(os.environ.get("MIN_FREE_GB", "5")) * 1024 ** 3
DISK_FULL_MSG = ("The server is out of disk space. Download your finished "
                 "files, then try again in a few minutes.")
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "2160"))
# Parallel fragment downloads (HLS/DASH). Same file, same quality, just faster.
FRAGMENTS = max(1, int(os.environ.get("CONCURRENT_FRAGMENTS", "8")))
# Playlist videos downloaded at the same time (per playlist job)
PLAYLIST_PARALLEL = max(1, int(os.environ.get("PLAYLIST_PARALLEL", "3")))
# Allow links from any site yt-dlp supports, not only YouTube
ANY_SITE = os.environ.get("ALLOW_ANY_SITE", "1") == "1"
# Login: empty password = no login page (e.g. local development)
PASSWORD = os.environ.get("APP_PASSWORD", "")
LOGIN_DAYS = int(os.environ.get("LOGIN_DAYS", "30"))
MAX_FAILS = 5                 # wrong passwords allowed per IP...
FAIL_WINDOW = 15 * 60         # ...within this many seconds

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
            "music.youtube.com", "youtu.be", "www.youtube-nocookie.com"}
QUALITIES = {"best": None, "2160": 2160, "1440": 1440, "1080": 1080,
             "720": 720, "480": 480}
FORMATS = {"mp4", "mkv", "mp3"}

PROGRESS_TEMPLATE = (
    "download:[PROG]%(progress._percent_str)s|"
    "%(progress._speed_str)s|%(progress._eta_str)s"
)
PROGRESS_RE = re.compile(r"\[PROG\]\s*([\d.]+)%\|\s*(.*?)\s*\|\s*(.*)")
DEST_RE = re.compile(r"^\[download\] Destination: (.+)$")
MERGE_RE = re.compile(r'^\[Merger\] Merging formats into "(.+)"$')
SPEED_RE = re.compile(r"([\d.]+)\s*([KMGT]?i?B)/s")
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}

# "/https://www.youtube.com/watch?v=..." (slashes may be collapsed by proxies)
PREFIX_RE = re.compile(
    r"^/(?:https?:/+)?((?:www\.|m\.|music\.)?youtube\.com|youtu\.be)(/.*)?$", re.I)
# YouTube-shaped paths, for a host like "ytyoutube.<tld>" pointed at this app
YT_PATH_RE = re.compile(r"^/(watch|playlist|shorts/|live/|embed/|v/)", re.I)
# "/https://any.site/path" - other sites need the scheme, so app paths like
# /favicon.ico are never mistaken for a link
ANY_PREFIX_RE = re.compile(r"^/(https?):/+([^/?#]+\.[^/?#]+)(/.*)?$", re.I)

app = Flask(__name__, static_folder=None)
app.url_map.merge_slashes = False
# Without a fixed SECRET_KEY every restart logs everyone out
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=LOGIN_DAYS))

jobs: dict[str, "Job"] = {}
jobs_lock = threading.Lock()
slots = threading.Semaphore(MAX_CONCURRENT)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean_url(raw: str) -> str | None:
    """Return a normalised http(s) URL (https for YouTube), or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        return None
    host = parts.hostname or ""
    if "." not in host and ":" not in host:    # needs a real domain or IP
        return None
    if is_youtube(raw):
        parts = parts._replace(scheme="https")
    return parts.geturl()


def is_youtube(url: str) -> bool:
    return urlsplit(url).hostname in YT_HOSTS


def is_public_host(host: str) -> bool:
    """True if every address the host resolves to is on the public internet.
    Stops the server being used to reach localhost, the LAN or cloud metadata."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return bool(infos) and all(
        ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


PLAYLIST_PARAMS = {"list", "index", "start_radio", "pp"}


def strip_playlist(url: str) -> str | None:
    """Drop playlist parameters so only the single video is fetched.
    Returns None if nothing but a playlist is left (no single video to get)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in PLAYLIST_PARAMS]
    if parts.path.lower().rstrip("/") == "/playlist" or (
            parts.path.lower().rstrip("/") == "/watch" and not any(k == "v" for k, _ in query)):
        return None
    return parts._replace(query=urlencode(query)).geturl()


def parse_speed(text: str) -> float:
    """'  5.00MiB/s' -> bytes per second (0 if unknown)."""
    m = SPEED_RE.search(text or "")
    return float(m.group(1)) * UNITS.get(m.group(2), 0) if m else 0.0


def format_speed(bps: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if bps < 1024 or unit == "GiB":
            return f"{bps:.2f}{unit}/s"
        bps /= 1024
    return ""


def cookie_args(dest: Path) -> list[str]:
    if COOKIES and os.path.isfile(COOKIES):
        # Copy so yt-dlp's cookie writes never touch the mounted original
        shutil.copyfile(COOKIES, dest)
        return ["--cookies", str(dest)]
    return []


def build_command(url, quality, container, outdir, template, cookies) -> list[str]:
    """yt-dlp command for one single video."""
    cmd = [
        "yt-dlp", "--newline", "--color", "never",
        "--progress-template", PROGRESS_TEMPLATE,
        "--no-playlist",
        "--concurrent-fragments", str(FRAGMENTS),
        "-P", str(outdir), "-o", template,
    ]
    if container == "mp3":
        cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        height = min(quality or MAX_HEIGHT, MAX_HEIGHT)
        sort = f"res:{height}"
        if container == "mp4":
            sort += ",ext:mp4:m4a"
        cmd += ["-f", "bv*+ba/b", "-S", sort, "--merge-output-format", container]
    cmd += cookies + ["--", url]
    return cmd


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, url, quality, container, playlist):
        self.id = uuid.uuid4().hex
        self.url = url
        self.quality = quality
        self.container = container
        self.playlist = playlist
        self.dir = JOBS_DIR / self.id
        self.out = self.dir / "out"
        self.created = time.time()
        self.finished: float | None = None   # set when the job stops for any reason
        self.procs: set[subprocess.Popen] = set()
        self.lock = threading.Lock()
        self.cancelled = False
        self.state = {
            "status": "queued",   # queued|running|processing|done|error|cancelled
            "stage": "Waiting for a free slot…",
            "percent": 0.0, "speed": "", "eta": "",
            "file": "", "item": "", "error": "", "log": [],
        }
        self.result: Path | None = None

    def update(self, **kw):
        self.state.update(kw)

    def log(self, line):
        with self.lock:
            self.state["log"] = (self.state["log"] + [line])[-60:]

    def public(self):
        s = dict(self.state)
        s["id"] = self.id
        s["download_name"] = self.result.name if self.result else ""
        return s

    # -- process handling ---------------------------------------------------
    def spawn(self, cmd) -> subprocess.Popen:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, start_new_session=True)
        with self.lock:
            self.procs.add(proc)
        if self.cancelled:       # cancel() ran while we were starting it
            kill(proc)
        return proc

    def reap(self, proc) -> int:
        code = proc.wait()
        with self.lock:
            self.procs.discard(proc)
        return code

    def run_ytdlp(self, cmd, on_progress, tag=""):
        """Run one yt-dlp process; returns (exit code, last error line)."""
        proc = self.spawn(cmd)
        last_error = ""
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if m := PROGRESS_RE.search(line):
                on_progress(float(m.group(1)), m.group(2), m.group(3))
                continue
            if line.startswith("[PROG]"):
                continue
            self.log(tag + line)
            if line.startswith("ERROR:"):
                last_error = line[6:].strip()
            elif not self.playlist:
                if m := DEST_RE.match(line):
                    self.update(file=Path(m.group(1)).name, status="running",
                                stage="Downloading on server…")
                elif m := MERGE_RE.match(line):
                    self.update(status="processing", stage="Merging video and audio…",
                                file=Path(m.group(1)).name)
                elif line.startswith("[ExtractAudio]"):
                    self.update(status="processing", stage="Converting to MP3…")
        return self.reap(proc), last_error

    # -- single video -------------------------------------------------------
    def run_single(self):
        def progress(pct, speed, eta):
            self.update(status="running", stage="Downloading on server…",
                        percent=pct, speed=speed, eta=eta)
        cmd = build_command(self.url, self.quality, self.container, self.out,
                            "%(title)s [%(id)s].%(ext)s",
                            cookie_args(self.dir / "cookies.txt"))
        return self.run_ytdlp(cmd, progress)

    # -- playlist: list the videos, then fetch several at once --------------
    def list_playlist(self):
        """Return (entry URLs, is a single video, exit code, last error)."""
        cmd = ["yt-dlp", "--flat-playlist", "--yes-playlist", "--no-warnings",
               "--color", "never", "--print", "%(_type)s\t%(url)s",
               *cookie_args(self.dir / "cookies.txt"), "--", self.url]
        proc = self.spawn(cmd)
        urls, single, last_error = [], False, ""
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("ERROR:"):
                last_error = line[6:].strip()
                self.log(line)
            elif "\t" in line:
                kind, url = line.split("\t", 1)
                if kind.startswith("url") and url.startswith(("http://", "https://")):
                    urls.append(url)
                elif kind in ("video", "NA"):
                    single = True    # the link is one video, not a playlist
        return urls, single, self.reap(proc), last_error

    def run_playlist(self):
        self.update(stage="Reading playlist…")
        urls, single, code, err = self.list_playlist()
        if not self.cancelled and not urls and single:
            # Not a playlist after all: just download the one video
            self.playlist = False
            return self.run_single()
        if self.cancelled or not urls:
            return (code or 1), err or "No videos found in this playlist."
        total = len(urls)
        width = max(3, len(str(total)))
        pct = {}      # index -> percent of that video
        speed = {}    # index -> bytes/s of that video
        done, failed, errors = [0], [0], []

        def refresh():
            with self.lock:
                overall = sum(pct.values()) / total
                bps = sum(speed.values())
            self.update(status="running", stage="Downloading on server…",
                        percent=overall, speed=format_speed(bps) if bps else "",
                        eta="", item=f"{done[0]} of {total} done")

        def fetch(job):
            index, url = job
            if self.cancelled:
                return
            def progress(p, sp, _eta):
                with self.lock:
                    pct[index], speed[index] = p, parse_speed(sp)
                refresh()
            cmd = build_command(
                url, self.quality, self.container,
                self.out, f"{index:0{width}d} - %(title)s.%(ext)s",
                cookie_args(self.dir / f"cookies-{index}.txt"))
            code, err = self.run_ytdlp(cmd, progress, tag=f"[{index}] ")
            with self.lock:
                pct[index], speed[index] = 100.0, 0.0
                done[0] += 1
                if code != 0:
                    failed[0] += 1
                    errors.append(err or f"yt-dlp exited with code {code}")
            refresh()

        refresh()
        with ThreadPoolExecutor(PLAYLIST_PARALLEL) as pool:
            list(pool.map(fetch, enumerate(urls, start=1)))
        if not failed[0]:
            return 0, ""
        return 1, f"{failed[0]} of {total} videos failed: {errors[-1]}"

    def run(self):
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - never leave a job stuck "running"
            msg = (DISK_FULL_MSG if getattr(exc, "errno", None) == errno.ENOSPC
                   else str(exc))
            self.update(status="error", stage="Download failed", error=msg)
        finally:
            self.finished = time.time()
            if self.state["status"] in ("error", "cancelled"):
                # Nothing to hand out: free the partial files right away
                shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self):
        self.out.mkdir(parents=True, exist_ok=True)
        with slots:
            if self.cancelled:
                return
            self.update(status="running", stage="Fetching video info…")
            try:
                code, last_error = (self.run_playlist() if self.playlist
                                    else self.run_single())
            except OSError as exc:
                self.update(status="error", error=f"Failed to start yt-dlp: {exc}")
                return

        if self.cancelled:
            self.update(status="cancelled", stage="Cancelled")
            return
        files = sorted(p for p in self.out.rglob("*")
                       if p.is_file() and not p.name.endswith((".part", ".ytdl")))
        if code != 0 and not files:
            self.update(status="error", stage="Download failed",
                        error=last_error or f"yt-dlp exited with code {code}")
            return
        if len(files) == 1:
            self.result = files[0]
        else:
            self.update(status="processing", stage="Zipping playlist…", percent=100)
            self.result = self.dir / f"playlist-{self.id[:8]}.zip"
            with zipfile.ZipFile(self.result, "w", zipfile.ZIP_STORED) as zf:
                for f in files:
                    zf.write(f, f.relative_to(self.out))
                    f.unlink()   # keep only one copy on disk
        self.update(status="done", stage="Ready", percent=100, speed="", eta="",
                    file=self.result.name,
                    error=last_error if code != 0 else "")

    def cancel(self):
        self.cancelled = True
        with self.lock:
            procs = list(self.procs)
        for proc in procs:
            kill(proc)
        if self.state["status"] == "queued":
            self.update(status="cancelled", stage="Cancelled")


# --------------------------------------------------------------------------- #
# Converter: upload a local file, convert it with ffmpeg
# --------------------------------------------------------------------------- #
# Input extension -> output format: video in gives MP3, audio in gives MP4.
# Any other file is accepted too: after the upload ffprobe looks inside it and
# picks MP3 when it has a real video picture, MP4 when it is sound only.
CONVERT_TO = {ext: "mp3" for ext in (".mp4", ".m4v", ".mov", ".mkv", ".webm")}
CONVERT_TO.update({ext: "mp4" for ext in
                   (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus")})
# The browser sends the file in pieces of this size. Cloudflare refuses
# request bodies over 100 MB, so each piece stays well under that.
UPLOAD_CHUNK = 32 * 1024 ** 2
UPLOAD_IDLE = 30 * 60      # an upload that sends nothing for this long is dropped
SAFE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,12}")
BAD_NAME_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


class UploadError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def ffprobe(path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120).stdout
        return json.loads(out or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def clock(seconds: float) -> str:
    seconds = int(seconds)
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class ConvertJob(Job):
    def __init__(self, name, size, target):
        super().__init__("", None, target, False)
        self.size = size
        self.received = 0
        self.touched = time.time()
        self.upload_lock = threading.Lock()
        suffix = Path(name).suffix.lower()
        self.src = self.dir / ("input" + (suffix if SAFE_EXT_RE.fullmatch(suffix) else ""))
        self.stem = BAD_NAME_RE.sub("_", Path(name).stem).strip(" ._")[:150] or "converted"
        self.dest = self.out / f"{self.stem}.{target}"
        self.out.mkdir(parents=True, exist_ok=True)
        self.update(status="uploading", stage="Uploading…", file=name)

    # -- upload ---------------------------------------------------------------
    def write(self, offset, stream) -> int:
        """Write one piece at `offset`. A piece may be sent again (after a
        network error), so anything already stored past `offset` is replaced."""
        with self.upload_lock:
            if self.state["status"] != "uploading":
                raise UploadError("This upload was cancelled or has expired.", 410)
            if not 0 <= offset <= self.received:
                raise UploadError("Upload out of order. Please try again.", 409)
            n = offset
            try:
                with open(self.src, "r+b" if self.src.exists() else "wb") as f:
                    f.seek(offset)
                    f.truncate()
                    while chunk := stream.read(1024 * 1024):
                        n += len(chunk)
                        if n > self.size:
                            raise UploadError("The file is bigger than announced.")
                        if self.cancelled:
                            raise UploadError("Cancelled.", 410)
                        f.write(chunk)
                        self.touched = time.time()
            finally:
                self.received = min(n, self.size)
            return self.received

    def start(self):
        with self.upload_lock:
            if self.state["status"] != "uploading":
                raise UploadError("This upload was cancelled or has expired.", 410)
            if self.received != self.size:
                raise UploadError("The upload is incomplete. Please try again.")
            self.update(status="queued", stage="Waiting for a free slot…", percent=0)
        threading.Thread(target=self.run, daemon=True).start()

    def cancel(self):
        super().cancel()
        with self.upload_lock:
            if self.state["status"] == "uploading":
                self.update(status="cancelled", stage="Cancelled")
                self.finished = time.time()
                shutil.rmtree(self.dir, ignore_errors=True)

    # -- conversion ---------------------------------------------------------
    def fail(self, msg):
        self.update(status="error", stage="Conversion failed", error=msg)

    def mp3_command(self):
        return ["-i", str(self.src), "-map", "0:a:0", "-vn", "-map_metadata", "0",
                "-c:a", "libmp3lame", "-q:a", "0", "-id3v2_version", "3"]

    def mp4_command(self, streams, duration):
        """A still picture (the file's cover art, or black) with the sound."""
        picture = None
        cover = next((s for s in streams if s.get("codec_type") == "video"
                      and s.get("disposition", {}).get("attached_pic")), None)
        if cover is not None:
            png = self.dir / "cover.png"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(self.src),
                            "-map", f"0:{cover['index']}", "-frames:v", "1", str(png)],
                           capture_output=True, timeout=120)
            picture = png if png.is_file() else None
        video_in = (["-loop", "1", "-framerate", "1", "-i", str(picture)] if picture
                    else ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=1"])
        cmd = video_in + [
            "-i", str(self.src), "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "1",
            # Even width and height (needed by H.264), at most 1920 wide
            "-vf", "scale='min(1920,trunc(iw/2)*2)':-2,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-r", "1",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart"]
        if duration:
            cmd += ["-t", f"{duration:.3f}"]
        return cmd

    def run_ffmpeg(self, args, duration):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-loglevel", "error",
               "-progress", "pipe:1", "-nostats", *args, str(self.dest)]
        proc = self.spawn(cmd)
        last_error, pos = "", 0.0
        for line in proc.stdout:
            line = line.strip()
            key, eq, value = line.partition("=")
            if eq and " " not in key:        # a -progress field
                if key == "out_time_us" and value.isdigit():
                    pos = int(value) / 1e6
                    if duration:
                        self.update(status="running", stage="Converting…",
                                    percent=min(99.9, pos / duration * 100))
                elif key == "speed" and value.strip().endswith("x"):
                    try:
                        factor = float(value.strip()[:-1])
                    except ValueError:
                        continue
                    eta = clock((duration - pos) / factor) if duration and factor > 0 else ""
                    self.update(speed=f"{factor:g}× real time", eta=eta)
                continue
            if line:
                self.log(line)
                last_error = line
        return self.reap(proc), last_error

    def _run(self):
        with slots:
            if self.cancelled:
                return
            self.update(status="processing", stage="Reading file…", percent=0)
            info = ffprobe(self.src)
            streams = info.get("streams", [])
            if not info.get("format"):
                return self.fail("This file can't be read. Is it really a video or audio file?")
            if not any(s.get("codec_type") == "audio" for s in streams):
                return self.fail("This file has no sound to convert.")
            try:
                duration = float(info["format"].get("duration") or 0)
            except ValueError:
                duration = 0.0
            if self.container == "auto":
                has_picture = any(s.get("codec_type") == "video"
                                  and not s.get("disposition", {}).get("attached_pic")
                                  for s in streams)
                self.container = "mp3" if has_picture else "mp4"
                self.dest = self.out / f"{self.stem}.{self.container}"
            self.update(stage="Converting…")
            args = (self.mp3_command() if self.container == "mp3"
                    else self.mp4_command(streams, duration))
            code, last_error = self.run_ffmpeg(args, duration)
        if self.cancelled:
            self.update(status="cancelled", stage="Cancelled")
            return
        if code != 0 or not self.dest.is_file():
            return self.fail(last_error or f"ffmpeg exited with code {code}")
        self.src.unlink(missing_ok=True)       # keep only the result on disk
        self.result = self.dest
        self.update(status="done", stage="Ready", percent=100, speed="", eta="",
                    file=self.result.name)


def kill(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def free_bytes() -> float:
    try:
        return shutil.disk_usage(JOBS_DIR if JOBS_DIR.is_dir() else JOBS_DIR.parent).free
    except OSError:
        return float("inf")


def make_room():
    """Delete the oldest finished jobs until MIN_FREE bytes are free."""
    while free_bytes() < MIN_FREE:
        with jobs_lock:
            finished = [j for j in jobs.values() if j.finished is not None]
            if not finished:
                return
            oldest = min(finished, key=lambda j: j.finished)
            jobs.pop(oldest.id, None)
        oldest.cancel()
        shutil.rmtree(oldest.dir, ignore_errors=True)


def janitor():
    """Delete jobs (and their files) JOB_TTL seconds after they finish.
    Running and queued jobs are never touched, however long they take."""
    while True:
        time.sleep(60)
        cutoff = time.time() - JOB_TTL
        with jobs_lock:
            old = [j for j in jobs.values()
                   if j.finished is not None and j.finished < cutoff]
            for j in old:
                jobs.pop(j.id, None)
            # Uploads the browser gave up on (tab closed, connection lost)
            idle = time.time() - UPLOAD_IDLE
            stale = [j for j in jobs.values() if isinstance(j, ConvertJob)
                     and j.state["status"] == "uploading" and j.touched < idle]
            for j in stale:
                jobs.pop(j.id, None)
        for j in old + stale:
            j.cancel()
            shutil.rmtree(j.dir, ignore_errors=True)
        # Leftovers from a previous container run
        if JOBS_DIR.is_dir():
            for d in JOBS_DIR.iterdir():
                if d.name not in jobs and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        make_room()


threading.Thread(target=janitor, daemon=True).start()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
OPEN_PATHS = {"/login", "/logout", "/healthz"}
fails: dict[str, list[float]] = {}     # ip -> times of recent wrong passwords
fails_lock = threading.Lock()


def password_tag() -> str:
    """Stored in the session; changing APP_PASSWORD logs everyone out."""
    return hmac.new(app.secret_key.encode(), PASSWORD.encode(), hashlib.sha256).hexdigest()[:32]


def logged_in() -> bool:
    return not PASSWORD or hmac.compare_digest(session.get("auth", ""), password_tag())


def client_ip() -> str:
    # Behind Cloudflare Tunnel the app is only reachable through cloudflared
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


def recent_fails(ip) -> list[float]:
    cutoff = time.time() - FAIL_WINDOW
    with fails_lock:
        fails[ip] = [t for t in fails.get(ip, []) if t > cutoff]
        if not fails[ip]:
            del fails[ip]
            return []
        return fails[ip]


def safe_next(target) -> str:
    """Only allow redirects back into this site."""
    target = target or "/"
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@app.before_request
def require_login():
    if logged_in() or request.path in OPEN_PATHS:
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Please log in again."), 401
    raw = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI") or request.full_path
    return redirect("/login?" + urlencode({"next": raw}), 302)


@app.get("/login")
def login_page():
    if PASSWORD and logged_in():
        return redirect(safe_next(request.args.get("next")))
    return send_from_directory(BASE / "static", "login.html")


@app.post("/login")
def login():
    ip, nxt = client_ip(), safe_next(request.form.get("next"))
    if len(recent_fails(ip)) >= MAX_FAILS:
        return redirect("/login?" + urlencode({"e": "locked", "next": nxt}), 303)
    if hmac.compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
        with fails_lock:
            fails.pop(ip, None)
        session.clear()
        session.permanent = True
        session["auth"] = password_tag()
        return redirect(nxt, 303)
    with fails_lock:
        fails.setdefault(ip, []).append(time.time())
    time.sleep(1)   # slow down guessing
    return redirect("/login?" + urlencode({"e": "wrong", "next": nxt}), 303)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login" if PASSWORD else "/", 302)


@app.before_request
def youtube_shortcuts():
    """Turn /https://youtube.com/watch?v=… or /watch?v=… into an auto-download."""
    qs = request.query_string.decode()
    # Raw (still %-encoded) path, so links to other sites survive unchanged
    raw = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI")
    path = raw.split("?", 1)[0] if raw else request.path
    yt_url = None
    if m := PREFIX_RE.match(path):
        yt_url = f"https://{m.group(1)}{m.group(2) or '/'}"
    elif YT_PATH_RE.match(path):
        yt_url = f"https://www.youtube.com{path}"
    elif ANY_SITE and (m := ANY_PREFIX_RE.match(path)):
        yt_url = f"{m.group(1).lower()}://{m.group(2)}{m.group(3) or '/'}"
    if yt_url:
        if qs:
            yt_url += "?" + qs
        return redirect(f"/?auto=1&url={quote(yt_url, safe='')}", 302)
    return None


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(BASE / "static", name)


@app.get("/healthz")
def health():
    return "ok"


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    url = clean_url(data.get("url", ""))
    if not url or (not ANY_SITE and not is_youtube(url)):
        return jsonify(error="Please enter a video link." if ANY_SITE
                       else "Please enter a YouTube link."), 400
    if not is_youtube(url) and not is_public_host(urlsplit(url).hostname):
        return jsonify(error="That address can't be downloaded from this server."), 400
    quality = QUALITIES.get(str(data.get("quality", "best")), None)
    container = data.get("format", "mp4") if data.get("format") in FORMATS else "mp4"
    playlist = data.get("playlist") is True
    if not playlist and is_youtube(url):
        url = strip_playlist(url)
        if not url:
            return jsonify(error="This link is a whole playlist. Turn on "
                                 "\"Download whole playlist\" to get it as a .zip."), 400
    make_room()
    if free_bytes() < MIN_FREE:
        return jsonify(error=DISK_FULL_MSG), 507
    job = Job(url, quality, container, playlist)
    with jobs_lock:
        jobs[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return jsonify(job.public()), 201


def get_job(job_id) -> "Job":
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    return jsonify(get_job(job_id).public())


@app.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id):
    get_job(job_id).cancel()
    return jsonify(ok=True)


@app.get("/api/jobs/<job_id>/file")
def job_file(job_id):
    job = get_job(job_id)
    if job.state["status"] != "done" or not job.result or not job.result.is_file():
        abort(404)
    return send_file(job.result, as_attachment=True, download_name=job.result.name)


@app.post("/api/convert")
def convert_create():
    """Start an upload: {name, size} -> job id and the piece size to use."""
    data = request.get_json(silent=True) or {}
    # Just the file name, never a path
    name = str(data.get("name") or "").replace("\\", "/").rsplit("/", 1)[-1][-255:]
    size = data.get("size")
    # Every file type is accepted; unknown ones are sorted out after upload
    target = CONVERT_TO.get(Path(name).suffix.lower(), "auto")
    if type(size) is not int or size <= 0:
        return jsonify(error="This file is empty."), 400
    make_room()
    if free_bytes() - size < MIN_FREE:
        return jsonify(error="The server doesn't have enough free disk space for "
                             "this file right now. Try again later."), 507
    job = ConvertJob(name, size, target)
    with jobs_lock:
        jobs[job.id] = job
    return jsonify(job.public() | {"chunk": UPLOAD_CHUNK}), 201


def get_convert_job(job_id) -> ConvertJob:
    job = get_job(job_id)
    if not isinstance(job, ConvertJob):
        abort(404)
    return job


@app.put("/api/convert/<job_id>")
def convert_upload(job_id):
    """One piece of the file, as the raw request body, at ?offset=N."""
    job = get_convert_job(job_id)
    try:
        offset = int(request.args.get("offset", ""))
    except ValueError:
        return jsonify(error="Missing offset."), 400
    try:
        received = job.write(offset, request.stream)
    except UploadError as exc:
        return jsonify(error=str(exc), received=job.received), exc.code
    except OSError as exc:
        if exc.errno != errno.ENOSPC:
            raise
        job.cancel()
        return jsonify(error=DISK_FULL_MSG), 507
    return jsonify(received=received)


@app.post("/api/convert/<job_id>/start")
def convert_start(job_id):
    job = get_convert_job(job_id)
    try:
        job.start()
    except UploadError as exc:
        return jsonify(error=str(exc)), exc.code
    return jsonify(job.public())


def finished_conversions(ids) -> list[ConvertJob]:
    with jobs_lock:
        found = [jobs.get(str(i)) for i in ids]
    return [j for j in dict.fromkeys(found) if isinstance(j, ConvertJob)
            and j.state["status"] == "done" and j.result and j.result.is_file()]


@app.post("/api/convert/keep")
def convert_keep():
    """{ids} -> which of them are still on the server. Each one found gets a
    fresh JOB_TTL, so results are kept while the page is open (until the disk
    runs low: make_room() still deletes the oldest first)."""
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    alive = finished_conversions(ids if isinstance(ids, list) else [])
    now = time.time()
    for j in alive:
        j.finished = now
    return jsonify(alive=[j.id for j in alive])


class _ZipSink:
    """A write-only file for ZipFile; the bytes are handed on as they come."""
    def __init__(self):
        self.parts = []

    def write(self, b):
        self.parts.append(bytes(b))
        return len(b)

    def flush(self):
        pass

    def take(self) -> bytes:
        data = b"".join(self.parts)
        self.parts.clear()
        return data


@app.post("/api/convert/zip")
def convert_zip():
    """Form field ids=a,b,c -> one .zip of those converted files, streamed as it
    is built (no copy on disk, no size limit). MP3/MP4 don't shrink, so the
    files are stored, not compressed."""
    ids = [i for i in (request.form.get("ids") or "").split(",") if i]
    done = finished_conversions(ids)
    if not done:
        return jsonify(error="These files are no longer on the server."), 404
    for j in done:
        j.finished = time.time()
    names, items = set(), []
    for j in done:
        name, n = j.result.name, 1
        while name.lower() in names:      # same name twice: "song (2).mp3"
            n += 1
            name = f"{j.result.stem} ({n}){j.result.suffix}"
        names.add(name.lower())
        items.append((j.result, name))

    def stream():
        sink = _ZipSink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for path, name in items:
                try:
                    src = open(path, "rb")
                except OSError:
                    continue                  # deleted meanwhile: leave it out
                with src:
                    info = zipfile.ZipInfo.from_file(path, name)
                    info.compress_type = zipfile.ZIP_STORED
                    with zf.open(info, "w", force_zip64=True) as dst:
                        while chunk := src.read(1024 * 1024):
                            dst.write(chunk)
                            if sink.parts:
                                yield sink.take()
                yield sink.take()
        yield sink.take()

    stamp = time.strftime("%Y-%m-%d %H.%M")
    return Response(stream(), mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="converted {stamp}.zip"',
        "X-Accel-Buffering": "no", "Cache-Control": "no-store"})


@app.get("/api/config")
def config():
    return jsonify(any_site=ANY_SITE, login=bool(PASSWORD))


@app.get("/api/version")
def version():
    try:
        out = subprocess.run(["yt-dlp", "--version"], capture_output=True,
                             text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        out = "unknown"
    return jsonify(ytdlp=out)


if __name__ == "__main__":
    # Local development only (Docker runs gunicorn). Listens on localhost by
    # default; the Werkzeug debugger allows running code, so it is opt-in.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
