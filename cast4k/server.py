"""Cast4K: локальний сервер на Mac, що віддає фільм на Android TV (Chromecast built-in)."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import socket
import subprocess
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import media

UI_FILE = Path(__file__).with_name("ui.html")
DEFAULT_FOLDERS = [Path.home() / "Downloads", Path.home() / "Movies", Path.home() / "Desktop"]


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Library:
    """Відображає файли на непрозорі id, щоб сервер не віддавав довільні шляхи."""

    def __init__(self, folders: list[Path]):
        self.folders = folders
        self.files: dict[str, Path] = {}
        self.info: dict[str, media.MediaInfo] = {}
        self.lock = threading.Lock()

    def add(self, p: Path) -> str:
        p = p.expanduser().resolve()
        fid = hashlib.sha1(str(p).encode()).hexdigest()[:12]
        with self.lock:
            self.files[fid] = p
        return fid

    def refresh(self) -> list[dict]:
        out = []
        for p in media.scan(self.folders):
            fid = self.add(p)
            out.append({"id": fid, "name": p.name, "folder": str(p.parent),
                        "size_gb": round(p.stat().st_size / 1e9, 1)})
        return out

    def get_info(self, fid: str) -> media.MediaInfo:
        if fid not in self.info:
            self.info[fid] = media.probe(self.files[fid])
        return self.info[fid]


class Caster:
    """Пошук ТВ і керування відтворенням через pychromecast."""

    def __init__(self):
        self.devices: dict[str, object] = {}
        self.cast = None
        self.session: dict = {}
        self.lock = threading.Lock()

    def discover(self, timeout: float = 6) -> list[dict]:
        import pychromecast
        casts, browser = pychromecast.get_chromecasts(timeout=timeout)
        browser.stop_discovery()
        for c in casts:
            self.devices[str(c.uuid)] = c
        return [{"id": str(c.uuid), "name": c.cast_info.friendly_name, "model": c.cast_info.model_name}
                for c in casts]

    def connect(self, dev_id: str):
        c = self.devices[dev_id]
        if self.cast is not c:
            c.wait(timeout=10)
            self.cast = c
        return c

    def load(self, url: str, title: str, mode: str, offset: float, duration: float,
             subs_url: str | None, start_at: float = 0):
        mc = self.cast.media_controller
        meta = {"metadataType": 1, "title": title}
        mc.play_media(url, "video/mp4", title=title, metadata=meta, stream_type="BUFFERED",
                      current_time=start_at if mode == "direct" else None,
                      subtitles=subs_url, subtitles_lang="uk", subtitle_id=1)
        mc.block_until_active(timeout=20)
        if subs_url:
            time.sleep(1.0)
            try:
                mc.enable_subtitle(1)
            except Exception:
                pass
        self.session.update(mode=mode, offset=offset, duration=duration, title=title)

    def status(self) -> dict:
        if not self.cast:
            return {"connected": False}
        st = self.cast.media_controller.status
        pos = (st.current_time or 0) + (self.session.get("offset", 0) if self.session.get("mode") == "live" else 0)
        return {
            "connected": True,
            "device": self.cast.cast_info.friendly_name,
            "state": st.player_state,
            "position": pos,
            "duration": self.session.get("duration") or st.duration or 0,
            "title": self.session.get("title", ""),
            "volume": round((self.cast.status.volume_level if self.cast.status else 0) * 100),
        }


class App:
    def __init__(self, folders: list[Path], port: int):
        self.lib = Library(folders)
        self.caster = Caster()
        self.port = port
        self.ip = local_ip()
        self.current: dict = {}

    def base(self) -> str:
        return f"http://{self.ip}:{self.port}"

    def play(self, fid: str, dev: str, start: float = 0, audio: int | None = None, sub: str | None = None):
        info = self.lib.get_info(fid)
        self.caster.connect(dev)
        direct = info.can_direct(audio) or not media.have_ffmpeg()
        nonce = int(time.time())
        aq = "" if audio is None else f"&a={audio}"
        if direct:
            url, offset = f"{self.base()}/file/{fid}?n={nonce}", 0.0
        else:
            url, offset = f"{self.base()}/live/{fid}.mp4?t={start:.2f}{aq}&n={nonce}", start
        subs_url = None
        if sub:
            subs_url = f"{self.base()}/subs/{fid}.vtt?s={quote(sub)}&t={offset:.2f}&n={nonce}"
        self.current = {"fid": fid, "dev": dev, "audio": audio, "sub": sub}
        self.caster.load(url, info.path.stem, "direct" if direct else "live", offset, info.duration,
                         subs_url, start_at=start)

    def control(self, action: str, value: float | None):
        c = self.caster.cast
        if not c:
            raise RuntimeError("ТВ не підключено")
        mc = c.media_controller
        if action == "pause":
            mc.pause()
        elif action == "play":
            mc.play()
        elif action == "stop":
            mc.stop()
        elif action == "volume":
            c.set_volume(max(0.0, min(1.0, (value or 0) / 100)))
        elif action in ("seek", "jump"):
            st = self.caster.status()
            target = float(value or 0) if action == "seek" else st["position"] + float(value or 0)
            target = max(0.0, min(target, (st["duration"] or target + 1) - 1))
            if self.caster.session.get("mode") == "direct":
                mc.seek(target)
            else:  # live-ремукс: перезапускаємо ffmpeg з нової позиції
                cur = self.current
                self.play(cur["fid"], cur["dev"], target, cur["audio"], cur["sub"])


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if os.environ.get("CAST4K_DEBUG"):
                super().log_message(fmt, *args)

        # ---------- helpers ----------
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _local_only(self) -> bool:
            # API керування — тільки з самого Mac; ТВ отримує лише медіа
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json({"error": "forbidden"}, 403)
                return False
            return True

        # ---------- routes ----------
        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head=False):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    body = UI_FILE.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if not head:
                        self.wfile.write(body)
                elif u.path == "/api/files":
                    if self._local_only():
                        self._json({"files": app.lib.refresh(), "folders": [str(f) for f in app.lib.folders]})
                elif u.path == "/api/info":
                    if self._local_only():
                        info = app.lib.get_info(q["id"]).to_dict()
                        p = app.lib.files[q["id"]]
                        if media.external_srt(p):
                            info["subs"].insert(0, {"index": "ext", "label": "Файл " + media.external_srt(p).name})
                        info["direct"] = app.lib.get_info(q["id"]).can_direct(None)
                        self._json(info)
                elif u.path == "/api/devices":
                    if self._local_only():
                        self._json({"devices": app.caster.discover()})
                elif u.path == "/api/status":
                    if self._local_only():
                        self._json(app.caster.status())
                elif m := re.fullmatch(r"/file/(\w+)", u.path):
                    self._serve_file(app.lib.files[m[1]], head)
                elif m := re.fullmatch(r"/live/(\w+)\.mp4", u.path):
                    a = q.get("a")
                    self._serve_live(m[1], float(q.get("t", 0)), int(a) if a else None, head)
                elif m := re.fullmatch(r"/subs/(\w+)\.vtt", u.path):
                    self._serve_subs(m[1], q.get("s", ""), float(q.get("t", 0)), head)
                else:
                    self._json({"error": "not found"}, 404)
            except KeyError:
                self._json({"error": "unknown id"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if not self._local_only():
                return
            u = urlparse(self.path)
            try:
                b = self._body()
                if u.path == "/api/play":
                    if b.get("path"):
                        b["file"] = app.lib.add(Path(b["path"]))
                    audio = b.get("audio")
                    app.play(b["file"], b["device"], float(b.get("start") or 0),
                             int(audio) if audio not in (None, "") else None, b.get("sub") or None)
                    self._json({"ok": True})
                elif u.path == "/api/control":
                    app.control(b["action"], b.get("value"))
                    self._json({"ok": True})
                elif u.path == "/api/add":
                    p = Path(b["path"]).expanduser()
                    if not p.is_file():
                        return self._json({"error": "файл не знайдено"}, 400)
                    self._json({"id": app.lib.add(p), "name": p.name})
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:  # noqa: BLE001 — показуємо помилку в UI
                self._json({"error": str(e) or e.__class__.__name__}, 500)

        # ---------- media ----------
        def _serve_file(self, path: Path, head: bool):
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
                if m[1]:
                    start = int(m[1])
                    end = int(m[2]) if m[2] else size - 1
                else:
                    start = size - int(m[2])
                end = min(end, size - 1)
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self._cors()
            self.end_headers()
            if head:
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _serve_live(self, fid: str, start: float, audio: int | None, head: bool):
            info = app.lib.get_info(fid)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Accept-Ranges", "none")
            self._cors()
            self.end_headers()
            if head:
                self.wfile.write(b"0\r\n\r\n")
                return
            proc = subprocess.Popen(media.stream_cmd(info, start, audio), stdout=subprocess.PIPE,
                                    stderr=None if os.environ.get("CAST4K_DEBUG") else subprocess.DEVNULL)
            try:
                while chunk := proc.stdout.read(1 << 18):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")
            finally:
                proc.kill()
                proc.wait()

        def _serve_subs(self, fid: str, sel: str, start: float, head: bool):
            path = app.lib.files[fid]
            if sel == "ext":
                data = media.external_vtt(media.external_srt(path), start)
            else:
                data = media.subtitles_vtt(app.lib.get_info(fid), int(sel), start)
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            if not head:
                self.wfile.write(data)

    return H


def main():
    ap = argparse.ArgumentParser(description="Cast4K — фільми з Mac на телевізор у 4K")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CAST4K_PORT", 8765)))
    ap.add_argument("--folder", action="append", type=Path, help="папка з фільмами (можна кілька)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    folders = args.folder or DEFAULT_FOLDERS
    app = App(folders, args.port)
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(app))
    srv.daemon_threads = True
    ui = f"http://localhost:{args.port}/"
    print(f"Cast4K працює: {ui}  (IP для ТВ: {app.ip})")
    if not media.have_ffmpeg():
        print("⚠️  ffmpeg не знайдено — MKV/DTS не гратимуть. Встанови: brew install ffmpeg")
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(ui)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nБувай!")


if __name__ == "__main__":
    main()
