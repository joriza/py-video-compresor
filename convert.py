#!/usr/bin/env python3
"""py-video-converter: conversión de videos locales a HEVC MP4 de tamaño minimizado.

Convierte uno o más archivos de video en archivos HEVC MP4 de tamaño
minimizado, escritos junto a cada fuente como ``<stem><suffix>.mp4``
(sufijo por defecto ``_m``).

Puntos de diseño:

* El encoder por defecto es la CPU (libx265); la detección de encoders
  por hardware (NVENC -> QSV -> AMF) es opcional vía --hw y corre una vez
  por corrida con respaldo de CPU, incluyendo un reintento único
  automático por archivo ante un fallo en tiempo de ejecución del encoder
  por hardware.
* Las familias de opciones (codecs, políticas de resolución, perfiles de
  velocidad, encoders) son registros en el tope del módulo, de modo que las
  opciones futuras sean solo entradas nuevas.
* Política de audio inteligente: copia directa de streams AAC sin pérdida a
  <= 160 kbit/s; en caso contrario, re-codificación a AAC (128k hasta
  estéreo, 256k por encima).
* Seguro para Windows: ffmpeg/ffprobe siempre se invocan como listas de
  argumentos, nunca a través de una shell; los nombres de archivo pueden
  contener espacios, paréntesis y unicode.

Requiere solo la biblioteca estándar de Python más ffmpeg/ffprobe del sistema.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constantes y registros
# --------------------------------------------------------------------------- #

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
MB = 1024 * 1024
ENCODER_PROBE_TIMEOUT_S = 15
FFPROBE_TIMEOUT_S = 30
AUDIO_COPY_MAX_BITRATE = 160_000  # bit/s; AAC a este valor o menos se copia directo
MAX_BITS_PER_PIXEL = 0.08  # tope de densidad de bits: bits por píxel por cuadro
MIN_MAXRATE_BPS = 300_000  # bit/s; piso del tope para resoluciones chicas
MAXRATE_BUFFER_MULT = 2.0  # bufsize = maxrate * este factor (buffer VBV)
PROGRESS_BAR_WIDTH = 24
PROGRESS_MIN_INTERVAL_S = 0.1

VIDEO_EXTENSIONS = {
    "mp4",
    "mkv",
    "avi",
    "mov",
    "m4v",
    "webm",
    "mpg",
    "mpeg",
    "ts",
    "wmv",
    "flv",
}


@dataclass(frozen=True)
class ResPolicy:
    """Política de resolución: tope opcional de altura, del lado corto, o de
    ambos lados (caja) para la salida (conserva el aspecto y nunca reescala
    hacia arriba).
    """

    description: str
    implemented: bool = False
    max_height: int | None = None
    max_short_side: int | None = None
    max_long_side: int | None = None


RES_POLICIES: dict[str, ResPolicy] = {
    "short480": ResPolicy(
        "fit within standard 480p bounds (854x480 landscape / 480x854 "
        "portrait); never upscale",
        implemented=True,
        max_short_side=480,
        max_long_side=854,
    ),
    "max720": ResPolicy(
        "cap height at 720p; never upscale", implemented=True, max_height=720
    ),
    "keep": ResPolicy("keep the source resolution"),
    "smart1080": ResPolicy("downscale to 1080p only when it saves bitrate"),
}


@dataclass(frozen=True)
class SpeedProfile:
    """Perfil de velocidad de codificación con nombre que selecciona los
    argumentos por encoder.
    """

    description: str
    implemented: bool = False


SPEED_PROFILES: dict[str, SpeedProfile] = {
    "fast": SpeedProfile("balanced speed/quality argument set", implemented=True),
    "max": SpeedProfile("maximum throughput argument set"),
    "ultra": SpeedProfile("preview-grade argument set"),
}


# Argumentos HEVC por perfil de velocidad; un perfil nuevo es una clave nueva aquí.
HEVC_ENCODER_ARGS: dict[str, dict[str, tuple[str, ...]]] = {
    "hevc_nvenc": {
        "fast": (
            "-preset",
            "p6",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            "26",
            "-b:v",
            "0",
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-rc-lookahead",
            "20",
        ),
    },
    "hevc_qsv": {
        "fast": ("-preset", "veryfast", "-global_quality", "26"),
    },
    "hevc_amf": {
        "fast": ("-quality", "balanced", "-rc", "qvbr", "-qvbr_quality_level", "26"),
    },
    "libx265": {
        "fast": (
            "-preset",
            "fast",
            "-crf",
            "28",
            "-x265-params",
            "log-level=error",
            "-tag:v",
            "hvc1",
        ),
    },
}


@dataclass(frozen=True)
class EncoderSpec:
    """Un encoder de ffmpeg: argumentos de sondeo más argumentos por perfil
    de velocidad.
    """

    name: str
    description: str
    # Argumentos para el mini sondeo de disponibilidad. None significa que
    # el encoder nunca se sondea porque es un respaldo de CPU siempre
    # disponible.
    probe_args: tuple[str, ...] | None
    args_by_speed: dict[str, tuple[str, ...]]

    def video_args(self, speed: str) -> tuple[str, ...]:
        """Devuelve los argumentos del encoder configurados para ``speed``."""
        return self.args_by_speed.get(speed, ())


def _hevc_encoder(
    name: str, description: str, probe_args: tuple[str, ...] | None
) -> EncoderSpec:
    """Construye un EncoderSpec conectado a la tabla compartida de argumentos HEVC."""
    return EncoderSpec(name, description, probe_args, HEVC_ENCODER_ARGS[name])


@dataclass(frozen=True)
class CodecSpec:
    """Un codec destino: orden de sondeo por hardware más un respaldo
    siempre disponible.
    """

    description: str
    implemented: bool = False
    hw_probe_order: tuple[EncoderSpec, ...] = ()
    cpu_fallback: EncoderSpec | None = None


CODECS: dict[str, CodecSpec] = {
    "hevc": CodecSpec(
        description="HEVC/H.265 in MP4",
        implemented=True,
        hw_probe_order=(
            _hevc_encoder("hevc_nvenc", "NVIDIA NVENC", ("-preset", "p1")),
            _hevc_encoder("hevc_qsv", "Intel Quick Sync", ("-preset", "veryfast")),
            _hevc_encoder("hevc_amf", "AMD AMF", ("-quality", "speed")),
        ),
        cpu_fallback=_hevc_encoder("libx265", "libx265 (CPU)", None),
    ),
    "av1": CodecSpec(description="AV1 (planned)"),
    "h264": CodecSpec(description="H.264 (planned)"),
}


# --------------------------------------------------------------------------- #
# Tipos de datos
# --------------------------------------------------------------------------- #


class MediaError(RuntimeError):
    """Se lanza cuando un archivo de medios no puede inspeccionarse ni convertirse."""


@dataclass
class MediaInfo:
    """Metadatos extraídos por ffprobe para un archivo fuente."""

    path: Path
    width: int = 0
    height: int = 0
    video_codec: str = ""
    duration_s: float | None = None
    fps: float | None = None
    size_bytes: int = 0
    audio_streams: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ConvertOptions:
    """Opciones de CLI validadas, compartidas por todos los archivos de la corrida."""

    codec: str
    res_policy: str
    speed: str
    suffix: str
    force: bool
    dry_run: bool
    hw: bool
    crf: int


@dataclass
class FileResult:
    """Resultado de un intento de conversión de un archivo."""

    src: Path
    dst: Path | None
    status: str  # "ok" | "failed" | "skipped"
    detail: str = ""
    in_bytes: int = 0
    out_bytes: int = 0
    elapsed_s: float = 0.0
    speed_factor: float | None = None
    encoder_used: str = ""


# --------------------------------------------------------------------------- #
# Auxiliares de formateo
# --------------------------------------------------------------------------- #


def say(message: str = "") -> None:
    """Imprime con flush inmediato para mantener el orden de logs y progreso."""
    print(message, flush=True)


def human_mb(n_bytes: float) -> str:
    """Formatea una cantidad de bytes como megabytes con un decimal."""
    return f"{n_bytes / MB:.1f}"


def format_clock(seconds: float | None) -> str:
    """Formatea segundos como MM:SS (o H:MM:SS por encima de una hora)."""
    total = round(max(0.0, seconds or 0.0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def configure_utf8_stdio() -> None:
    """Fuerza stdio UTF-8 para que los nombres de archivo unicode se impriman
    de forma segura en Windows.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="replace")


