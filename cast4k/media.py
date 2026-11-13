"""Аналіз відеофайлів (ffprobe) і побудова ffmpeg-команд для стріму на Chromecast."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".webm"}

# Що Chromecast / Google TV вміє грати "як є"
CAST_VIDEO_CODECS = {"h264", "hevc", "vp9", "av1"}
CAST_AUDIO_COPY = {"aac", "mp3", "ac3", "eac3", "opus", "flac"}
DIRECT_CONTAINERS = {".mp4", ".m4v", ".webm"}
DIRECT_AUDIO = {"aac", "mp3", "opus"}
TEXT_SUBS = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@dataclass
class Track:
    index: int  # абсолютний індекс потоку у файлі
    codec: str
    lang: str = ""
    title: str = ""
    channels: int = 0
    default: bool = False

    def label(self) -> str:
        parts = [self.lang or "und", self.codec]
        if self.channels:
            parts.append(f"{self.channels}ch")
        if self.title:
            parts.append(self.title)
        return " · ".join(parts)


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    video_codec: str = ""
    width: int = 0
    height: int = 0
    hdr: str = ""
    audio: list[Track] = field(default_factory=list)
    subs: list[Track] = field(default_factory=list)

    @property
    def resolution(self) -> str:
        if self.height >= 2000 or self.width >= 3800:
            return "4K"
        if self.height >= 1000:
            return "1080p"
        return f"{self.height}p" if self.height else "?"

    def can_direct(self, audio_idx: int | None) -> bool:
        """Файл можна віддати на ТВ без ffmpeg (з нативною перемоткою)."""
        if self.path.suffix.lower() not in DIRECT_CONTAINERS:
            return False
        if self.video_codec not in CAST_VIDEO_CODECS:
            return False
        a = self.pick_audio(audio_idx)
        if a is None:
            return True
        # Для прямого режиму ТВ грає першу аудіодоріжку
        return a.codec in DIRECT_AUDIO and (not self.audio or a.index == self.audio[0].index)

    def pick_audio(self, audio_idx: int | None) -> Track | None:
        if not self.audio:
            return None
        for a in self.audio:
            if a.index == audio_idx:
                return a
        for a in self.audio:
            if a.default:
                return a
        return self.audio[0]

    def to_dict(self) -> dict:
        return {
            "duration": self.duration,
            "video": self.video_codec,
            "resolution": self.resolution,
            "hdr": self.hdr,
            "audio": [{"index": a.index, "label": a.label()} for a in self.audio],
            "subs": [{"index": s.index, "label": s.label()} for s in self.subs if s.codec in TEXT_SUBS],
        }


def probe(path: Path) -> MediaInfo:
    info = MediaInfo(path=path)
    if not have_ffmpeg():
        return info
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    data = json.loads(out.stdout or "{}")
    info.duration = float(data.get("format", {}).get("duration") or 0)
    for s in data.get("streams", []):
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        kind = s.get("codec_type")
        if kind == "video" and not info.video_codec and not disp.get("attached_pic"):
            info.video_codec = s.get("codec_name", "")
            info.width = int(s.get("width") or 0)
            info.height = int(s.get("height") or 0)
            trc = s.get("color_transfer", "")
            info.hdr = {"smpte2084": "HDR10", "arib-std-b67": "HLG"}.get(trc, "")
        elif kind == "audio":
            info.audio.append(Track(s["index"], s.get("codec_name", ""), tags.get("language", ""),
                                    tags.get("title", ""), int(s.get("channels") or 0), bool(disp.get("default"))))
        elif kind == "subtitle":
            info.subs.append(Track(s["index"], s.get("codec_name", ""), tags.get("language", ""),
                                   tags.get("title", ""), 0, bool(disp.get("default"))))
    return info


def stream_cmd(info: MediaInfo, start: float, audio_idx: int | None) -> list[str]:
    """ffmpeg: відео копіюється без втрат (4K лишається 4K), аудіо — копія або AAC 5.1.

    Вихід — fragmented MP4 у stdout, який Chromecast грає як звичайне відео.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(info.path), "-map", "0:v:0"]
    a = info.pick_audio(audio_idx)
    if a:
        cmd += ["-map", f"0:{a.index}"]
    cmd += ["-c:v", "copy"]
    if info.video_codec == "hevc":
        cmd += ["-tag:v", "hvc1"]
    if a:
        if a.codec in CAST_AUDIO_COPY and a.codec not in {"flac", "opus"}:
            cmd += ["-c:a", "copy"]
        else:  # DTS, TrueHD, PCM, FLAC... -> AAC 5.1 (усі Chromecast це грають)
            ch = min(a.channels or 2, 6)
            cmd += ["-c:a", "aac", "-b:a", "448k" if ch > 2 else "192k", "-ac", str(ch)]
    cmd += ["-sn", "-dn", "-map_chapters", "-1",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration", "2000000", "-f", "mp4", "pipe:1"]
    return cmd


def subtitles_vtt(info: MediaInfo, sub_idx: int, start: float) -> bytes:
    """Витягує текстові субтитри у WebVTT, зсунуті на `start` секунд."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(info.path), "-map", f"0:{sub_idx}", "-f", "webvtt", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, timeout=300).stdout


def external_srt(path: Path) -> Path | None:
    for ext in (".srt", ".vtt"):
        p = path.with_suffix(ext)
        if p.exists():
            return p
    return None


def external_vtt(srt: Path, start: float) -> bytes:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-sub_charenc", "UTF-8"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(srt), "-f", "webvtt", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, timeout=60).stdout


def scan(folders: list[Path]) -> list[Path]:
    files: list[Path] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for p in folder.rglob("*"):
            if (p.suffix.lower() in VIDEO_EXTS and p.is_file() and not p.name.startswith(".")
                    and p.stat().st_size > 50 * 1024 * 1024):
                files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files
