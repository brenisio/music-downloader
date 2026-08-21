#!/usr/bin/env python3
"""run_baixador.py — DJ music downloader for Serato.

Usage:
    python run_baixador.py                  # M4A/AAC nativo do YouTube, sem re-encode (padrão)
    python run_baixador.py --formato mp3    # MP3 320kbps (re-encode, 2ª geração lossy)

Coloque os arquivos CSV (exportados do Exportify) na pasta 'download/' e rode o script.
As músicas são salvas em pastas com o nome do CSV, no formato:

    01_129bpm_StereoLove_EdwardMaya.m4a

O índice preserva a ordem da playlist do Spotify; o BPM é detectado por análise do
áudio real (librosa), não copiado do metadado do Spotify. A tonalidade não entra no
nome — ela é registrada no log. Como o --shuffle harmônico lê a tonalidade do nome
do arquivo, ele não opera em pastas nomeadas assim (e é justamente o ponto: aqui a
ordem é a da playlist).
"""

import argparse
import csv
import json
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

def setup_logging(log_file: Path = LOG_FILE) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

# ---------------------------------------------------------------------------
# Análise de áudio
# ---------------------------------------------------------------------------

# Quão perto um múltiplo precisa ficar da referência do Spotify pra gente
# acreditar nela. O Tempo do Spotify também erra — em funk ele lê tercina e
# devolve 86 pra uma faixa de 129 — então só aceitamos a correção quando algum
# múltiplo casa de verdade. Sem casamento, o áudio medido manda.
OCTAVE_TOLERANCE = 0.10


def _fix_octave(bpm: float, reference: Optional[float]) -> float:
    """
    Corrige erro de oitava do detector: ele acerta a pulsação mas erra o múltiplo
    (marca pagode de 80 como 161). Escolhe entre metade/dobro/quádruplo o mais
    próximo da estimativa do Spotify — mas só se ele realmente bater.

    A medição sempre vem do áudio real; o Spotify só desempata o múltiplo, e só
    quando é claramente um deles. Se a referência não casa com nenhum múltiplo,
    ela é que está errada, e o valor medido fica como está.
    """
    if not reference:
        return bpm

    melhor = min([bpm / 4, bpm / 2, bpm, bpm * 2, bpm * 4],
                 key=lambda c: abs(c - reference))
    return melhor if abs(melhor - reference) <= reference * OCTAVE_TOLERANCE else bpm


def analyze_audio(file_path: Path, tempo_ref: Optional[float] = None) -> tuple[str, str, int]:
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
    bpm_bruto = float(np.atleast_1d(tempo)[0])
    bpm       = round(_fix_octave(bpm_bruto, tempo_ref))
    if abs(bpm - bpm_bruto) > 1:
        logging.info(f"  Oitava corrigida: {bpm_bruto:.1f} → {bpm} (Spotify: {tempo_ref:.0f})")

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