def _remove_quietly(path: Path) -> None:
    """Borra un archivo de salida parcial, ignorando errores de borrado."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Sondeo de medios
# --------------------------------------------------------------------------- #


def _to_float(value: object) -> float | None:
    """Convierte a float campos de ffprobe que llegan como cadena, si es posible."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _safe_int(value: object, default: int = 0) -> int:
    """Convierte a int lo mejor posible; devuelve ``default`` si algo falla."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_rate_fraction(value: object) -> float | None:
    """Interpreta una fracción de ffprobe ('30000/1001', '30/1') como float.

    Devuelve None cuando falta, no se puede interpretar o el resultado no es
    positivo.
    """
    text = str(value or "").strip()
    if not text:
        return None
    numerator_txt, _, denominator_txt = text.partition("/")
    try:
        numerator = float(numerator_txt)
        denominator = float(denominator_txt) if denominator_txt else 1.0
    except ValueError:
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    return numerator / denominator


def probe_media(path: Path) -> MediaInfo:
    """Sondea ``path`` con ffprobe y devuelve el MediaInfo analizado.

    Lanza:
        MediaError: Si la ruta no existe, no es un archivo, no admite
            sondeo o no contiene un stream de video utilizable.
    """
    if not path.exists():
        raise MediaError(f"file not found: {path}")
    if not path.is_file():
        raise MediaError(f"not a regular file: {path}")

    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFPROBE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise MediaError("ffprobe not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffprobe timed out on: {path.name}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:300] or "unknown error"
        raise MediaError(f"ffprobe failed on '{path.name}': {detail}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"could not parse ffprobe output for '{path.name}'") from exc

    streams = data.get("streams", [])
    video_streams = [
        s
        for s in streams
        if s.get("codec_type") == "video"
        and not _safe_int(s.get("disposition", {}).get("attached_pic"))
    ]
    if not video_streams:
        raise MediaError(f"no video stream found in '{path.name}'")
    video = video_streams[0]
    audio = [s for s in streams if s.get("codec_type") == "audio"]

    fmt = data.get("format", {})
    duration = _to_float(fmt.get("duration")) or _to_float(video.get("duration"))
    try:
        size = int(fmt.get("size"))
    except (TypeError, ValueError):
        size = path.stat().st_size

    fps = _parse_rate_fraction(video.get("avg_frame_rate"))
    if fps is None:
        fps = _parse_rate_fraction(video.get("r_frame_rate"))

    return MediaInfo(
        path=path,
        width=_safe_int(video.get("width")),
        height=_safe_int(video.get("height")),
        video_codec=str(video.get("codec_name") or ""),
        duration_s=duration,
        fps=fps,
        size_bytes=size,
        audio_streams=audio,
    )


# --------------------------------------------------------------------------- #
# Resolución de políticas (escalado / audio / bitrate)
# --------------------------------------------------------------------------- #


def _even_size(width: float, height: float) -> tuple[int, int]:
    """Redondea dimensiones al entero par más cercano (mínimo 2).

    Con cotas pares, el redondeo hacia arriba del ajuste de paridad nunca
    supera la cota correspondiente.
    """
    out_w = max(2, int(round(width)))
    if out_w % 2:
        out_w += 1
    out_h = max(2, int(round(height)))
    if out_h % 2:
        out_h += 1
    return out_w, out_h


def target_size_for(
    policy_name: str, source_width: int, source_height: int
) -> tuple[int, int]:
    """Devuelve las dimensiones de salida (ancho, alto) de la política.

    Para políticas con ``max_short_side`` ajusta la fuente a la caja de la
    política (lado corto ``max_short_side``, lado largo ``max_long_side`` si
    está definido; si no, el mismo tope del lado corto) con un único factor
    de escala común y sin reescalar hacia arriba; como ambas cotas son pares,
    la salida nunca supera su tope. Con ``max_height`` aplica el tope clásico
    de altura (mismo redondeo a par). Para políticas sin tope devuelve las
    dimensiones de la fuente sin cambios.
    """
    policy = RES_POLICIES[policy_name]
    if source_width <= 0 or source_height <= 0:
        return source_width, source_height
    if policy.max_short_side is not None:
        short_bound = policy.max_short_side
        long_bound = (
            policy.max_long_side
            if policy.max_long_side is not None
            else policy.max_short_side
        )
        scale = min(
            short_bound / min(source_width, source_height),
            long_bound / max(source_width, source_height),
            1.0,
        )
        if scale >= 1.0:
            return source_width, source_height
        return _even_size(source_width * scale, source_height * scale)
    if policy.max_height is not None and source_height > policy.max_height:
        scale = policy.max_height / source_height
        return _even_size(source_width * scale, source_height * scale)
    return source_width, source_height


def scale_args_for(
    policy_name: str, source_width: int, source_height: int
) -> tuple[list[str], str]:
    """Devuelve (argumentos ffmpeg, descripción legible) de la política de
    resolución.

    Con ``max_short_side`` se ajusta la fuente a la caja estándar de la
    política (lado corto y lado largo a la vez), conservando el aspecto y sin
    reescalar hacia arriba; con ``max_height`` se aplica el tope clásico
    de altura.
    """
    policy = RES_POLICIES[policy_name]
    out_w, out_h = target_size_for(policy_name, source_width, source_height)
    if (out_w, out_h) == (source_width, source_height):
        if policy.max_short_side is not None:
            return [], (
                f"none (source {source_width}x{source_height} fits within "
                f"{policy.max_long_side or policy.max_short_side}x"
                f"{policy.max_short_side} bounds)"
            )
        return [], f"none (source height {source_height}, no scaling)"
    vf = f"scale={out_w}:{out_h}:flags=lanczos"
    return ["-vf", vf], f"{vf} ({source_width}x{source_height} -> {out_w}x{out_h}; never upscale)"


def audio_args_for(audio_streams: list[dict]) -> tuple[list[str], str]:
    """Devuelve (argumentos ffmpeg, descripción legible) de la política de audio.

    Copia directa de streams solo cuando cada stream de audio es AAC con un
    bitrate conocido a ``AUDIO_COPY_MAX_BITRATE`` o por debajo; en caso
    contrario, re-codifica a AAC a 128k (hasta estéreo) o 256k (más de
    2 canales).
    """
    if not audio_streams:
        return [], "none (no audio streams)"

    def copyable(stream: dict) -> bool:
        if stream.get("codec_name") != "aac":
            return False
        bitrate = _to_float(stream.get("bit_rate"))
        return bitrate is not None and bitrate <= AUDIO_COPY_MAX_BITRATE

    if all(copyable(s) for s in audio_streams):
        return [
            "-c:a",
            "copy",
        ], f"stream copy (all AAC <= {AUDIO_COPY_MAX_BITRATE // 1000} kbit/s)"

    max_channels = max((_safe_int(s.get("channels")) for s in audio_streams), default=0)
    bitrate = "256k" if max_channels > 2 else "128k"
    return [
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
    ], f"re-encode AAC {bitrate} (max {max_channels or '?'} ch)"


def bitrate_cap_args_for(
    out_w: int, out_h: int, fps: float | None
) -> tuple[list[str], str]:
    """Devuelve (argumentos ffmpeg, descripción legible) del tope de bitrate.

    Calcula un techo de densidad de bits (``MAX_BITS_PER_PIXEL`` por píxel y
    por cuadro) sobre el tamaño de salida, con piso ``MIN_MAXRATE_BPS``, y lo
    aplica como ``-maxrate``/``-bufsize`` detrás del objetivo de calidad del
    encoder. Sin fps conocido (o con dimensiones inválidas) no se aplica
    nada: manda el objetivo de calidad.
    """
    if fps is None or fps <= 0 or out_w <= 0 or out_h <= 0:
        return [], "none (fps unknown; encoder quality target only)"
    rate = max(MAX_BITS_PER_PIXEL * out_w * out_h * fps, MIN_MAXRATE_BPS)
    maxrate_k = max(1, round(rate / 1000))
    bufsize_k = max(2, round(rate * MAXRATE_BUFFER_MULT / 1000))
    bpp = rate / (out_w * out_h * fps)
    desc = f"cap {maxrate_k}k video bitrate ({bpp:.3f} bpp at {out_w}x{out_h}x{fps:.2f})"
    return ["-maxrate", f"{maxrate_k}k", "-bufsize", f"{bufsize_k}k"], desc


# --------------------------------------------------------------------------- #
# Detección de encoders
# --------------------------------------------------------------------------- #


def probe_encoder(encoder: EncoderSpec) -> bool:
    """Devuelve True cuando ``encoder`` abre y codifica un mini clip de prueba."""
    if encoder.probe_args is None:  # respaldo de CPU: sin sondeo, siempre disponible
        return True
    cmd = [
        FFMPEG,
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x256:r=30:d=0.3",
        "-c:v",
        encoder.name,
        *encoder.probe_args,
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=ENCODER_PROBE_TIMEOUT_S
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def pick_encoder(options: ConvertOptions) -> EncoderSpec:
    """Resuelve el encoder de la corrida y muestra la elección.

    Por defecto usa el respaldo de CPU; solo con ``--hw`` sondea los
    encoders por hardware registrados para el codec.
    """
    spec = CODECS[options.codec]
    fallback = spec.cpu_fallback

    if not options.hw:
        # Camino por defecto: la CPU, sin sondeo de hardware.
        if fallback is None:
            raise SystemExit(
                f"ERROR codec '{options.codec}' has no usable encoder configured"
            )
        say(
            f"Encoder: {fallback.name} ({fallback.description}; "
            "CPU encoder is the default (pass --hw to enable hardware detection))"
        )
        return fallback

    if not spec.hw_probe_order:
        # Con --hw explícito, un codec sin tabla de hardware informa el salto.
        if fallback is None:
            raise SystemExit(
                f"ERROR codec '{options.codec}' has no usable encoder configured"
            )
        say(
            f"Encoder: {fallback.name} ({fallback.description}; no hardware table, "
            "detection skipped)"
        )
        return fallback

    for encoder in spec.hw_probe_order:
        available = probe_encoder(encoder)
        say(
            f"  probe {encoder.name:<11} -> {'available' if available else 'not available'}"
        )
        if available:
            say(f"Encoder: {encoder.name} ({encoder.description}; hardware detected)")
            return encoder

    if fallback is None:
        raise SystemExit(
            f"ERROR codec '{options.codec}' has no CPU fallback encoder configured"
        )
    say(
        f"Encoder: {fallback.name} ({fallback.description}; no hardware encoder detected)"
    )
    return fallback


def _replace_flag_value(
    args: tuple[str, ...], flag: str, value: str
) -> tuple[str, ...]:
    """Devuelve una tupla nueva con el valor que sigue a ``flag`` reemplazado.

    Si ``flag`` no está presente, la tupla se devuelve sin cambios.
    """
    if flag not in args:
        return args
    index = args.index(flag)
    if index + 1 >= len(args):
        return args
    return (*args[: index + 1], value, *args[index + 2 :])


def build_ffmpeg_command(
    src: Path,
    dst: Path,
    encoder: EncoderSpec,
    speed: str,
    scale_args: Sequence[str],
    audio_args: Sequence[str],
    crf: int | None = None,
    bitrate_cap_args: Sequence[str] = (),
) -> list[str]:
    """Ensambla la lista completa de argumentos de ffmpeg para una conversión."""
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
    ]
    cmd.extend(scale_args)
    cmd.extend(["-c:v", encoder.name])
    video_args = encoder.video_args(speed)
    if crf is not None:
        video_args = _replace_flag_value(video_args, "-crf", str(crf))
    cmd.extend(video_args)
    cmd.extend(bitrate_cap_args)
    cmd.extend(audio_args)
    cmd.extend(["-c:s", "mov_text", "-movflags", "+faststart", str(dst)])
    return cmd


# --------------------------------------------------------------------------- #
# Renderizado de progreso
# --------------------------------------------------------------------------- #


def _parse_speed(value: str) -> float | None:
    """Interpreta un valor de velocidad de progreso ffmpeg como '1.53x' o 'N/A'."""
    try:
        return float(value.strip().removesuffix("x"))
    except ValueError:
        return None


def draw_progress(
    out_time_s: float, duration_s: float | None, speed: float | None
) -> None:
    """Redibuja el indicador de progreso de una sola línea con \\r."""
    speed_txt = f"{speed:.2f}x" if speed else "n/a"
    if duration_s and duration_s > 0:
        pct = max(0.0, min(100.0, out_time_s / duration_s * 100.0))
        filled = _safe_int(pct / 100.0 * PROGRESS_BAR_WIDTH)
        bar = "#" * filled + "-" * (PROGRESS_BAR_WIDTH - filled)
        if speed and speed > 0:
            eta_s = max(0.0, (duration_s - out_time_s) / speed)
            eta_txt = f"ETA {format_clock(eta_s)}"
        else:
            eta_txt = "ETA --:--"
        line = f"\r  [{bar}] {pct:5.1f}%  speed {speed_txt}  {eta_txt}   "
    else:
        line = f"\r  encoded {format_clock(out_time_s)}  speed {speed_txt}   "
    sys.stdout.write(line)
    sys.stdout.flush()


def run_ffmpeg_with_progress(
    cmd: list[str], duration_s: float | None
) -> tuple[int, str]:
    """Ejecuta una conversión con ffmpeg, mostrando progreso en vivo por stdout.

    Devuelve:
        (returncode, cola de stderr), donde la cola de stderr es útil para
        reportar errores tras una codificación fallida.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    stderr_tail: list[str] = []

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())
            if len(stderr_tail) > 60:
                del stderr_tail[: len(stderr_tail) - 60]

    drain = threading.Thread(target=_drain_stderr, daemon=True)
    drain.start()

    out_time_s = 0.0
    speed: float | None = None
    saw_us = False
    drew = False
    last_draw = 0.0
    try:
        for raw in proc.stdout or ():
            line = raw.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "out_time_us":
                saw_us = True
                out_time_s = (_to_float(value) or 0.0) / 1_000_000
            elif key == "out_time_ms" and not saw_us:
                # Clave heredada: ffmpeg emite microsegundos pese al nombre.
                out_time_s = (_to_float(value) or 0.0) / 1_000_000
            elif key == "speed":
                speed = _parse_speed(value)
            now = time.monotonic()
            if now - last_draw >= PROGRESS_MIN_INTERVAL_S:
                last_draw = now
                draw_progress(out_time_s, duration_s, speed)
                drew = True
    finally:
        returncode = proc.wait()
        drain.join(timeout=5)
    if drew:
        say()
    return returncode, " ".join(stderr_tail)[-600:]


