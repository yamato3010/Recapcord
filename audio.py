import re
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from discord.sinks import Sink


PCM_BYTES_PER_SECOND = 48_000 * 2 * 2
CLIP_PAUSE_SECONDS = 1.5


@dataclass
class AudioClip:
    speaker_id: int
    start: float
    path: Path


class RecordingSink(Sink):
    """Write timestamped clips per speaker to temporary WAV files."""

    def __init__(self, directory: Path):
        super().__init__()
        self.directory = directory
        self.started_at = time.monotonic()
        self.clips: list[AudioClip] = []
        self.speaker_names: dict[int, str] = {}
        self._tracks = {}
        self._lock = threading.Lock()
        self._closed = False

    def write(self, data, user):
        if user is None:
            return
        if getattr(user, "bot", False):
            return
        pcm = data if isinstance(data, bytes) else data.pcm
        if not pcm:
            return

        now = time.monotonic() - self.started_at
        with self._lock:
            if self._closed:
                return
            if name := getattr(user, "display_name", None):
                self.speaker_names[user.id] = name
            track = self._tracks.get(user.id)
            if track is not None:
                wav, start, last_packet_end = track
                gap = now - last_packet_end
                if gap > CLIP_PAUSE_SECONDS or now - start >= 60:
                    wav.close()
                    track = None
                elif gap > 0.1:
                    # Retain pauses for Whisper and keep the clip timestamps aligned.
                    silence_frames = round(gap * 48_000)
                    wav.writeframesraw(b"\x00" * (silence_frames * 4))
            if track is None:
                path = self.directory / f"{len(self.clips):06d}.wav"
                wav = wave.open(str(path), "wb")
                wav.setparams((2, 2, 48_000, 0, "NONE", "not compressed"))
                start = now
                self.clips.append(AudioClip(user.id, start, path))
            self._tracks[user.id] = (wav, start, now + len(pcm) / PCM_BYTES_PER_SECOND)
            wav.writeframesraw(pcm)

    def cleanup(self):
        with self._lock:
            self._closed = True
            for wav, *_ in self._tracks.values():
                wav.close()
            self._tracks.clear()


def timestamp(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def markdown_text(text: str) -> str:
    return re.sub(r'([\\`*_{}\[\]<>|~])', r'\\\1', " ".join(text.split()))


def save_transcript(model, clips, speaker_names, language, destination, *, started_at):
    rows = []
    for clip in clips:
        speaker = speaker_names.get(clip.speaker_id, f"ユーザー {clip.speaker_id}")
        segments, _ = model.transcribe(
            str(clip.path), language=language, vad_filter=True
        )
        for segment in segments:
            if text := markdown_text(segment.text):
                rows.append((clip.start + segment.start, speaker, text))

    lines = [
        "# トランスクリプト",
        "",
        f"録音開始日時: {started_at.isoformat(sep=' ', timespec='seconds')}",
        "",
    ]
    for start, speaker, text in sorted(rows):
        lines.append(f"- `{timestamp(start)}` **{markdown_text(speaker)}** — {text}")
    if not rows:
        lines.append("文字起こしできる発話はありませんでした。")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(".tmp")
    try:
        staging.write_text("\n".join(lines) + "\n", encoding="utf-8")
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)
    return len(rows)
