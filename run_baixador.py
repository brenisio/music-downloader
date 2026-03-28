#!/usr/bin/env python3
"""run_baixador.py — DJ music downloader for Serato.

Usage:
    python run_baixador.py                  # MP3 320kbps (padrão)
    python run_baixador.py --formato mp3    # MP3 320kbps
    python run_baixador.py --formato flac   # FLAC lossless

Coloque os arquivos CSV (exportados do Exportify) na pasta 'download/' e rode o script.
As músicas serão salvas em pastas com o nome do CSV, nomeadas com tonalidade e BPM reais
detectados via análise do áudio baixado.
"""

import argparse
import csv
import logging
import re
import shutil
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

import librosa
import numpy as np

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

ROOT_DIR      = Path(__file__).parent
DOWNLOAD_DIR  = ROOT_DIR / "download"
PROCESSED_DIR = ROOT_DIR / "processed"
LOG_FILE      = ROOT_DIR / "baixador.log"

# Nomes seguros para filesystem (sem # ou b)
KEY_NAMES = ["C", "Csharp", "D", "Dsharp", "E", "F", "Fsharp", "G", "Gsharp", "A", "Asharp", "B"]

# Camelot wheel: (key, mode) → (número, letra)
# Compatíveis: mesmo número (A↔B = relativa), ±1 mesmo lado (quinta acima/abaixo)
CAMELOT_MAP = {
    ("B",      "major"): (1,  "B"), ("Fsharp", "major"): (2,  "B"),
    ("Csharp", "major"): (3,  "B"), ("Gsharp", "major"): (4,  "B"),
    ("Dsharp", "major"): (5,  "B"), ("Asharp", "major"): (6,  "B"),
    ("F",      "major"): (7,  "B"), ("C",      "major"): (8,  "B"),
    ("G",      "major"): (9,  "B"), ("D",      "major"): (10, "B"),
    ("A",      "major"): (11, "B"), ("E",      "major"): (12, "B"),
    ("Gsharp", "minor"): (1,  "A"), ("Dsharp", "minor"): (2,  "A"),
    ("Asharp", "minor"): (3,  "A"), ("F",      "minor"): (4,  "A"),
    ("C",      "minor"): (5,  "A"), ("G",      "minor"): (6,  "A"),
    ("D",      "minor"): (7,  "A"), ("A",      "minor"): (8,  "A"),
    ("E",      "minor"): (9,  "A"), ("B",      "minor"): (10, "A"),
    ("Fsharp", "minor"): (11, "A"), ("Csharp", "minor"): (12, "A"),
}

# Padrão de keys ordenado do mais longo pro mais curto (evita match parcial)
_KEY_PATTERN = "|".join(sorted(KEY_NAMES, key=len, reverse=True))

# Perfis de Krumhansl-Schmuckler para detecção de tonalidade
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

# ---------------------------------------------------------------------------
# Análise de áudio
# ---------------------------------------------------------------------------

def analyze_audio(file_path: Path) -> tuple[str, str, int]:
    """
    Analisa o arquivo de áudio e retorna (key, mode, bpm) reais.
    Usa os primeiros 60s da música para agilizar.

    Retorna:
        key:  ex. 'Fsharp', 'C', 'Asharp'
        mode: 'major' ou 'minor'
        bpm:  inteiro arredondado
    """
    y, sr = librosa.load(str(file_path), mono=True, duration=60, res_type="kaiser_fast")

    # --- BPM ---
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = round(float(np.atleast_1d(tempo)[0]))

    # --- Tonalidade (Krumhansl-Schmuckler) ---
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)

    best_key, best_mode, best_corr = 0, "major", -np.inf
    for k in range(12):
        corr_maj = float(np.corrcoef(chroma_mean, np.roll(_MAJOR_PROFILE, k))[0, 1])
        corr_min = float(np.corrcoef(chroma_mean, np.roll(_MINOR_PROFILE, k))[0, 1])
        if corr_maj > best_corr:
            best_corr, best_key, best_mode = corr_maj, k, "major"
        if corr_min > best_corr:
            best_corr, best_key, best_mode = corr_min, k, "minor"

    return KEY_NAMES[best_key], best_mode, bpm

# ---------------------------------------------------------------------------
# Helpers de nome de arquivo
# ---------------------------------------------------------------------------

