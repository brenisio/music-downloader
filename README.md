# DJ Music Downloader

Baixa músicas de playlists do Spotify em alta qualidade, prontas para uso no **Serato DJ**.

Cada arquivo é salvo na **ordem da playlist**, com o BPM **detectado do áudio real**
(via librosa, não copiado do metadado do Spotify):

```
01_129bpm_StereoLove_EdwardMaya.m4a
02_089bpm_NemDeGraca_Pixote.m4a
03_148bpm_SultansOfSwing_DireStraits.m4a
```

O índice preserva a ordem do CSV; o BPM vai com zero à esquerda pra ordenação por
BPM também sair certa. A tonalidade fica registrada no log, não no nome.

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

**M4A** (padrão, melhor qualidade possível):
```bash
python run_baixador.py
```

Baixa o AAC nativo do YouTube (~130kbps, itag 140) **sem re-encode** — o arquivo
que chega no Serato é bit a bit o que o YouTube serve. É o teto real: o YouTube
não distribui lossless, então FLAC aqui só inflaria o arquivo sem recuperar um bit.

**MP3 320kbps** (se algum equipamento não ler m4a):
```bash
python run_baixador.py --formato mp3
```

Passa o áudio por um re-encode (2ª geração lossy). Soa igual ou pior que o m4a,
nunca melhor — use só por compatibilidade.

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
│   ├── 01_148bpm_SultansOfSwing_DireStraits.m4a
│   ├── 02_120bpm_HaveACigar_PinkFloyd.m4a
│   └── ...
└── processed/
    └── MinhaPlaylist.csv
```

---

## Listar músicas de um diretório

Depois de baixar uma playlist, você pode consultar quais músicas estão em uma pasta sem precisar abrir o explorador de arquivos — útil para copiar a lista e usar em outro lugar (ex: montar set, criar referência, importar para outro sistema).

```bash
python run_baixador.py --ler_dict -n <parte-do-nome>
```

O filtro é por **substring**, sem diferenciar maiúsculas de minúsculas. Exemplo:

```bash
python run_baixador.py --ler_dict -n The_Drake
```

Saída:
```
=== The_Drake_Richy_Malone/ (42 faixas) ===
01_087bpm_GodsPlan_Drake
02_091bpm_InMyFeelings_Drake
03_112bpm_PassionFruit_Drake
...
```

Se o filtro bater em mais de um diretório, todos são listados. Os nomes exibidos não incluem a extensão — são exatamente como o Serato exibe as faixas.

---

## Ordenar faixas para mixing (shuffle inteligente)

> **Atenção:** o `--shuffle` lê a tonalidade **do nome do arquivo**, e o formato
> atual (`01_129bpm_Nome_Artista`) não guarda tonalidade — o nome preserva a ordem
> da playlist, que é o oposto do que o shuffle faz. Ele continua funcionando nas
> pastas antigas (`nome_artista_Fmajor_148bpm`); em pastas novas ele avisa
> "nenhuma faixa com formato reconhecido" e não mexe em nada.

### Por que isso existe

Quando você monta um set no Serato, a ordem das músicas importa muito. Misturar duas faixas em tonalidades incompatíveis soa errado mesmo que o BPM bata — e ajustar isso manualmente em uma playlist de 50+ músicas é trabalhoso.

Este comando resolve isso automaticamente: ele lê o BPM e a tonalidade que já estão no nome de cada arquivo e calcula a ordem ideal para que cada transição seja harmonicamente suave.

### Como funciona

A compatibilidade harmônica entre faixas é calculada pelo **Camelot wheel** — o sistema usado por DJs profissionais onde cada tonalidade tem um número (1–12) e uma letra (A = menor, B = maior). Faixas são compatíveis quando estão na mesma posição ou em posições adjacentes no wheel.

O algoritmo:

1. Converte a tonalidade de cada faixa para a posição Camelot (ex: `Fmajor` → `7B`, `Aminor` → `8A`)
2. Começa pela faixa com BPM mediano da playlist
3. A cada passo, escolhe a próxima faixa com menor "custo de transição" — uma combinação de distância harmônica no Camelot (60%) e diferença de BPM (40%)
4. O BPM considera mix em dobro/metade: 70 e 140 bpm são tratados como compatíveis

### Comandos

```bash
# Ordenar por compatibilidade harmônica + BPM
python run_baixador.py --shuffle -n The_Drake

# Idem, sem repetir o mesmo artista consecutivamente
python run_baixador.py --shuffle --sem_repeticao -n The_Drake
```

O filtro `-n` funciona igual ao `--ler_dict`: substring, case-insensitive.

### Exemplo de ordenação

Dado um conjunto de faixas com tonalidades e BPMs variados, o algoritmo produz uma sequência onde cada transição é harmônica:

```
00. cooler_than_a_bitch_gunna_Eminor_117bpm    → 9A  117 bpm
01. blame_it_on_me_post_malone_Eminor_123bpm   → 9A  123 bpm  (mesma tonalidade)
02. starboy_the_weeknd_Aminor_123bpm           → 8A  123 bpm  (8A é adjacente a 9A, BPM igual)
03. start_wit_me_roddy_ricch_Cmajor_129bpm     → 8B  129 bpm  (8B é relativa de 8A)
04. fake_love_drake_Dmajor_136bpm              → 10B 136 bpm  (sobe gradualmente)
05. hotline_bling_drake_Dminor_136bpm          → 7A  136 bpm  (mesma letra, BPM igual)
06. saint_tropez_post_malone_Fmajor_129bpm     → 7B  129 bpm  (7B é relativa de 7A)
...
```

A notação no final (`9a`, `8b`, etc.) é a posição Camelot — você pode usá-la como referência visual na hora de mixar no Serato.

O comando é **idempotente**: rodar novamente reconhece os prefixos e sufixos já adicionados e reordena corretamente sem duplicar informação no nome.

---

## Notas

- **Re-execução segura**: arquivos já baixados são pulados automaticamente
- **Log completo**: tudo é registrado em `baixador.log`
- A análise usa os primeiros 60 segundos do áudio para agilizar
- Músicas não encontradas no YouTube são puladas e registradas no log
- **Escolha da versão**: a busca traz 5 candidatos e escolhe pela **duração do
  Spotify**, desempatando por canal (`- Topic` > oficial/VEVO > terceiros). Pegar
  o primeiro resultado cegamente traz live, slowed e nightcore no lugar da faixa.
- **Relatório de conferência**: no fim de cada CSV, as faixas cuja duração não
  bateu com o Spotify são listadas juntas. Confira essas antes de tocar — é onde
  mora o risco de ter baixado outra música com nome parecido.
- **Se der HTTP 403**: seu `yt-dlp` está velho. `pip install -U yt-dlp` resolve.
