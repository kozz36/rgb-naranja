# Panel OLED Thermalright

Esta guía describe el dashboard de usuario para el panel USB `87ad:70db`.
Cubre la composición Python/Qt, el modo GPU opcional y el codificador JPEG Rust;
no cambia OpenRGB, el monitor Naga ni los perfiles RGB existentes.

> **Estado:** documenta código fuente y validaciones; no afirma que una instalación concreta esté operativa. La salud de panel, USB y servicio se verifica por separado.

## Alcance y límites

- Cada imagen tiene **1600×720** píxeles y el bucle despierto apunta a **24 Hz**: cadencia objetivo del host, no FPS físico ni frecuencia interna del panel.
- El dashboard se ejecuta como usuario, nunca como root.
- El servicio usa `TRCC_DAEMON=0` y Qt `offscreen`; su instalador no toca OpenRGB, Naga ni configuración global.
- El dispositivo usa la clave `87ad:70db`; una prueba física abre USB y requiere coordinación con quien opera el servicio.

## Arquitectura

| Componente | Responsabilidad | Límite |
|---|---|---|
| `thermalright-dashboard.py` | Bucle, panel clásico, TRCC y DPMS | Carga TRCC sólo en modo vivo |
| `thermalright_cinematic.py` | Composición cinematográfica CPU/Qt | Alternativa CPU explícita |
| `thermalright_cinematic_gpu.py` | Campo fluido OpenGL 3.3 fuera de pantalla | Requiere hardware OpenGL |
| `thermalright_jpeg.py` | Cliente stdlib del proceso JPEG | No inicia el hijo al importarse |
| `rust-dashboard-jpeg` | JPEG persistente por FFI a TurboJPEG | No reemplaza UI, sensores ni TRCC |

El UI principal es Python con PySide6: en vivo consulta el mapeo crudo `read_all` de TRCC y envía la imagen al panel. Datos, composición Qt y transporte siguen siendo Python; Rust sólo codifica JPEG RGBA opcionalmente, no es una reescritura completa.

### Telemetría y gráficos

- CPU y GPU muestran temperatura, uso y potencia finita; ausente, inválida o no estimable es `N/A`, mientras que `0 W` es válido.
- Las temperaturas conservan cinco minutos; CPU y GPU escalan independientemente y una muestra inválida o hueco corta el trazo.
- RAM muestra usado/total válido. Los vatios son telemetría disponible, no una estimación de energía ni garantía de consumo.

### DPMS y espera

El proveedor KDE consulta `kscreen-doctor --dpms show`. Sólo entra en espera si **todos** los monitores reportan `off`; una salida encendida implica despierto y un resultado incompleto, duplicado o desconocido conserva estado y cadencia confirmados.

En espera conserva un JPEG negro Qt en caché y lo reenvía a 1 Hz, sin sensores, render GPU ni JPEG Rust. Al despertar registra un hueco en ambos historiales. La prueba física larga de apagado/encendido de todos los monitores sigue pendiente.

## Renderizadores

El CLI tiene tres opciones de `--renderer`:

| Opción | Uso |
|---|---|
| `classic` | Predeterminada; dashboard CPU/Qt clásico |
| `cinematic` | Composición cinematográfica CPU/Qt |
| `cinematic-gpu` | Campo fluido con el sidecar OpenGL y composición Qt |

`cinematic-gpu` crea un contexto Qt OpenGL core 3.3 fuera de pantalla y rechaza software como llvmpipe. Si fallan contexto, FBO o shaders, **no hay fallback automático a CPU**: seleccione explícitamente `classic` o `cinematic`.

## Dependencias y entorno observado

El instalador valida los módulos `trcc`, `PySide6` y `numpy` sin iniciar TRCC. `importlib.metadata` verificó la distribución **`trcc-linux` 9.9.12**: ése es el nombre para `pip`; el módulo se importa como `trcc`.

| Área | Requisito |
|---|---|
| Base | Python 3, PySide6, numpy y `trcc-linux` |
| DPMS | KDE `kscreen-doctor` para conocer el estado; si falla, queda desconocido |
| GPU | Controlador OpenGL de hardware compatible con core 3.3 |
| JPEG Rust | Compilador Cargo/Rust y biblioteca host `libturbojpeg` |
| Exportación WebM | PySide6 y `ffmpeg` disponible en `PATH` |