# --------------------------------------------------------------------------- #
# Pipeline por archivo
# --------------------------------------------------------------------------- #


def convert_one(src: Path, options: ConvertOptions, encoder: EncoderSpec) -> FileResult:
    """Convierte un único archivo según ``options``. Nunca lanza excepciones."""
    say(f"--- {src.name}")
    try:
        info = probe_media(src)
    except MediaError as exc:
        say(f"ERROR {exc}")
        return FileResult(src=src, dst=None, status="failed", detail=str(exc))

    dst = src.with_name(f"{src.stem}{options.suffix}.mp4")
    if dst == src:
        detail = "output path equals source path; refusing to overwrite the input"
        say(f"ERROR {detail}")
        return FileResult(src=src, dst=dst, status="failed", detail=detail)

    if dst.exists() and not options.force:
        detail = f"output already exists (use --force to re-encode): {dst.name}"
        say(f"SKIP {detail}")
        return FileResult(
            src=src, dst=dst, status="skipped", detail=detail, in_bytes=info.size_bytes
        )

    scale_args, scale_desc = scale_args_for(options.res_policy, info.width, info.height)
    audio_args, audio_desc = audio_args_for(info.audio_streams)
    out_w, out_h = target_size_for(options.res_policy, info.width, info.height)
    cap_args, cap_desc = bitrate_cap_args_for(out_w, out_h, info.fps)

    if options.dry_run:
        say("DRY-RUN plan:")
        encoder_txt = f"{encoder.name} ({encoder.description})"
        if encoder.name == "libx265":
            encoder_txt += f", crf {options.crf}"
        say(f"  encoder : {encoder_txt}")
        say(
            f"  video   : {info.video_codec} {info.width}x{info.height}, "
            f"{format_clock(info.duration_s)}, {human_mb(info.size_bytes)} MB"
        )
        say(f"  scale   : {scale_desc}")
        say(f"  bitrate : {cap_desc}")
        say(f"  audio   : {audio_desc}")
        say("  subs    : mov_text (converted when present)")
        say(f"  output  : {dst}")
        return FileResult(
            src=src,
            dst=dst,
            status="ok",
            detail="dry-run",
            in_bytes=info.size_bytes,
            encoder_used=encoder.name,
        )

    say(
        f"  source: {info.width}x{info.height} {info.video_codec}, "
        f"{human_mb(info.size_bytes)} MB -> {dst.name}"
    )
    started = time.monotonic()
    crf = options.crf if encoder.name == "libx265" else None
    cmd = build_ffmpeg_command(
        src,
        dst,
        encoder,
        options.speed,
        scale_args,
        audio_args,
        crf=crf,
        bitrate_cap_args=cap_args,
    )
    returncode, err_tail = run_ffmpeg_with_progress(cmd, info.duration_s)
    used = encoder

    # Reintento único automático con el respaldo de CPU cuando falla la
    # codificación por hardware; caer al respaldo aquí también activa el
    # fusible a nivel de corrida de main (el resto de los archivos pasa a
    # CPU en lugar de reintentar el hardware en cada archivo).
    fallback = CODECS[options.codec].cpu_fallback
    if returncode != 0 and fallback is not None and used.name != fallback.name:
        say(
            f"WARN {used.name} failed on this file; retrying once with {fallback.name} (CPU fallback)."
        )
        _remove_quietly(dst)
        crf = options.crf if fallback.name == "libx265" else None
        cmd = build_ffmpeg_command(
            src,
            dst,
            fallback,
            options.speed,
            scale_args,
            audio_args,
            crf=crf,
            bitrate_cap_args=cap_args,
        )
        returncode, err_tail = run_ffmpeg_with_progress(cmd, info.duration_s)
        used = fallback

    elapsed = time.monotonic() - started
    if returncode != 0:
        _remove_quietly(dst)
        detail = f"{used.name} failed: {err_tail or 'ffmpeg reported no error output'}"
        say(f"ERROR {detail}")
        return FileResult(
            src=src,
            dst=dst,
            status="failed",
            detail=detail,
            in_bytes=info.size_bytes,
            elapsed_s=elapsed,
            encoder_used=used.name,
        )

    out_bytes = dst.stat().st_size
    saved_pct = (1.0 - out_bytes / info.size_bytes) * 100.0 if info.size_bytes else 0.0
    factor = info.duration_s / elapsed if info.duration_s and elapsed > 0 else None
    speed_txt = f" @ {factor:.2f}x" if factor else ""
    say(
        f"OK {human_mb(info.size_bytes)} MB -> {human_mb(out_bytes)} MB "
        f"({saved_pct:.1f}% saved) | {elapsed:.1f}s | {used.name}{speed_txt}"
    )
    return FileResult(
        src=src,
        dst=dst,
        status="ok",
        detail=used.name,
        in_bytes=info.size_bytes,
        out_bytes=out_bytes,
        elapsed_s=elapsed,
        speed_factor=factor,
        encoder_used=used.name,
    )


