"""YT Downloader (web) - a Flask front-end for yt-dlp + ffmpeg.

The server downloads with yt-dlp into a per-job folder, the browser polls
for progress, then fetches the finished file (or a zip for playlists).

"yt prefix" shortcuts - any of these open the app and start downloading:
  https://<this-host>/https://www.youtube.com/watch?v=ID   (prepend host)
  https://<this-host>/watch?v=ID                            (YouTube-shaped path)
"""

import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from flask import Flask, abort, jsonify, redirect, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("JOBS_DIR", BASE / "jobs"))
COOKIES = os.environ.get("COOKIES_FILE", "/data/cookies.txt")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
JOB_TTL = int(os.environ.get("JOB_TTL_MINUTES", "60")) * 60
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "2160"))

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
ITEM_RE = re.compile(r"^\[download\] Downloading item (\d+) of (\d+)")

# "/https://www.youtube.com/watch?v=..." (slashes may be collapsed by proxies)
PREFIX_RE = re.compile(
    r"^/(?:https?:/+)?((?:www\.|m\.|music\.)?youtube\.com|youtu\.be)(/.*)?$", re.I)
# YouTube-shaped paths, for a host like "ytyoutube.<tld>" pointed at this app
YT_PATH_RE = re.compile(r"^/(watch|playlist|shorts/|live/|embed/|v/)", re.I)

app = Flask(__name__, static_folder=None)
app.url_map.merge_slashes = False

jobs: dict[str, "Job"] = {}
jobs_lock = threading.Lock()
slots = threading.Semaphore(MAX_CONCURRENT)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean_youtube_url(raw: str) -> str | None:
    """Return a normalised https YouTube URL, or None if it isn't one."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.hostname not in YT_HOSTS:
        return None
    return parts._replace(scheme="https").geturl()


def build_command(url, quality, container, outdir, playlist) -> list[str]:
    template = ("%(playlist_index)03d - %(title)s.%(ext)s" if playlist
                else "%(title)s [%(id)s].%(ext)s")
    cmd = [
        "yt-dlp", "--newline", "--color", "never",
        "--progress-template", PROGRESS_TEMPLATE,
        "--yes-playlist" if playlist else "--no-playlist",
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
    if COOKIES and os.path.isfile(COOKIES):
        # Copy so yt-dlp's cookie writes never touch the mounted original
        cookie_copy = Path(outdir).parent / "cookies.txt"
        shutil.copyfile(COOKIES, cookie_copy)
        cmd += ["--cookies", str(cookie_copy)]
    cmd += ["--", url]
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
        self.proc: subprocess.Popen | None = None
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
        self.state["log"] = (self.state["log"] + [line])[-60:]

    def public(self):
        s = dict(self.state)
        s["id"] = self.id
        s["download_name"] = self.result.name if self.result else ""
        return s

    def run(self):
        self.out.mkdir(parents=True, exist_ok=True)
        with slots:
            if self.cancelled:
                return
            self.update(status="running", stage="Fetching video info…")
            cmd = build_command(self.url, self.quality, self.container,
                                self.out, self.playlist)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=env, start_new_session=True)
            except OSError as exc:
                self.update(status="error", error=f"Failed to start yt-dlp: {exc}")
                return
            last_error = ""
            for line in self.proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if m := PROGRESS_RE.search(line):
                    self.update(status="running", stage="Downloading on server…",
                                percent=float(m.group(1)), speed=m.group(2), eta=m.group(3))
                    continue
                if line.startswith("[PROG]"):
                    continue
                self.log(line)
                if m := DEST_RE.match(line):
                    self.update(file=Path(m.group(1)).name, status="running",
                                stage="Downloading on server…")
                elif m := MERGE_RE.match(line):
                    self.update(status="processing", stage="Merging video and audio…",
                                file=Path(m.group(1)).name)
                elif line.startswith("[ExtractAudio]"):
                    self.update(status="processing", stage="Converting to MP3…")
                elif m := ITEM_RE.match(line):
                    self.update(item=f"{m.group(1)} of {m.group(2)}")
                elif line.startswith("ERROR:"):
                    last_error = line[6:].strip()
            code = self.proc.wait()
            self.proc = None

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
        self.update(status="done", stage="Ready", percent=100, speed="", eta="",
                    file=self.result.name,
                    error=last_error if code != 0 else "")

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.state["status"] == "queued":
            self.update(status="cancelled", stage="Cancelled")


def janitor():
    """Delete finished jobs (and their files) after JOB_TTL seconds."""
    while True:
        time.sleep(60)
        cutoff = time.time() - JOB_TTL
        with jobs_lock:
            old = [j for j in jobs.values() if j.created < cutoff]
            for j in old:
                jobs.pop(j.id, None)
        for j in old:
            j.cancel()
            shutil.rmtree(j.dir, ignore_errors=True)
        # Leftovers from a previous container run
        if JOBS_DIR.is_dir():
            for d in JOBS_DIR.iterdir():
                if d.name not in jobs and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)


threading.Thread(target=janitor, daemon=True).start()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.before_request
def youtube_shortcuts():
    """Turn /https://youtube.com/watch?v=… or /watch?v=… into an auto-download."""
    path, qs = request.path, request.query_string.decode()
    yt_url = None
    if m := PREFIX_RE.match(path):
        yt_url = f"https://{m.group(1)}{m.group(2) or '/'}"
    elif YT_PATH_RE.match(path):
        yt_url = f"https://www.youtube.com{path}"
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
    url = clean_youtube_url(data.get("url", ""))
    if not url:
        return jsonify(error="Please enter a YouTube link."), 400
    quality = QUALITIES.get(str(data.get("quality", "best")), None)
    container = data.get("format", "mp4") if data.get("format") in FORMATS else "mp4"
    job = Job(url, quality, container, bool(data.get("playlist")))
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


@app.get("/api/version")
def version():
    try:
        out = subprocess.run(["yt-dlp", "--version"], capture_output=True,
                             text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        out = "unknown"
    return jsonify(ytdlp=out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=True,
            threaded=True)
