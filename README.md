# py-video-converter

Repositorio: <https://github.com/joriza/py-video-compresor>

CLI de Python en un solo archivo que convierte videos a **archivos HEVC (H.265) MP4
de tamaño minimizado, escritos junto a cada fuente** como `<stem>_m.mp4`. Sin
dependencias: biblioteca estándar de Python más `ffmpeg`/`ffprobe` del sistema.

## Requisitos

- Python 3.11+ (desarrollado y probado en 3.11)
- `ffmpeg` y `ffprobe` en PATH (probado con la build completa de ffmpeg 8.x)
- Seguro para Windows por diseño: los subprocesos siempre se lanzan con listas
  de argumentos (nunca una shell), de modo que los nombres de archivo con
  espacios, paréntesis o unicode se manejan correctamente.

## Uso

```text
python convert.py [OPTIONS] [INPUT ...]
```

- Con una o más rutas `INPUT`: convierte esos archivos (se eliminan duplicados).
- Sin rutas: abre el selector interactivo sobre `--input-dir`.

```bash
# Convertir un solo archivo (salida: "my video_m.mp4" junto a él)
python convert.py "C:\\videos\\my video.mp4"

# Selector interactivo sobre ./input
python convert.py

# Codificar con detección de hardware (por defecto se usa la CPU)
python convert.py --hw clip.avi

# Más compresión en el encoder de CPU (CRF 28 en vez de 24)
python convert.py --crf 28 episode.mkv

# Mostrar el plan sin codificar nada
python convert.py --dry-run episode.mkv

# Re-codificar aunque la salida ya exista
python convert.py --force clip.mov
```

### Opciones

| Opción | Predeterminado | Descripción |
| --- | --- | --- |
| `INPUT ...` | *(ninguno)* | Archivos de video a convertir; omitir para usar el selector. |
| `--input-dir DIR` | `input` | Carpeta que escanea el selector interactivo. |
| `--codec {hevc}` | `hevc` | Codec destino. `av1`/`h264` están registrados pero **aún sin soporte**. |
| `--res {max720}` | `max720` | Política de resolución. `keep`/`smart1080` están registradas pero **aún sin soporte**. |
| `--speed {fast}` | `fast` | Perfil de velocidad. `max`/`ultra` están registrados pero **aún sin soporte**. |
| `--suffix SUFFIX` | `_m` | Sufijo del nombre de archivo de salida (antes de `.mp4`). |
| `--force` | desactivado | Re-codificar aunque el archivo de salida exista. |
| `--dry-run` | desactivado | Imprime el plan por archivo (encoder, escalado, audio, salida) y no codifica nada. |
| `--hw`, `--gpu` | desactivado | Habilita la detección de encoders por hardware (NVENC/QSV/AMF); por defecto se usa la CPU (`libx265`). |
| `--crf N` | `24` | Calidad CRF de `libx265`: más alto = archivo más chico (probar 26-28); aplica solo al encoder de CPU. |

### Probar valores no predeterminados (A/B)

Para experimentar con valores distintos de los predeterminados, combine
`--dry-run` (vista previa del plan), `--crf N` (calidad/tamaño del encoder de
CPU) y `--suffix` para generar salidas comparables lado a lado sin sobrescribir
la salida estándar `<stem>_m.mp4`:

```bash
# Ver el plan sin codificar nada (incluye el CRF elegido)
python convert.py --dry-run --crf 28 "clip.mp4"

# Barrido de CRF: cada variante escribe su propio archivo junto a la fuente
python convert.py --force --crf 26 --suffix _c26 "clip.mp4"
python convert.py --force --crf 28 --suffix _c28 "clip.mp4"

# Probar el encoder de hardware (nota: --crf aplica solo a libx265)
python convert.py --force --hw --suffix _hw "clip.mp4"
```

- Regla práctica del CRF: cada +6 divide el bitrate a la mitad; 26-28 es el
  rango útil para 720p (el valor predeterminado es 24).
- `--crf` solo afecta a `libx265`. Los encoders por hardware (`--hw`) tienen su
  calidad fijada en el registro e ignoran este valor; si un encoder de hardware
  falla y el archivo se reintenta con CPU, ese reintento sí usa el CRF indicado.
- Las opciones registradas pero aún sin soporte (`--codec av1|h264`,
  `--res keep|smart1080`, `--speed max|ultra`) se rechazan con un error claro;
  ver la hoja de ruta.

### Selector interactivo

Lista los videos encontrados en `--input-dir` (ordenados, con tamaños en MB),
omitiendo los archivos cuyo nombre ya termina con el sufijo configurado.
Sintaxis de selección: `1 3 5-7` (espacios o comas, rangos inclusivos),
`a` = todos, `q` = salir. Si la carpeta falta o está vacía, se crea con una guía.

## Cómo funciona