def sanitize_filename(text: str) -> str:
    """Converte texto para formato seguro para filesystem."""
    normalized = unicodedata.normalize("NFD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^\w\s-]", "", ascii_text)
    cleaned = re.sub(r"[\s\-]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip("_").lower()[:60]


def get_primary_artist(artist_field: str) -> str:
    """Retorna o primeiro artista (split em ';')."""
    return artist_field.split(";")[0].strip()


def build_output_filename(track: str, artist: str, key: str, mode: str, bpm: int, ext: str) -> str:
    """Ex: 'sultans_of_swing_dire_straits_Fmajor_148bpm.mp3'"""
    track_s  = sanitize_filename(track)
    artist_s = sanitize_filename(get_primary_artist(artist))
    return f"{track_s}_{artist_s}_{key}{mode}_{bpm}bpm.{ext}"


def already_downloaded(output_dir: Path, track: str, artist: str, ext: str) -> bool:
    """Verifica se já existe um arquivo para essa faixa (independente de key/bpm)."""
    prefix = f"{sanitize_filename(track)}_{sanitize_filename(get_primary_artist(artist))}_"
    return any(f.name.startswith(prefix) and f.suffix == f".{ext}" for f in output_dir.iterdir())

# ---------------------------------------------------------------------------
# Verificação de dependências
# ---------------------------------------------------------------------------

def check_dependencies() -> None:
    """Verifica se yt-dlp e ffmpeg estão instalados."""
    for tool in ["yt-dlp", "ffmpeg"]:
        path = shutil.which(tool)
        if path is None:
            raise EnvironmentError(
                f"'{tool}' não encontrado no PATH.\n"
                f"  yt-dlp : pip install yt-dlp\n"
                f"  ffmpeg (conda) : conda install -c conda-forge ffmpeg\n"
                f"  ffmpeg (Windows) : winget install ffmpeg\n"
                f"  ffmpeg (Ubuntu)  : sudo apt install ffmpeg"
            )
        logging.info(f"  {tool}: {path}")

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_track_tmp(track: str, artist: str, output_dir: Path, audio_format: str) -> Optional[Path]:
    """
    Baixa a música via yt-dlp para um arquivo temporário na pasta de saída.
    Retorna o Path do arquivo baixado, ou None em caso de falha.
    """
    search_query = f"ytsearch1:{track} {get_primary_artist(artist)} audio"
    tmp_template = str(output_dir / "_tmp_%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--format", "bestaudio/best",
        "--extract-audio",
        "--audio-format", audio_format,
        "--no-playlist",
        "--retries", "3",
        "--socket-timeout", "30",
        "--extractor-args", "youtube:player_client=android",
        "--quiet",
        "--no-warnings",
        "--print", "after_move:filepath",
        "--output", tmp_template,
        search_query,
    ]

    if audio_format == "mp3":
        cmd += ["--audio-quality", "0", "--postprocessor-args", "ffmpeg:-b:a 320k"]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        logging.error(f"  [FALHA download] {err[:200]}")
        return None

    path_str = result.stdout.strip()
    if not path_str:
        logging.error("  [FALHA download] yt-dlp não retornou caminho do arquivo")
        return None

    path = Path(path_str)
    if not path.exists():
        logging.error(f"  [FALHA download] arquivo não encontrado: {path}")
        return None

    return path

# ---------------------------------------------------------------------------
# Processar CSV
# ---------------------------------------------------------------------------

def process_csv(csv_path: Path, audio_format: str) -> tuple[int, int]:
    """
    Processa um CSV: baixa e analisa cada música.
    Retorna (success_count, fail_count).
    """
    output_dir = ROOT_DIR / csv_path.stem
    output_dir.mkdir(exist_ok=True)

    success_count = 0
    fail_count    = 0
    skipped_count = 0

    logging.info(f"\n{'='*60}")
    logging.info(f"Processando: {csv_path.name}  →  {output_dir.name}/")
    logging.info(f"{'='*60}")

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    logging.info(f"Total de faixas: {total}\n")

    for i, row in enumerate(rows, 1):
        track  = (row.get("Track Name") or "").strip()
        artist = (row.get("Artist Name(s)") or "").strip()

        logging.info(f"[{i}/{total}] {track} — {artist}")

        if not track or not artist:
            logging.warning("  [PULANDO] Track Name ou Artist Name vazio")
            fail_count += 1
            continue

        # Idempotência: pula se já existe arquivo com esse prefixo
        if output_dir.exists() and already_downloaded(output_dir, track, artist, audio_format):
            logging.info("  [EXISTENTE] Já baixada, pulando")
            skipped_count += 1
            continue

        # 1. Baixar para arquivo temporário
        tmp_path = download_track_tmp(track, artist, output_dir, audio_format)
        if tmp_path is None:
            fail_count += 1
            continue

        # 2. Analisar áudio real (tonalidade + BPM)
        logging.info("  Analisando tonalidade e BPM...")
        try:
            key, mode, bpm = analyze_audio(tmp_path)
            logging.info(f"  Resultado: {key}{mode} / {bpm} BPM")
        except Exception as e:
            logging.warning(f"  [AVISO] Análise falhou ({e}), usando 'unknownkey_0bpm'")
            key, mode, bpm = "unknown", "", 0

        # 3. Renomear para nome final
        final_filename = build_output_filename(track, artist, key, mode, bpm, audio_format)
        final_path = output_dir / final_filename
        if final_path.exists():
            final_path.unlink()
        tmp_path.rename(final_path)

        logging.info(f"  [OK] → {final_filename}")
        success_count += 1

    logging.info(
        f"\nResumo {csv_path.name}: "
        f"{success_count} baixadas, {fail_count} falhas, {skipped_count} já existiam"
    )
    return success_count, fail_count

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shuffle / ordenamento para mixing
# ---------------------------------------------------------------------------

def _parse_audio_filename(path: Path) -> Optional[dict]:
    """
    Extrai key, mode, bpm e artista do nome do arquivo.
    Suporta re-execução: ignora prefixo numérico e sufixo Camelot já existentes.
    """
    stem = path.stem
    stem = re.sub(r'^\d+_', '', stem)                        # remove prefixo: "00_"
    stem = re.sub(r'_\d{1,2}[ab]$', '', stem, flags=re.IGNORECASE)  # remove sufixo: "_8a"

    m = re.match(rf'^(.+)_({_KEY_PATTERN})(major|minor)_(\d+)bpm$', stem)
    if not m:
        return None

    prefix, key, mode, bpm_str = m.group(1), m.group(2), m.group(3), m.group(4)
    bpm = int(bpm_str)
    camelot_num, camelot_letter = CAMELOT_MAP.get((key, mode), (0, "?"))

    # Último token do prefix = artista sanitizado (ex: "dire_straits_Fmajor" → "straits")
    artist_hint = prefix.split('_')[-1]

    return {
        "path":           path,
        "clean_stem":     stem,
        "key":            key,
        "mode":           mode,
        "bpm":            bpm,
        "camelot_num":    camelot_num,
        "camelot_letter": camelot_letter,
        "artist_hint":    artist_hint,
    }


def _camelot_distance(n1: int, l1: str, n2: int, l2: str) -> float:
    """Distância harmônica no Camelot wheel. 0 = mesma tonalidade."""
    if n1 == n2 and l1 == l2:
        return 0.0
    if n1 == n2:          # relativa maior/menor
        return 0.5
    num_dist = min(abs(n1 - n2), 12 - abs(n1 - n2))  # circular 1-12
    letter_penalty = 0.0 if l1 == l2 else 0.5
    return float(num_dist) + letter_penalty


def _bpm_distance(b1: int, b2: int) -> float:
    """Distância de BPM considerando dobro/metade (ex: 70 e 140 são compatíveis)."""
    return float(min(abs(b1 - b2), abs(b1 - 2 * b2), abs(2 * b1 - b2)))


def _mixing_score(t1: dict, t2: dict) -> float:
    """Score de transição: menor = melhor. 60% harmônico, 40% BPM."""
    camelot_d = _camelot_distance(
        t1["camelot_num"], t1["camelot_letter"],
        t2["camelot_num"], t2["camelot_letter"],
    )
    bpm_d = _bpm_distance(t1["bpm"], t2["bpm"])
    return 0.6 * (camelot_d / 6.0) + 0.4 * (bpm_d / 100.0)


def _sort_for_mixing(tracks: list, sem_repeticao: bool) -> list:
    """
    Greedy nearest-neighbor: começa pela faixa de BPM mediano,
    a cada passo escolhe a de menor mixing_score.
    Com sem_repeticao, evita artista igual ao anterior.
    """
    if not tracks:
        return tracks

    sorted_bpm = sorted(tracks, key=lambda t: t["bpm"])
    current    = sorted_bpm[len(sorted_bpm) // 2]
    remaining  = [t for t in tracks if t is not current]
    result     = [current]

    while remaining:
        prev        = result[-1]
        prev_artist = prev["artist_hint"] if sem_repeticao else None

        candidates = (
            [t for t in remaining if t["artist_hint"] != prev_artist]
            if sem_repeticao else remaining
        )
        if not candidates:       # fallback: aceita mesmo artista
            candidates = remaining

        next_track = min(candidates, key=lambda t: _mixing_score(prev, t))
        result.append(next_track)
        remaining.remove(next_track)

    return result


def apply_shuffle(name_filter: str, sem_repeticao: bool) -> None:
    """Ordena faixas para mixing fluido e renomeia com índice + notação Camelot."""
    matches = [
        d for d in sorted(ROOT_DIR.iterdir())
        if d.is_dir() and name_filter.lower() in d.name.lower()
    ]

    if not matches:
        print(f"Nenhum diretório encontrado com '{name_filter}'.")
        return

    for directory in matches:
        audio_files = sorted(
            f for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in (".mp3", ".flac")
        )

        tracks      = [t for t in (_parse_audio_filename(f) for f in audio_files) if t]
        unparseable = [f for f in audio_files if _parse_audio_filename(f) is None]

        if not tracks:
            print(f"{directory.name}/: nenhuma faixa com formato reconhecido.")
            continue

        ordered = _sort_for_mixing(tracks, sem_repeticao)

        # Renomear em dois passos para evitar conflito de nomes
        for i, track in enumerate(ordered):
            camelot_str = f"{track['camelot_num']}{track['camelot_letter'].lower()}"
            new_name    = f"{i:02d}_{track['clean_stem']}_{camelot_str}{track['path'].suffix}"
            tmp_path    = directory / f"_shuf_tmp_{i}{track['path'].suffix}"
            track["path"].rename(tmp_path)
            track["tmp_path"] = tmp_path
            track["new_name"] = new_name

        print(f"\n=== {directory.name}/ — {len(ordered)} faixas ===")
        for i, track in enumerate(ordered):
            final_path = directory / track["new_name"]
            Path(track["tmp_path"]).rename(final_path)
            camelot_str = f"{track['camelot_num']}{track['camelot_letter']}"
            print(f"  {i:02d}. {track['new_name']}  [{track['bpm']} bpm | {camelot_str}]")

        if unparseable:
            print(f"\n  [{len(unparseable)} arquivo(s) ignorado(s) — sem key/bpm no nome]")


def list_directory(name_filter: str) -> None:
    """Lista os nomes de músicas em diretórios que contenham name_filter no nome."""
    matches = [
        d for d in sorted(ROOT_DIR.iterdir())
        if d.is_dir() and name_filter.lower() in d.name.lower()
    ]

    if not matches:
        print(f"Nenhum diretório encontrado com '{name_filter}' em '{ROOT_DIR}'.")
        return

    for directory in matches:
        audio_files = sorted(
            f for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in (".mp3", ".flac")
        )
        print(f"\n=== {directory.name}/ ({len(audio_files)} faixas) ===")
        for f in audio_files:
            print(f.stem)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Baixa músicas de playlists CSV (Exportify) para uso no Serato DJ."
    )
    parser.add_argument(
        "--formato",
        choices=["mp3", "flac"],
        default="mp3",
        help="Formato de áudio: mp3 (320kbps, padrão) ou flac (lossless)",
    )
    parser.add_argument(
        "--ler_dict",
        action="store_true",
        help="Lista os nomes das músicas em um diretório (requer -n)",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Ordena faixas para mixing fluido por BPM e tonalidade (requer -n)",
    )
    parser.add_argument(
        "--sem_repeticao",
        action="store_true",
        help="Evita artista repetido consecutivamente (usado com --shuffle)",
    )
    parser.add_argument(
        "-n", "--nome",
        default=None,
        help="Filtro parcial do nome do diretório (usado com --ler_dict e --shuffle)",
    )
    args = parser.parse_args()
    audio_format: str = args.formato

    if args.ler_dict:
        if not args.nome:
            parser.error("--ler_dict requer -n <nome>")
        list_directory(args.nome)
        return

    if args.shuffle:
        if not args.nome:
            parser.error("--shuffle requer -n <nome>")
        apply_shuffle(args.nome, args.sem_repeticao)
        return

    setup_logging()

    # No Windows, conda instala ffmpeg em Library/bin que não está no PATH por padrão
    import os
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        lib_bin = Path(conda_prefix) / "Library" / "bin"
        if lib_bin.exists() and str(lib_bin) not in os.environ["PATH"]:
            os.environ["PATH"] = str(lib_bin) + os.pathsep + os.environ["PATH"]

    logging.info("=" * 60)
    logging.info("  Serato DJ Music Downloader")
    logging.info(f"  Formato: {audio_format.upper()}  |  Análise: librosa (áudio real)")
    logging.info("=" * 60)

    try:
        check_dependencies()
    except EnvironmentError as e:
        logging.error(str(e))
        return

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    PROCESSED_DIR.mkdir(exist_ok=True)

    csv_files = sorted(DOWNLOAD_DIR.glob("*.csv"))
    if not csv_files:
        logging.info(f"Nenhum CSV encontrado em '{DOWNLOAD_DIR}'.")
        logging.info("Exporte sua playlist em exportify.net, coloque o .csv em 'download/' e rode novamente.")
        return

    logging.info(f"Encontrados {len(csv_files)} CSV(s) para processar.\n")

    total_success = 0
    total_fail    = 0

    for csv_path in csv_files:
        success, fail = process_csv(csv_path, audio_format)
        total_success += success
        total_fail    += fail

        dest = PROCESSED_DIR / csv_path.name
        if dest.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = PROCESSED_DIR / f"{csv_path.stem}_{timestamp}.csv"
        shutil.move(str(csv_path), str(dest))
        logging.info(f"CSV movido para: {dest}")

    logging.info("\n" + "=" * 60)
    logging.info(f"CONCLUÍDO — Total: {total_success} baixadas, {total_fail} falhas")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
