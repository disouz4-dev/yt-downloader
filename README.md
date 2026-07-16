# Voxly YT Downloader

Gerenciador de músicas para karaokê — baixa vídeos do YouTube, renomeia automaticamente via iTunes, detecta duplicatas entre canais e organiza o acervo em estrutura flat compatível com VirtualDJ e similares.

---

## Download

**Opção A — Git (recomendado):**
```bash
git clone https://github.com/disouz4-dev/yt-downloader.git
cd yt-downloader
```

**Opção B — Arquivo ZIP:**
1. Clique em **Code → Download ZIP** no topo desta página
2. Extraia a pasta onde quiser
3. Abra o terminal dentro da pasta extraída

---

## Funcionalidades

- **Download inteligente** — baixa playlists/canais inteiros, pula vídeos já baixados (por ID e por título normalizado)
- **Renomeação automática** — consulta iTunes para obter artista e título padronizados
- **Estrutura flat** — todos os arquivos na pasta raiz, sem subpastas por canal
- **Deduplicação cross-canal** — evita baixar a mesma música de dois canais diferentes
- **Verificador de integridade** — detecta arquivos corrompidos via ffmpeg e oferece re-download
- **Migração para flat** — extrai todos os arquivos de subpastas antigas e remove as pastas
- **Organização por artista** — opcionalmente move cada arquivo para `Artista/musica.mp4`
- **Cookies automáticos** — usa cookies do Chrome/Firefox para contornar restrições do YouTube

---

## Requisitos

| Dependência | Versão mínima | Finalidade |
|---|---|---|
| Python | 3.11+ | runtime |
| ffmpeg | qualquer | merge de vídeo+áudio, verificação de integridade |
| Node.js | 22+ | resolver n-challenge do YouTube (EJS) |
| yt-dlp | 2026.6.9+ | engine de download |
| Pillow | 10+ | processamento de capas |
| requests | 2.31+ | busca iTunes |
| yt-dlp-ejs | 0.8+ | solver de JavaScript para YouTube |

---

## Instalação — Windows

### 1. Instalar Python

Baixe em **https://www.python.org/downloads/** (versão 3.12 recomendada).

> **Importante:** marque a opção **"Add Python to PATH"** durante a instalação.

### 2. Instalar ffmpeg

**Opção A — via winget (recomendado):**
```
winget install Gyan.FFmpeg
```

**Opção B — manual:**
1. Baixe em https://www.gyan.dev/ffmpeg/builds/ → `ffmpeg-release-essentials.zip`
2. Extraia para `C:\ffmpeg`
3. Adicione `C:\ffmpeg\bin` ao PATH do sistema:
   - Pesquise **"Editar variáveis de ambiente do sistema"**
   - `Variáveis de ambiente` → `Path` → `Editar` → `Novo` → `C:\ffmpeg\bin`

Teste: abra o terminal e execute `ffmpeg -version`

### 3. Instalar Node.js

Baixe em **https://nodejs.org/** (versão LTS).

Teste: `node --version`

### 4. Instalar dependências Python

Abra o terminal na pasta do projeto e execute:

```
pip install -r requirements.txt
```

### 5. Executar

```
python renomear_musicas.py
```

---

## Instalação — Linux

```bash
# Dependências do sistema
sudo apt install ffmpeg python3-tk

# Node.js v22
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install nodejs

# Dependências Python
pip install -r requirements.txt --break-system-packages

# Executar
python3 renomear_musicas.py
```

---

## Uso rápido

1. Abra o app → aba **⬇ Download**
2. Cole a URL de um canal ou playlist do YouTube
3. Selecione a pasta de destino no disco externo
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

---

## Observações

- O banco de dados (`.voxly_db.json`) e o arquivo de controle (`.voxly_yt_archive.txt`) ficam na própria pasta de músicas.
- O sufixo `[abc123]` no nome do arquivo é um hash curto usado para controle interno — não afeta a reprodução.
- Para migrar uma biblioteca com subpastas antigas: aba **✏️ Renomear** → **📤 Migrar para Flat**.