El entorno usado fue CachyOS, Python 3.14, `trcc-linux` 9.9.12, PySide6 y una RTX 5070 Ti: contexto de prueba, no compatibilidad universal.

### Crear el entorno virtual

Los siguientes son comandos documentados; no se ejecutan como parte de esta
guía. Instalan únicamente dependencias verificadas del dashboard:

```bash
python3 -m venv "$HOME/.local/share/rgb-naranja/trcc-venv"
"$HOME/.local/share/rgb-naranja/trcc-venv/bin/python" -m pip install \
  "trcc-linux==9.9.12" PySide6 numpy
```

## Codificador JPEG Rust opcional

`rust-dashboard-jpeg` recibe RGBA 1600×720 por `stdin` con longitud `u32` big-endian y responde JPEG. Usa Rust stdlib y FFI a `libturbojpeg` host (3.2.0 observada); `Cargo.toml` no declara dependencias Cargo externas.

Compile fuera del repositorio en ruta privada absoluta. Instalador y cliente exigen ejecutable regular propio, sin symlinks ni ancestros escribibles por grupo/otros y con un ancestro privado del usuario; un `target/` `0755` del repositorio puede no ser admitido.

```bash
install -d -m700 "$HOME/.local/share/rgb-naranja/rust-build"
cargo build --release --offline \
  --manifest-path experiments/rust-dashboard-bench/Cargo.toml \
  --target-dir "$HOME/.local/share/rgb-naranja/rust-build"
```

El artefacto resultante es:

```text
$HOME/.local/share/rgb-naranja/rust-build/release/rust-dashboard-jpeg
```

El modo Rust se selecciona sólo con `--renderer cinematic-gpu --continuous` y
`--jpeg-encoder ABSOLUTE_PATH`. El cliente limita entrada y salida, comprueba el
protocolo y aplica un timeout al hijo; no convierte el renderer en una prueba de
causalidad atribuible al lenguaje.

## Vista previa y benchmark sin USB

Estas rutas no arrancan TRCC ni acceden al USB del panel. La vista previa PNG
usa siempre datos sintéticos marcados `DEMO`; sólo `--renderer cinematic` elige
la variante cinematográfica para `--preview`.

```bash
VENV_PYTHON="$HOME/.local/share/rgb-naranja/trcc-venv/bin/python"
"$VENV_PYTHON" scripts/thermalright-dashboard.py \
  --preview "$PWD/thermalright-demo.png"
"$VENV_PYTHON" scripts/thermalright-dashboard.py \
  --renderer cinematic --preview "$PWD/thermalright-cinematic-demo.png"
"$VENV_PYTHON" scripts/preview_thermalright_cinematic.py \
  --output "$PWD/thermalright-cinematic-demo.webm" --fps 24 --duration 5
```

El benchmark compara rutas Qt y Rust con corpus sintético; no compila Cargo y
requiere un binario Rust ya construido. La opción siguiente usa sólo opciones
aceptadas por su ayuda y crea un JSON nuevo (nunca sobrescribe uno existente):

```bash
"$VENV_PYTHON" scripts/benchmark_rust_dashboard_jpeg.py \
  --encoder "$HOME/.local/share/rgb-naranja/rust-build/release/rust-dashboard-jpeg" \
  --output "$PWD/rust-dashboard-report.json" \
  --rounds 3 --frames 120 --timeout-s 10 --rust-subsampling 420
```

En cuatro tramos de 60 s alternando GPU Qt / Rust / Rust / Qt, se observó una
baja de 26.785 a 23.277 ms de CPU por frame (13.1 %). La observación incluye
contabilidad de proceso hijo por cgroup y arranque; no mide potencia, no promete
rendimiento indefinido, no demuestra causalidad por lenguaje ni FPS físico.

## Instalación del servicio de usuario

Revise primero las opciones reales:

```bash
bash scripts/install-thermalright-dashboard.sh --help
```

Sin opciones, el instalador preflighta y publica cinco módulos y la unidad,
crea una copia de seguridad privada y ejecuta `systemctl --user daemon-reload`.
**Sí escribe** los módulos y la unidad, pero no detiene, activa, habilita ni
reinicia un servicio; no es un modo dry-run. Sin `--jpeg-encoder`, la unidad
publicada conserva GPU+Qt.

```bash
bash scripts/install-thermalright-dashboard.sh
```