def summarize(results: Sequence[FileResult], options: ConvertOptions) -> None:
    """Imprime el resumen TOTAL final de la corrida."""
    ok = [r for r in results if r.status == "ok"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]
    say("-" * 60)
    if options.dry_run:
        say(
            f"TOTAL (dry-run): {len(ok)} planned, {len(failed)} failed, "
            f"{len(skipped)} skipped - nothing was encoded"
        )
        return
    in_bytes = sum(r.in_bytes for r in ok)
    out_bytes = sum(r.out_bytes for r in ok)
    saved_pct = (1.0 - out_bytes / in_bytes) * 100.0 if in_bytes else 0.0
    say(
        f"TOTAL: {len(ok)} ok, {len(failed)} failed, {len(skipped)} skipped | "
        f"{human_mb(in_bytes)} MB -> {human_mb(out_bytes)} MB ({saved_pct:.1f}% saved)"
    )


# --------------------------------------------------------------------------- #
# Resolución de entradas y selector interactivo
# --------------------------------------------------------------------------- #


def list_videos(directory: Path, suffix: str) -> list[Path]:
    """Lista los videos convertibles de ``directory``, ordenados por nombre."""
    if not directory.is_dir():
        return []
    files = [
        p
        for p in directory.iterdir()
        if p.is_file()
        and p.suffix.lower().lstrip(".") in VIDEO_EXTENSIONS
        and not p.stem.endswith(suffix)
    ]
    return sorted(files, key=lambda p: p.name.lower())


