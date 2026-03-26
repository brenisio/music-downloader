# DJ Music Downloader

Baixa músicas de playlists do Spotify em alta qualidade, prontas para uso no **Serato DJ**.

Cada arquivo é salvo com tonalidade e BPM **detectados do áudio real** (via librosa):

```
sultans_of_swing_dire_straits_Fmajor_148bpm.mp3
passion_fruit_drake_Bmajor_112bpm.mp3
```

---

## Pré-requisitos

### 1. Python 3.10+
Baixe em [python.org](https://www.python.org/downloads/)

### 2. ffmpeg

**Conda (recomendado se estiver usando Anaconda/Miniconda):**
```bash
conda install -c conda-forge ffmpeg
```

**Windows (sem Conda):**
```bash
winget install ffmpeg
```
Ou baixe manualmente em [ffmpeg.org](https://ffmpeg.org/download.html) e adicione ao PATH.

**Ubuntu/Debian:**
```bash
sudo apt update && sudo apt install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

### 3. Dependências Python
Na pasta do projeto:
```bash
pip install -r requirements.txt
```

---

## Como usar

### Passo 1 — Exporte sua playlist do Spotify

1. Acesse **[exportify.net](https://exportify.net/)**
2. Faça login com sua conta Spotify
3. Clique em **Export** na playlist desejada
4. Salve o arquivo `.csv` gerado

### Passo 2 — Coloque o CSV na pasta `download/`

```
music-downloader/
└── download/
    └── MinhaPlaylist.csv
```

Você pode colocar vários CSVs de uma vez.

### Passo 3 — Rode o script

**MP3 320kbps** (padrão, recomendado para a maioria dos setups):
```bash
python run_baixador.py
```

**FLAC lossless** (qualidade máxima):
```bash
python run_baixador.py --formato flac
```

---

## O que acontece

1. O script lê cada CSV em `download/`
2. Para cada música: busca no YouTube e baixa a melhor qualidade disponível
3. Analisa o áudio com **librosa** para detectar tonalidade e BPM reais
4. Salva o arquivo com o nome contendo os dados detectados
5. Move o CSV para `processed/` após concluir

As músicas ficam em pastas com o nome da playlist:
```
music-downloader/
├── MinhaPlaylist/
│   ├── sultans_of_swing_dire_straits_Fmajor_148bpm.mp3
│   ├── have_a_cigar_pink_floyd_Eminor_120bpm.mp3
│   └── ...
└── processed/
    └── MinhaPlaylist.csv
```

---

## Notas

- **Re-execução segura**: arquivos já baixados são pulados automaticamente
- **Log completo**: tudo é registrado em `baixador.log`
- A análise usa os primeiros 60 segundos do áudio para agilizar
- Músicas não encontradas no YouTube são puladas e registradas no log
