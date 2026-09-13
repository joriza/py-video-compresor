#!/usr/bin/env python3
"""Pruebas de humo para py-video-converter (solo biblioteca estándar).

Ejecutar desde la raíz del proyecto:
    .venv/Scripts/python.exe test_smoke.py

Genera muestras sintéticas pequeñas con ffmpeg lavfi dentro de tests_tmp/
(un nombre de archivo contiene espacios a propósito, para demostrar el
manejo de argumentos seguro para Windows), ejecuta convert.py como
subproceso y verifica las salidas con ffprobe. tests_tmp/ se elimina al
salir.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
CONVERTER = ROOT / "convert.py"
TMP = ROOT / "tests_tmp"

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """Registra e imprime una aserción PASS/FAIL."""
    global passed, failed
    if condition:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        suffix = f" -- {detail}" if detail else ""
        print(f"[FAIL] {name}{suffix}")


def run_converter(
    args: list[str], timeout: float = 600.0
) -> subprocess.CompletedProcess:
    """Ejecuta convert.py como subproceso con argumentos en lista (sin shell)."""
    return subprocess.run(
        [PYTHON, str(CONVERTER), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def make_sample(path: Path, size: str, seconds: float, audio_kbps: int) -> None:
    """Genera una muestra sintética H.264/AAC vía fuentes lavfi de ffmpeg."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=30:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_kbps}k",
            "-shortest",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sample generation failed: {proc.stderr[:400]}")