def parse_selection(text: str, count: int) -> list[int] | None:
    """Interpreta una selección del selector como '1 3 5-7' en índices base 0.

    Devuelve None cuando la selección está vacía o es inválida.
    """
    indices: set[int] = set()
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if match:
            low, high = _safe_int(match.group(1)), _safe_int(match.group(2))
            if low > high:
                low, high = high, low
            if low < 1 or high > count:
                return None
            indices.update(range(low - 1, high))
            continue
        if token.isdigit():
            number = _safe_int(token)
            if number < 1 or number > count:
                return None
            indices.add(number - 1)
            continue
        return None
    return sorted(indices) if indices else None


def interactive_select(directory: Path, suffix: str) -> list[Path]:
    """Selecciona videos de ``directory`` en forma interactiva vía input()."""
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
    videos = list_videos(directory, suffix)
    if not videos:
        say(
            f"Input folder '{directory}' is missing or has no video files (created if needed)."
        )
        say("Put videos there, or pass full paths as arguments, e.g.:")
        say('  python convert.py "C:\\path\\to\\my video.mp4"')
        return []

    say(f"Videos in {directory} (files already ending in '{suffix}' excluded):")
    for index, path in enumerate(videos, 1):
        say(f"  {index:>2}. {path.name}  ({human_mb(path.stat().st_size)} MB)")
    while True:
        try:
            raw = input("Select files (e.g. '1 3 5-7', 'a'=all, 'q'=quit): ").strip()
        except EOFError:
            return []
        lowered = raw.lower()
        if lowered in ("q", "quit"):
            return []
        if lowered in ("a", "all"):
            return videos
        indices = parse_selection(raw, len(videos))
        if not indices:
            say(
                "Invalid selection. Use numbers/ranges like '1 3 5-7', 'a' for all, 'q' to quit."
            )
            continue
        return [videos[i] for i in indices]


