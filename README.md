# Voxly YT Downloader

Gerenciador de músicas para karaokê — baixa vídeos do YouTube, renomeia automaticamente via iTunes, detecta duplicatas entre canais e organiza o acervo em estrutura flat compatível com VirtualDJ e similares.

---

## Download

```bash
git clone https://github.com/disouz4-dev/yt-downloader.git
cd yt-downloader
```

Ou clique em **Code → Download ZIP** no topo desta página e extraia a pasta.

---

## Instalação — Windows

### 1. Instalar Python 3.12

Baixe em **https://www.python.org/downloads/**

> ⚠️ Marque **"Add Python to PATH"** durante a instalação.

### 2. Instalar Node.js 22

Baixe a versão LTS em **https://nodejs.org/**

> O Node.js é necessário para resolver as proteções anti-bot do YouTube.

### 3. Instalar as dependências

Abra o terminal na pasta do projeto e execute:

```
pip install -r requirements.txt
```

Isso instala automaticamente: `yt-dlp`, `openai`, `ffmpeg`, `Pillow`, `requests` e o solver de JavaScript do YouTube.

> O ffmpeg é baixado automaticamente pelo pacote `static-ffmpeg` — não é necessário instalar manualmente.

### 4. Executar

```
python renomear_musicas.py
```

---

## Instalação — Linux

### 1. Instalar Node.js 22

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs python3-tk
```

### 2. Instalar as dependências

```bash
pip install -r requirements.txt --break-system-packages
```

### 3. Executar

```bash
python3 renomear_musicas.py
```

---

## Funcionalidades

- **Download inteligente** — baixa playlists/canais inteiros, pula vídeos já baixados (por ID e por título normalizado)
- **Renomeação automática** — consulta iTunes para obter artista e título padronizados
- **Estrutura flat** — todos os arquivos na pasta raiz, sem subpastas por canal
- **Deduplicação cross-canal** — evita baixar a mesma música de dois canais diferentes
- **Verificador de integridade** — detecta arquivos corrompidos via ffmpeg e oferece re-download
- **Migração para flat** — extrai todos os arquivos de subpastas antigas e remove as pastas
- **Cookies automáticos** — usa cookies do Chrome para contornar restrições do YouTube (403, n-challenge)

---

## Uso rápido

1. Abra o app → aba **⬇ Download**
2. Cole a URL de um canal ou playlist do YouTube
3. Selecione a pasta de destino (no disco externo)
4. Escolha a qualidade (padrão: 1080p)
5. Clique em **⬇ Baixar**

Os cookies do Chrome já vêm habilitados por padrão — necessário para canais grandes e vídeos com restrição de idade.

---

## Estrutura de arquivos gerada

```
/karaoke/
├── Artista - Nome da Música - [abc123].mp4
├── Artista - Nome da Música - [def456].mp4
├── .voxly_db.json          ← banco de dados interno
└── .voxly_yt_archive.txt   ← IDs já baixados
```

> Para migrar uma biblioteca com subpastas antigas: aba **✏️ Renomear** → **📤 Migrar para Flat**.
