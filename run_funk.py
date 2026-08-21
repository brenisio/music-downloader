#!/usr/bin/env python3
"""Helper temporário: processa só o download/Funk.csv, isolado do lote principal.

Existe porque run_baixador.py varre TODO o download/*.csv — rodar ele de novo
reprocessaria os CSVs ainda em andamento. Este script chama process_csv apenas
no Funk.csv, com log separado (baixador_funk.log) pra não brigar com baixador.log.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import run_baixador as rb

# PATH do ffmpeg (conda no Windows não coloca Library/bin no PATH)
conda_prefix = os.environ.get("CONDA_PREFIX")
if conda_prefix:
    lib_bin = Path(conda_prefix) / "Library" / "bin"
    if lib_bin.exists() and str(lib_bin) not in os.environ["PATH"]:
        os.environ["PATH"] = str(lib_bin) + os.pathsep + os.environ["PATH"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(rb.ROOT_DIR / "baixador_funk.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

csv_path = rb.DOWNLOAD_DIR / "Funk.csv"
if not csv_path.exists():
    logging.error(f"Não encontrei {csv_path}")
    raise SystemExit(1)

rb.check_dependencies()
success, fail = rb.process_csv(csv_path, "mp3")

dest = rb.PROCESSED_DIR / csv_path.name
if dest.exists():
    dest = rb.PROCESSED_DIR / f"{csv_path.stem}_{datetime.now():%Y%m%d_%H%M%S}.csv"
shutil.move(str(csv_path), str(dest))

logging.info(f"\nFunk concluído: {success} baixadas, {fail} falhas. CSV → {dest}")