def resolve_inputs(
    input_dir: Path, raw_inputs: Sequence[str], suffix: str
) -> list[Path]:
    """Devuelve la lista deduplicada de archivos a convertir."""
    if raw_inputs:
        seen: dict[Path, None] = {}
        for raw in raw_inputs:
            seen.setdefault(Path(raw).expanduser().resolve(), None)
        return list(seen)
    return interactive_select(input_dir, suffix)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Construye el parser de CLI con argparse a partir de los registros."""
    parser = argparse.ArgumentParser(
        prog="convert.py",
        description=(
            "Convert videos to size-minimized HEVC MP4 files written next to "
            "each source as <stem><suffix>.mp4."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        metavar="INPUT",
        help="video files to convert; omit to pick interactively from --input-dir",
    )
    parser.add_argument(
        "--input-dir",
        default="input",
        metavar="DIR",
        help="folder scanned by the interactive picker",
    )
    parser.add_argument(
        "--codec",
        choices=tuple(CODECS),
        default="hevc",
        help="target video codec",
    )
    parser.add_argument(
        "--res",
        choices=tuple(RES_POLICIES),
        default="short480",
        help="resolution policy",
    )
    parser.add_argument(
        "--speed",
        choices=tuple(SPEED_PROFILES),
        default="fast",
        help="encoding speed profile",
    )
    parser.add_argument(
        "--suffix", default="_m", help="suffix for generated filenames (before .mp4)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-encode even if the output file already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the per-file plan (encoder, scale, audio, output) without encoding",
    )
    parser.add_argument(
        "--hw",
        "--gpu",
        dest="hw",
        action="store_true",
        help=(
            "enable hardware encoder detection (NVENC/QSV/AMF); "
            "CPU (libx265) is the default"
        ),
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=28,
        metavar="N",
        help=(
            "libx265 CRF quality: higher = smaller file, lower = better "
            "quality; applies to the CPU encoder only"
        ),
    )
    return parser


def validate_registry_choices(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Rechaza valores registrados pero sin implementar, con errores claros."""
    checks = (
        ("--codec", CODECS, args.codec),
        ("--res", RES_POLICIES, args.res),
        ("--speed", SPEED_PROFILES, args.speed),
    )
    for flag, table, value in checks:
        entry = table[value]
        if not entry.implemented:
            available = [name for name, spec in table.items() if spec.implemented]
            parser.error(
                f"{flag} {value!r} is registered but not yet supported by this "
                f"build (available: {', '.join(available)})"
            )