def ffprobe_json(path: Path) -> dict:
    """Devuelve los metadatos JSON de ffprobe para ``path``."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {proc.stderr[:400]}")
    return json.loads(proc.stdout)


def video_stream(data: dict) -> dict:
    """Devuelve el primer stream de video de un payload JSON de ffprobe."""
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    raise RuntimeError("no video stream in probe output")


def audio_count(data: dict) -> int:
    """Cuenta los streams de audio de un payload JSON de ffprobe."""
    return sum(1 for s in data.get("streams", []) if s.get("codec_type") == "audio")


def fail_detail(proc: subprocess.CompletedProcess) -> str:
    """Construye una cadena compacta de depuración de una corrida del conversor."""
    return (
        f"rc={proc.returncode}\n"
        f"stdout tail:\n{proc.stdout[-1200:]}\n"
        f"stderr tail:\n{proc.stderr[-600:]}"
    )


def test_convert_1080p_with_spaces(src: Path) -> Path:
    """Pasos 1-2 de la especificación: convierte una muestra 1080p (con
    espacios en el nombre) a HEVC dentro del marco estándar 480p (854x480).
    """
    out = src.with_name("sample test_m.mp4")
    proc = run_converter([str(src)])
    check("1080p: convert.py exits 0", proc.returncode == 0, fail_detail(proc))
    check("1080p: output file exists", out.exists(), str(out))
    if not out.exists():
        return out

    data = ffprobe_json(out)
    vs = video_stream(data)
    check(
        "1080p: video codec is HEVC",
        str(vs.get("codec_name", "")).startswith("hevc"),
        f"codec={vs.get('codec_name')}",
    )
    check(
        "1080p: height scaled to 480 (short side cap)",
        vs.get("height") == 480,
        f"height={vs.get('height')}",
    )
    check(
        "1080p: width scaled to 854 (standard 480p)",
        vs.get("width") == 854,
        f"width={vs.get('width')}",
    )
    check("1080p: audio stream present", audio_count(data) >= 1, "no audio stream")
    check(
        "1080p: output smaller than input",
        out.stat().st_size < src.stat().st_size,
        f"out={out.stat().st_size} in={src.stat().st_size}",
    )
    return out


def test_no_upscale_480(src: Path) -> Path:
    """Paso 3 de la especificación: una fuente 854x480 conserva su altura
    (nunca se reescala hacia arriba).
    """
    out = src.with_name("small clip_m.mp4")
    proc = run_converter([str(src)])
    check("480p: convert.py exits 0", proc.returncode == 0, fail_detail(proc))
    check("480p: output file exists", out.exists(), str(out))
    if not out.exists():
        return out
    vs = video_stream(ffprobe_json(out))
    check(
        "480p: height stays 480", vs.get("height") == 480, f"height={vs.get('height')}"
    )
    check(
        "480p: video codec is HEVC",
        str(vs.get("codec_name", "")).startswith("hevc"),
        f"codec={vs.get('codec_name')}",
    )
    return out


def test_portrait_480(src: Path) -> None:
    """Caso real del usuario: una fuente vertical 608x1080 queda dentro del
    marco 480x854 (el lado corto es el ancho), nunca a 270 de ancho.
    """
    out = src.with_name("portrait test_m.mp4")
    proc = run_converter([str(src)])
    check("portrait: convert.py exits 0", proc.returncode == 0, fail_detail(proc))
    check("portrait: output file exists", out.exists(), str(out))
    if not out.exists():
        return
    vs = video_stream(ffprobe_json(out))
    check(
        "portrait: width scaled to 480",
        vs.get("width") == 480,
        f"width={vs.get('width')}",
    )
    check(
        "portrait: height ~854 (aspect kept, long side of the box)",
        isinstance(vs.get("height"), int) and 840 <= vs["height"] <= 864,
        f"height={vs.get('height')}",
    )
    check(
        "portrait: video codec is HEVC",
        str(vs.get("codec_name", "")).startswith("hevc"),
        f"codec={vs.get('codec_name')}",
    )


def test_dry_run(src: Path) -> None:
    """Paso 4 de la especificación: --dry-run imprime un plan y no crea
    ningún archivo de salida.
    """
    out = src.with_name("dry run clip_m.mp4")
    proc = run_converter(["--dry-run", str(src)])
    check("dry-run: exits 0", proc.returncode == 0, fail_detail(proc))
    check("dry-run: prints a plan", "DRY-RUN plan:" in proc.stdout, proc.stdout[-400:])
    check("dry-run: mentions chosen encoder", "encoder" in proc.stdout.lower())
    check(
        "dry-run: mentions bitrate cap",
        "bitrate" in proc.stdout,
        proc.stdout[-400:],
    )
    check("dry-run: creates no output file", not out.exists(), str(out))
    check(
        "dry-run: CPU encoder is the default",
        "libx265" in proc.stdout,
        proc.stdout[-400:],
    )
    check(
        "dry-run: default CRF is 28",
        "crf 28" in proc.stdout,
        proc.stdout[-400:],
    )

    proc = run_converter(["--dry-run", "--crf", "28", str(src)])
    check(
        "dry-run: --crf 28 accepted (exit 0)",
        proc.returncode == 0,
        fail_detail(proc),
    )


def test_skip_and_force(src: Path, out: Path) -> None:
    """Paso 5 de la especificación: omite la salida existente sin --force;
    la re-codifica con él.
    """
    if not out.exists():
        check("skip/force: prerequisite output exists", False, str(out))
        return
    mtime_before = out.stat().st_mtime_ns

    proc = run_converter([str(src)])
    check("skip: exits 0", proc.returncode == 0, fail_detail(proc))
    check("skip: reported SKIP", "SKIP" in proc.stdout, proc.stdout[-400:])
    check("skip: file untouched (no re-encode)", out.stat().st_mtime_ns == mtime_before)

    proc = run_converter(["--force", str(src)])
    check("force: exits 0", proc.returncode == 0, fail_detail(proc))
    check("force: re-encoded (mtime changed)", out.stat().st_mtime_ns != mtime_before)


UNIT_SRC = '''\
"""Verificación unitaria: matemática de políticas y armado del comando.