def pascal_filename(text: str) -> str:
    """
    Converte para PascalCase sem separadores: 'Stereo Love' → 'StereoLove'.
    Só a primeira letra de cada palavra é forçada — o resto do token fica como
    veio, pra siglas não virarem 'Mc Gw'.
    """
    normalized = unicodedata.normalize("NFD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned    = re.sub(r"[^\w\s-]", " ", ascii_text)
    tokens     = [t for t in re.split(r"[\s\-_]+", cleaned) if t]
    return "".join(t[0].upper() + t[1:] for t in tokens)[:60]


def track_signature(track: str, artist: str, ext: str) -> str:
    """Sufixo que identifica a faixa, independente de posição e BPM."""
    return f"_{pascal_filename(track)}_{pascal_filename(get_primary_artist(artist))}.{ext}"


def build_output_filename(
    index: int, width: int, track: str, artist: str, bpm: int, ext: str
) -> str:
    """
    Ex: '01_129bpm_StereoLove_EdwardMaya.m4a'

    O índice preserva a ordem da playlist do Spotify; o BPM vem com zero à
    esquerda pra ordenação alfabética por BPM também sair certa.
    """
    return f"{index:0{width}d}_{bpm:03d}bpm{track_signature(track, artist, ext)}"


def find_downloaded(output_dir: Path, track: str, artist: str, ext: str) -> Optional[Path]:
    """
    Procura um arquivo já baixado dessa faixa. Casa pelo sufixo, porque o começo
    do nome (posição e BPM) muda entre execuções.
    """
    sig = track_signature(track, artist, ext)
    return next((f for f in output_dir.iterdir() if f.name.endswith(sig)), None)


def reindex_directory(output_dir: Path, rows: list, width: int, ext: str) -> int:
    """
    Realinha o índice dos arquivos já baixados com a ordem atual do CSV, sem
    rebaixar nada. Reordenar a playlist e rodar de novo deve custar zero download.

    O rename é em duas fases (tudo para nomes temporários, depois para os finais)
    porque trocar duas faixas de posição faria o destino colidir com um arquivo
    que ainda existe.
    """
    planejado = []
    for i, row in enumerate(rows, 1):
        track  = (row.get("Track Name") or "").strip()
        artist = (row.get("Artist Name(s)") or "").strip()
        if not track or not artist:
            continue

        atual = find_downloaded(output_dir, track, artist, ext)
        if atual is None:
            continue

        m = re.match(r"^(\d+)_(\d+bpm.*)$", atual.name)
        if not m:
            continue

        desejado = f"{i:0{width}d}_{m.group(2)}"
        if desejado != atual.name:
            planejado.append((atual, desejado))

    if not planejado:
        return 0

    temporarios = []
    for origem, desejado in planejado:
        tmp = origem.with_name(f"_reidx_{origem.name}")
        origem.rename(tmp)
        temporarios.append((tmp, desejado, origem.name))

    for tmp, desejado, antigo in temporarios:
        tmp.rename(tmp.with_name(desejado))
        logging.info(f"  [REORDENADA] {antigo} → {desejado}")

    return len(temporarios)

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

# Quantos resultados a busca traz. Pegar o top-1 cego erra bastante: o primeiro
# resultado costuma ser live, slowed, nightcore ou cover. A duração do Spotify
# desempata entre os candidatos.
SEARCH_CANDIDATES    = 5
DURATION_TOLERANCE_S = 8


def _run_search(termos: str) -> list:
    """Roda uma busca no YouTube e devolve os metadados rasos dos resultados."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--quiet",
        "--no-warnings",
        "--socket-timeout", "30",
        f"ytsearch{SEARCH_CANDIDATES}:{termos}",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        logging.error(f"  [FALHA busca] {(result.stderr or '').strip()[:200]}")
        return []

    candidates = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return candidates


def _search_candidates(track: str, artist: str) -> list:
    """
    Busca candidatos no YouTube nas duas ordens de termos e junta os resultados.

    As duas buscas são necessárias, não redundantes: a ordem dos termos muda
    radicalmente o que o YouTube devolve. Para 'MEGA SHINE FOREVER'/GRANA, a
    busca 'faixa artista' traz Rihanna e Lana Del Rey; 'artista faixa' traz o
    canal Topic do próprio GRANA em primeiro. Para 'Pills'/Mukkan e 'Na Ponta
    Ela Fica'/Delano, idem. Rodar só uma das ordens perde a faixa certa sem
    nunca dar sinal de que existia.
    """
    principal = get_primary_artist(artist)

    vistos, candidatos = set(), []
    for termos in (f"{track} {principal} audio", f"{principal} {track}"):
        for c in _run_search(termos):
            chave = c.get("id") or c.get("url")
            if chave and chave not in vistos:
                vistos.add(chave)
                candidatos.append(c)
    return candidatos


def _channel_rank(candidate: dict) -> int:
    """
    Menor é melhor. Canais '- Topic' são gerados pelo YouTube Music a partir do
    master da gravadora — melhor fonte de áudio disponível. Canais oficiais/VEVO
    vêm em seguida; upload de terceiro é o último recurso.
    """
    canal = (candidate.get("channel") or candidate.get("uploader") or "").lower()
    if canal.endswith(" - topic"):
        return 0
    if "vevo" in canal or "official" in canal or "records" in canal:
        return 1
    return 2


# Fração das palavras do título que precisa aparecer no título do candidato.
TITLE_OVERLAP_MIN = 0.5


def _tokens(texto: str) -> set:
    """Palavras significativas de um título, sem acento nem pontuação."""
    ascii_txt = unicodedata.normalize("NFD", texto).encode("ascii", "ignore").decode()
    return {t for t in re.split(r"[^\w]+", ascii_txt.lower()) if len(t) >= 3}


def _title_overlap(candidate: dict, track: str) -> float:
    """Quanto do nome da faixa aparece no título do candidato (0.0 a 1.0)."""
    alvo = _tokens(track)
    if not alvo:
        return 1.0
    return len(alvo & _tokens(candidate.get("title") or "")) / len(alvo)


def _version_tokens(track: str) -> set:
    """
    Palavras que identificam a VERSÃO, não a música. O Exportify põe isso depois
    do último ' - ': 'Slow Down (feat. Jorja Smith) - Vintage Culture & Slow
    Motion Remix' → {vintage, culture, slow, motion, remix}.
    """
    return _tokens(track.rsplit(" - ", 1)[1]) if " - " in track else set()


def _version_miss(candidate: dict, track: str) -> int:
    """
    0 se o candidato parece ser a versão pedida, 1 se não. O nome do canal conta
    junto com o título — remix costuma sair no canal de quem remixou.

    Sem isso, 'Slow Down ... - Vintage Culture Remix' baixava o ORIGINAL do
    Maverick Sabre: metade das palavras batia e a duração batia melhor que a do
    remix. As palavras que distinguem são justamente as que faltavam.
    """
    alvo = _version_tokens(track)
    if not alvo:
        return 0

    texto = f"{candidate.get('title') or ''} {candidate.get('channel') or ''}"
    return 0 if len(alvo & _tokens(texto)) / len(alvo) >= 0.5 else 1


def _pick_candidate(candidates: list, duration_ms: Optional[int], track: str = "") -> Optional[dict]:
    """
    Escolhe o candidato certo em três critérios, nesta ordem: o título tem que ser
    da música, a duração tem que bater, e a fonte tem que ser a melhor disponível.

    O título vem primeiro porque duração sozinha erra feio — 'Saquarema' (2:31)
    puxava 'Rua Do Ouro' (2:23) do mesmo artista, outra música. Só depois de
    filtrar por título é que a duração desempata versões (ao vivo, remix, edit).
    """
    if not candidates:
        return None
    if not duration_ms:
        return candidates[0]

    target_s = duration_ms / 1000

    def delta(c: dict) -> float:
        d = c.get("duration")
        return abs(d - target_s) if d else float("inf")

    # Candidatos que são de fato esta música. Se nenhum título casa, a busca
    # trouxe outra coisa — aí a duração decide sozinha e o log avisa.
    do_titulo = [c for c in candidates if _title_overlap(c, track) >= TITLE_OVERLAP_MIN]
    disputa   = do_titulo or candidates

    # Ordem dos critérios: é esta música? é esta versão? bate a duração? a fonte
    # é boa? A versão vem antes da duração de propósito — a música certa no
    # tamanho errado é melhor que a música errada no tamanho certo.
    return min(disputa, key=lambda c: (_version_miss(c, track),
                                       delta(c) > DURATION_TOLERANCE_S,
                                       _channel_rank(c),
                                       delta(c)))


def download_track_tmp(
    track: str,
    artist: str,
    output_dir: Path,
    audio_format: str,
    duration_ms: Optional[int] = None,
) -> tuple:
    """
    Baixa a música via yt-dlp para um arquivo temporário na pasta de saída.
    Retorna (Path, titulo_escolhido, delta_segundos) — delta é a diferença de
    duração pro Spotify, pra quem chamou saber se a versão é suspeita.
    Em caso de falha, retorna (None, None, None).
    """
    chosen = _pick_candidate(_search_candidates(track, artist), duration_ms, track)
    if chosen is None:
        logging.error("  [FALHA busca] nenhum candidato encontrado")
        return None, None, None

    # Auditoria da escolha: melhor conferir no log do que descobrir no meio do set.
    dur       = chosen.get("duration")
    titulo    = chosen.get("title", "?")
    delta     = None
    delta_txt = "?"
    if dur and duration_ms:
        delta     = dur - duration_ms / 1000
        delta_txt = f"{delta:+.0f}s"
    canal = chosen.get("channel") or chosen.get("uploader") or "?"
    logging.info(f"  Escolhido: '{titulo}' | {canal} | {dur or '?'}s (Δ {delta_txt})")
    if delta is not None and abs(delta) > DURATION_TOLERANCE_S:
        logging.warning(f"  [ATENÇÃO] duração destoa do Spotify em {delta_txt} — confira a versão")

    tmp_template = str(output_dir / "_tmp_%(id)s.%(ext)s")
    url = chosen.get("url") or f"https://www.youtube.com/watch?v={chosen.get('id')}"

    cmd = [
        "yt-dlp",
        # AAC nativo do YouTube: o Serato lê m4a e o arquivo sai sem re-encode.
        # Opus soaria melhor no mesmo bitrate, mas o Serato não abre.
        "--format", "bestaudio[ext=m4a]/bestaudio",
        "--extract-audio",
        "--audio-format", audio_format,
        "--no-playlist",
        "--retries", "3",
        "--socket-timeout", "30",
        "--quiet",
        "--no-warnings",
        "--print", "after_move:filepath",
        "--output", tmp_template,
        url,
    ]

    if audio_format == "mp3":
        cmd += ["--audio-quality", "0", "--postprocessor-args", "ffmpeg:-b:a 320k"]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        logging.error(f"  [FALHA download] {err[:200]}")
        return None, None, None

    path_str = result.stdout.strip()
    if not path_str:
        logging.error("  [FALHA download] yt-dlp não retornou caminho do arquivo")
        return None, None, None

    path = Path(path_str)
    if not path.exists():
        logging.error(f"  [FALHA download] arquivo não encontrado: {path}")
        return None, None, None

    return path, titulo, delta

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
    # Faixas cuja duração não bateu com o Spotify — provavelmente versão errada.
    suspeitas: list = []

    logging.info(f"\n{'='*60}")
    logging.info(f"Processando: {csv_path.name}  →  {output_dir.name}/")
    logging.info(f"{'='*60}")

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    # Largura do índice: 2 dígitos até 99 faixas, 3 daí pra cima.
    width = max(2, len(str(total)))
    logging.info(f"Total de faixas: {total}\n")

    # Se o CSV foi reordenado desde a última execução, realinha os índices dos
    # arquivos que já estão no disco antes de sair baixando.
    if output_dir.exists():
        movidas = reindex_directory(output_dir, rows, width, audio_format)
        if movidas:
            logging.info(f"  {movidas} faixa(s) reposicionada(s) pela nova ordem do CSV\n")

    for i, row in enumerate(rows, 1):
        track  = (row.get("Track Name") or "").strip()
        artist = (row.get("Artist Name(s)") or "").strip()

        # Duração do Spotify: usada pra escolher a versão certa entre os candidatos.
        raw_duration = (row.get("Duration (ms)") or "").strip()
        duration_ms  = int(raw_duration) if raw_duration.isdigit() else None

        # Tempo do Spotify: só desempata o múltiplo do BPM medido (ver _fix_octave).
        try:
            tempo_ref = float((row.get("Tempo") or "").strip()) or None
        except ValueError:
            tempo_ref = None

        logging.info(f"[{i}/{total}] {track} — {artist}")

        if not track or not artist:
            logging.warning("  [PULANDO] Track Name ou Artist Name vazio")
            fail_count += 1
            continue

        # Idempotência: pula se já existe arquivo pra essa faixa
        if output_dir.exists() and find_downloaded(output_dir, track, artist, audio_format):
            logging.info("  [EXISTENTE] Já baixada, pulando")
            skipped_count += 1
            continue

        # 1. Baixar para arquivo temporário
        tmp_path, titulo, delta = download_track_tmp(
            track, artist, output_dir, audio_format, duration_ms
        )
        if tmp_path is None:
            fail_count += 1
            continue

        if delta is not None and abs(delta) > DURATION_TOLERANCE_S:
            suspeitas.append((i, track, titulo, delta))

        # 2. Analisar áudio real (tonalidade + BPM)
        # A tonalidade não entra mais no nome do arquivo, mas continua no log —
        # é a única cópia dela até o Serato fazer a análise dele.
        logging.info("  Analisando tonalidade e BPM...")
        try:
            key, mode, bpm = analyze_audio(tmp_path, tempo_ref)
            camelot_num, camelot_letter = CAMELOT_MAP.get((key, mode), (0, "?"))
            logging.info(f"  Resultado: {key}{mode} ({camelot_num}{camelot_letter}) / {bpm} BPM")
        except Exception as e:
            logging.warning(f"  [AVISO] Análise falhou ({e}), BPM vai como 000")
            bpm = 0

        # 3. Renomear para nome final
        final_filename = build_output_filename(i, width, track, artist, bpm, audio_format)
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

    # Relatório de conferência: baixar a versão errada é pior que um buraco
    # conhecido, então essas ficam listadas juntas em vez de espalhadas no log.
    if suspeitas:
        logging.warning(
            f"\n{'-'*60}\n"
            f"CONFERIR — {len(suspeitas)} faixa(s) com duração diferente do Spotify.\n"
            f"Provável versão errada (ao vivo, remix, edit ou outra música):\n"
            f"{'-'*60}"
        )
        for idx, track, titulo, delta in suspeitas:
            logging.warning(f"  [{idx:02d}] {track}")
            logging.warning(f"       baixou: '{titulo}'  (Δ {delta:+.0f}s)")

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
            if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".flac")
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
            if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".flac")
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
        choices=["m4a", "mp3"],
        default="m4a",
        help="Formato de áudio: m4a (AAC nativo do YouTube, sem re-encode — padrão) "
             "ou mp3 (320kbps, re-encode)",
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
    parser.add_argument(
        "--csv",
        default=None,
        help="Processa apenas um CSV específico (nome do arquivo em download/). "
             "Permite rodar vários CSVs em paralelo, um processo por arquivo.",
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

    # Log separado por CSV quando rodando em paralelo (--csv), pra evitar
    # escritas concorrentes no mesmo arquivo.
    log_file = LOG_FILE if not args.csv else ROOT_DIR / f"baixador_{Path(args.csv).stem}.log"
    setup_logging(log_file)

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

    if args.csv:
        target = DOWNLOAD_DIR / args.csv
        if not target.exists():
            logging.error(f"CSV não encontrado: {target}")
            return
        csv_files = [target]
    else:
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
