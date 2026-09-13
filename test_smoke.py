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
    espacios en el nombre) a HEVC 720p.
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
        "1080p: height scaled to 720",
        vs.get("height") == 720,
        f"height={vs.get('height')}",
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


def test_dry_run(src: Path) -> None:
    """Paso 4 de la especificación: --dry-run imprime un plan y no crea
    ningún archivo de salida.
    """
    out = src.with_name("dry run clip_m.mp4")
    proc = run_converter(["--dry-run", str(src)])
    check("dry-run: exits 0", proc.returncode == 0, fail_detail(proc))
    check("dry-run: prints a plan", "DRY-RUN plan:" in proc.stdout, proc.stdout[-400:])
    check("dry-run: mentions chosen encoder", "encoder" in proc.stdout.lower())
    check("dry-run: creates no output file", not out.exists(), str(out))
    check(
        "dry-run: CPU encoder is the default",
        "libx265" in proc.stdout,
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

        dry = TMP / "dry run clip.mp4"
        print(f"Generating dry-run sample: {dry.name}")
        make_sample(dry, "320x240", 2, 64)
        test_dry_run(dry)

        test_skip_and_force(small, out_small)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\nRESULT: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
