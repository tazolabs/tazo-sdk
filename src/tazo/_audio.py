"""Bounded, replayable uploads and standard-library WAV helpers."""

from __future__ import annotations

import mimetypes
import os
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import InvalidRequestError, RequestTooLargeError

AudioInput = str | os.PathLike[str] | bytes | BinaryIO
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, repr=False)
class Upload:
    """One immutable snapshot, reused unchanged for every HTTP attempt."""

    data: bytes
    filename: str
    content_type: str


def snapshot_audio(audio: AudioInput, filename: str | None, content_type: str | None) -> Upload:
    name = "audio"
    if isinstance(audio, bytes):
        data = audio
    elif isinstance(audio, (str, os.PathLike)):
        path = Path(audio)
        name = path.name
        with path.open("rb") as handle:
            data = handle.read(MAX_UPLOAD_BYTES + 1)
    else:
        candidate = getattr(audio, "name", None)
        if isinstance(candidate, str):
            name = Path(candidate).name
        position = audio.tell() if audio.seekable() else None
        try:
            data = audio.read(MAX_UPLOAD_BYTES + 1)
        finally:
            if position is not None:
                audio.seek(position)
        if not isinstance(data, bytes):
            raise InvalidRequestError("Audio must be a binary file, bytes, or a filesystem path.")
    if not data:
        raise InvalidRequestError("Audio must not be empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise RequestTooLargeError("Audio uploads are limited to 16 MiB.")
    name = Path(filename).name if filename else name
    media_type = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Upload(data, name, media_type)


def wav_chunks(path: str | os.PathLike[str], *, chunk_ms: int = 100) -> Iterator[bytes]:
    """Yield raw mono 16 kHz PCM16 from a WAV, without headers or real-time pacing.

    Args:
        path: PCM16 WAV recording. Compressed WAV and other formats are rejected.
        chunk_ms: Chunk size from 1 to 200 milliseconds.

    Yields:
        Binary audio frames suitable for ``session.send_audio``.
    """
    if isinstance(chunk_ms, bool) or not isinstance(chunk_ms, int) or not 1 <= chunk_ms <= 200:
        raise InvalidRequestError("chunk_ms must be an integer between 1 and 200.")
    with wave.open(str(path), "rb") as handle:
        if (
            handle.getnchannels(),
            handle.getsampwidth(),
            handle.getframerate(),
            handle.getcomptype(),
        ) != (1, 2, 16000, "NONE"):
            raise InvalidRequestError("Streaming requires a mono, 16 kHz, PCM16 WAV.")
        if handle.getnframes() == 0:
            raise InvalidRequestError("Audio must not be empty.")
        remaining = handle.getnframes()
        while remaining:
            frames = min(chunk_ms * 16, remaining)
            data = handle.readframes(frames)
            if len(data) != frames * 2:
                raise InvalidRequestError("The WAV file is truncated.")
            remaining -= frames
            yield data