Con autorización explícita del operador para detener/activar el servicio, el
modo Rust copia un ELF admitido a una ruta administrada privada y transforma
sólo la unidad staged para usar esa copia fija. El instalador lee y valida el
binario fuente; nunca lo ejecuta por sí mismo antes de que el servicio arranque.

```bash
bash scripts/install-thermalright-dashboard.sh \
  --jpeg-encoder "$HOME/.local/share/rgb-naranja/rust-build/release/rust-dashboard-jpeg" \
  --start
```

`--start` detiene una unidad que ya estaba activa antes de publicar, habilita e
inicia la unidad completa y comprueba durante tres segundos que siga activa con
un `MainPID` estable. Esa comprobación no prueba USB ni salud del panel. Un
fallo ordinario posterior a la publicación, o `HUP`/`INT`/`TERM`, intenta
restaurar el bundle y el estado previo; `SIGKILL` no puede garantizar rollback.

### Copias de seguridad y rollback

Cada instalación exitosa imprime una línea `BACKUP_DIR=/ruta/absoluta`. Guarde
esa salida y use **esa ruta exacta**, no una ruta histórica elegida a mano:

```bash
BACKUP_DIR="/ruta/absoluta/impresa-por-el-instalador"
bash scripts/install-thermalright-dashboard.sh --rollback "$BACKUP_DIR"
```

Las copias nuevas usan manifiesto v3. El rollback conserva compatibilidad de
lectura con v1 y v2, pero antes de detener el servicio exige un hijo directo del
directorio privado de backups, propietarios correctos, modos privados, ausencia
de symlinks, manifiesto fijo y hashes íntegros. El par binario/manifiesto Rust debe
estar completo o ausente según el respaldo; los respaldos anteriores a Rust no lo
requieren. Cualquier desajuste se rechaza antes de parar el servicio.

## Permiso USB activo por asiento

La regla incluida es `udev/70-rgb-naranja-thermalright.rules`. Coincide sólo con
`87ad:70db`, `SUBSYSTEM=="usb"`, `DEVTYPE=="usb_device"` y añade `TAG+="uaccess"`.
Con ello el usuario de la sesión activa obtiene acceso al panel sin ejecutar el
dashboard como root. No use bypasses de perfiles inseguros ni triggers globales.

La instalación de una regla es una decisión administrativa opcional. Lea el
archivo exacto antes de aplicarlo; este trabajo no ejecuta ninguna operación de
configuración global:

```bash
less udev/70-rgb-naranja-thermalright.rules
sudo install -o root -g root -m 0644 udev/70-rgb-naranja-thermalright.rules \
  /etc/udev/rules.d/70-rgb-naranja-thermalright.rules
sudo udevadm control --reload-rules
```

Después, reconecte sólo el panel o siga el procedimiento específico de la
distribución. No ejecute `udevadm trigger` de forma global.

## Pruebas

Los trial tools físicos aceptan una duración finita (máximo 60 s) y realmente
abren USB. No los lance junto a un servicio de larga duración: primero coordine
una parada protegida del servicio y su restauración posterior. Por esa razón no
se incluye aquí un comando casual de trial físico.

Las pruebas de software y el test Rust pueden ejecutarse después de preparar el
entorno y el directorio privado de build. Los conteos pueden variar; no son un
contrato público estable.

```bash
VENV_PYTHON="$HOME/.local/share/rgb-naranja/trcc-venv/bin/python"
env QT_QPA_PLATFORM=offscreen TRCC_DAEMON=0 "$VENV_PYTHON" \
  -m unittest discover -s tests -p 'test_thermalright_*.py'
env QT_QPA_PLATFORM=offscreen TRCC_DAEMON=0 TRCC_GPU_TESTS=1 "$VENV_PYTHON" \
  -m unittest discover -s tests -p 'test_cinematic*.py'
"$VENV_PYTHON" -m unittest discover -s tests -p 'test_rust_dashboard_jpeg.py'
cargo test --offline --manifest-path experiments/rust-dashboard-bench/Cargo.toml \
  --target-dir "$HOME/.local/share/rgb-naranja/rust-build"
```

## Operación responsable

1. Empiece por una vista previa `DEMO` o el benchmark offline.
2. Confirme permisos USB y la disponibilidad de OpenGL de hardware antes de GPU.
3. Autorice `--start` sólo cuando sea aceptable detener/restaurar el servicio.
4. Conserve la línea `BACKUP_DIR` de cada instalación para un rollback verificable.