1. **Sondeo** — `ffprobe` (JSON) lee el primer stream de video, los streams de
   audio, la duración y el tamaño. Los archivos faltantes o sin stream de video
   fallan con un error limpio por archivo; la corrida continúa.
2. **Elección de encoder (una vez por corrida)** — por defecto se usa la CPU
   (`libx265`); con `--hw`/`--gpu` se sondean los encoders por hardware en el
   orden `hevc_nvenc` → `hevc_qsv` → `hevc_amf` con una mini codificación de
   prueba 256x256 (timeout de 15 s cada uno); gana el primero disponible; si
   ninguno lo está, la corrida usa el respaldo de CPU `libx265`. La elección
   se muestra por salida estándar.
3. **Codificación de video (velocidad `fast`)** — objetivos de calidad por
   encoder (el CRF de `libx265` se puede ajustar con `--crf N`):
   - `libx265`: `-preset fast -crf 24 -x265-params log-level=error -tag:v hvc1`
   - `hevc_amf`: `-quality balanced -rc qvbr -qvbr_quality_level 26`
   - `hevc_nvenc`: `-preset p6 -tune hq -rc vbr -cq 26 -b:v 0 -spatial-aq 1 -temporal-aq 1 -rc-lookahead 20`
   - `hevc_qsv`: `-preset veryfast -global_quality 26`
4. **Resolución (`max720`)** — las fuentes más altas que 720p reciben
   `-vf scale=-2:720:flags=lanczos`; las fuentes más pequeñas nunca se reescalan.
5. **Audio** — copia directa de streams (`-c:a copy`, sin pérdida) cuando todos
   los streams de audio son AAC a ≤ 160 kbit/s; en caso contrario, se
   re-codifica a AAC a `128k` (≤ 2 canales) o `256k` (> 2 canales). Los
   subtítulos se mapean cuando están presentes y se convierten a `mov_text`.
6. **Salida** — `-map 0:v:0 -map 0:a? -map 0:s?`, MP4 con
   `-movflags +faststart`, escrito junto a la fuente como
   `<stem><suffix>.mp4`. Las salidas existentes se omiten con una advertencia,
   salvo que se pase `--force`.
7. **Resiliencia** — si el encoder por hardware detectado falla en un archivo,
   ese archivo se reintenta una vez automáticamente con `libx265` (queda
   registrado claramente en el log); además, un fusible a nivel de corrida
   pasa el resto de los archivos a `libx265` tras la primera caída al
   respaldo, para no reintentar el hardware en cada archivo.
8. **Progreso** — ffmpeg corre con `-progress pipe:1 -nostats -hide_banner
   -loglevel error`; la herramienta muestra porcentaje, velocidad y ETA en una
   sola línea, seguida de un resumen por archivo (MB entrada → salida,
   % ahorrado, tiempo transcurrido, factor de velocidad de codificación) y una
   línea TOTAL final.

### Códigos de salida

- `0` — todos los archivos convertidos, omitidos o planificados (dry-run) con éxito.
- `1` — al menos un archivo falló (incluye ffmpeg/ffprobe ausentes).

## Pruebas

Desde la raíz del proyecto (usa el Python del venv local):

```bash
.venv/Scripts/python.exe test_smoke.py
```

La suite genera muestras sintéticas 1080p/480p con ffmpeg lavfi dentro de
`tests_tmp/` (se elimina al salir), incluye un nombre de archivo con espacios y
verifica el comportamiento de codec, escalado, audio, tamaño, dry-run, omisión
y forzado (21 verificaciones).

## Historial de cambios

- **2026-09-13**
  - El encoder de CPU (`libx265`) pasa a ser el predeterminado; `--cpu-only` se
    eliminó y la detección de hardware quedó como opt-in (`--hw`/`--gpu`).
  - Nuevo `--crf N` para controlar el punto calidad/tamaño del encoder de CPU.
  - Fusible a nivel de corrida: tras un fallo del encoder de hardware, el resto
    del lote pasa directo a la CPU.
  - Documentación y comentarios del código traducidos al español.
  - Suite de smoke ampliada a 21 verificaciones.

## Limitaciones conocidas

- Los formatos de subtítulos de mapa de bits (p. ej. PGS en MKV) no se pueden
  convertir a `mov_text`; esos archivos fallan, caen al respaldo y siguen
  fallando. Los subtítulos de texto se convierten bien.
- Los archivos cuyos streams de audio carecen de metadatos de bitrate se
  re-codifican en lugar de copiarse (la política de copia requiere AAC con
  ≤ 160 kbit/s conocidos).

## Hoja de ruta

Implementado mediante registros en el tope de `convert.py`; habilitar una
opción futura significa agregar/marcar una entrada del registro:

- Codecs: `av1`, `h264` (registrados, aún sin soporte)
- Políticas de resolución: `keep`, `smart1080`
- Perfiles de velocidad: `max`, `ultra`