def ensure_ffmpeg_available() -> bool:
    """Verifica que ffmpeg y ffprobe estén disponibles en PATH."""
    missing = [
        label
        for label, exe in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE))
        if shutil.which(exe) is None
    ]
    if missing:
        say(f"ERROR required tool(s) not found in PATH: {', '.join(missing)}.")
        say("Install ffmpeg (with ffprobe) and make sure it is on PATH.")
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada de la CLI. Devuelve el código de salida del proceso
    (0 ok, 1 cualquier fallo).
    """
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_registry_choices(parser, args)
    if not 0 <= args.crf <= 51:
        parser.error(f"--crf {args.crf} is out of range; expected 0-51")
    if not ensure_ffmpeg_available():
        return 1

    options = ConvertOptions(
        codec=args.codec,
        res_policy=args.res,
        speed=args.speed,
        suffix=args.suffix,
        force=args.force,
        dry_run=args.dry_run,
        hw=args.hw,
        crf=args.crf,
    )
    inputs = resolve_inputs(
        Path(args.input_dir).expanduser(), args.inputs, options.suffix
    )
    if not inputs:
        return 0

    encoder = pick_encoder(options)
    fallback = CODECS[options.codec].cpu_fallback
    results: list[FileResult] = []
    for src in inputs:
        result = convert_one(src, options, encoder)
        results.append(result)
        if (
            fallback is not None
            and encoder.name != fallback.name
            and result.encoder_used == fallback.name
        ):
            # Fusible: un solo fallo del hardware pasa el resto de la
            # corrida al respaldo de CPU, en vez de reintentarlo archivo
            # por archivo.
            say(
                f"NOTE switching the rest of the run to {fallback.name} "
                "(hardware encoder failed above)."
            )
            encoder = fallback
    summarize(results, options)
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("Interrupted.")
        sys.exit(130)