Corre en un subproceso aparte (python -c) contra el convert.py real; una
aserción fallida corta con traceback y código de salida distinto de cero.
"""

import convert
from pathlib import Path


def args_of(policy: str, w: int, h: int) -> list[str]:
    """Devuelve solo los argumentos ffmpeg de la política de resolución."""
    return convert.scale_args_for(policy, w, h)[0]


# Caja estándar 480p: lado corto <= 480 y lado largo <= 854, sin upscale.
assert args_of("short480", 1920, 1080) == ["-vf", "scale=854:480:flags=lanczos"]
assert args_of("short480", 1280, 512) == ["-vf", "scale=854:342:flags=lanczos"]
assert args_of("short480", 1000, 400) == ["-vf", "scale=854:342:flags=lanczos"]
assert args_of("short480", 640, 480) == []  # ya entra en la caja: no se toca
assert args_of("short480", 608, 1080) == ["-vf", "scale=480:854:flags=lanczos"]
assert args_of("max720", 1920, 1080) == ["-vf", "scale=1280:720:flags=lanczos"]
assert args_of("max720", 640, 480) == []  # 480 <= 720: no se toca
assert convert.target_size_for("short480", 1920, 1080) == (854, 480)
# El tope de bitrate se calcula sobre las dimensiones REALES de salida con
# cualquier política (max720 incluida), no sobre las de origen.
assert convert.target_size_for("max720", 1920, 1080) == (1280, 720)

# Tope de densidad de bits: 0.08 bpp (piso de 300 kbit/s) como -maxrate/-bufsize.
assert convert.bitrate_cap_args_for(854, 480, 30.0)[0] == [
    "-maxrate",
    "984k",
    "-bufsize",
    "1968k",
]
assert convert.bitrate_cap_args_for(320, 240, 30.0)[0] == [
    "-maxrate",
    "300k",
    "-bufsize",
    "600k",
]  # aplica el piso de 300 kbit/s
assert convert.bitrate_cap_args_for(854, 480, None)[0] == []  # sin fps no hay tope

# El tope va después de los argumentos del encoder (CRF) y antes del audio.
spec = convert.CODECS["hevc"].cpu_fallback
cmd = convert.build_ffmpeg_command(
    Path("a.mp4"),
    Path("b.mp4"),
    spec,
    "fast",
    [],
    [],
    crf=28,
    bitrate_cap_args=["-maxrate", "984k", "-bufsize", "1968k"],
)
i_crf = cmd.index("-crf")
i_maxrate = cmd.index("-maxrate")
i_bufsize = cmd.index("-bufsize")
assert cmd[i_crf + 1] == "28"
assert cmd[i_maxrate + 1] == "984k"
assert cmd[i_bufsize + 1] == "1968k"
assert i_crf < i_maxrate < i_bufsize

print("unit ok")
'''


def test_policy_unit() -> None:
    """Verificación unitaria de la matemática de políticas y del armado del
    comando ffmpeg, en un subproceso aparte (una sola verificación de suite;
    el detalle queda en la salida del subproceso si falla).
    """
    proc = subprocess.run(
        [sys.executable, "-c", UNIT_SRC],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120.0,
    )
    check(
        "unit: policy math and command wiring",
        proc.returncode == 0,
        (
            f"rc={proc.returncode}\n"
            f"stdout:\n{proc.stdout[-1200:]}\n"
            f"stderr:\n{proc.stderr[-1200:]}"
        ),
    )


def main() -> int:
    """Ejecuta todas las pruebas de humo y limpia tests_tmp/ al final."""
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    try:
        big = TMP / "sample test.mp4"
        print(f"Generating 1080p sample: {big.name}")
        make_sample(big, "1920x1080", 6, 192)
        test_convert_1080p_with_spaces(big)

        small = TMP / "small clip.mp4"
        print(f"Generating 480p sample: {small.name}")
        make_sample(small, "854x480", 3, 96)
        out_small = test_no_upscale_480(small)

        portrait = TMP / "portrait test.mp4"
        print(f"Generating portrait sample: {portrait.name}")
        make_sample(portrait, "608x1080", 3, 96)
        test_portrait_480(portrait)

        dry = TMP / "dry run clip.mp4"
        print(f"Generating dry-run sample: {dry.name}")
        make_sample(dry, "320x240", 2, 64)
        test_dry_run(dry)

        test_skip_and_force(small, out_small)

        test_policy_unit()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\nRESULT: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
