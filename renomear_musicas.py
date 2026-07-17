#!/usr/bin/env python3
import os
import re
import json
import hashlib
import time
import threading
import subprocess
import urllib.request
import urllib.parse
import unicodedata

# Garante ffmpeg disponível mesmo sem instalação manual
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    pass  # ffmpeg já no PATH ou usuário instalou manualmente

import tkinter as tk
from tkinter import filedialog, ttk, messagebox
from openai import OpenAI, RateLimitError, APIError
import yt_dlp

def _notificar(titulo: str, msg: str) -> None:
    """Notificação de sistema cross-platform (falha silenciosa)."""
    try:
        if os.name == "nt":
            from ctypes import windll
            windll.user32.MessageBeep(0)
        else:
            subprocess.Popen(
                ["notify-send", "-i", "dialog-information", titulo, msg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass

CONFIG_FILE  = os.path.expanduser("~/.voxly_renomeador.json")
DB_FILE      = os.path.expanduser("~/.voxly_db.json")
YT_ARCHIVE   = os.path.expanduser("~/.voxly_yt_archive.txt")

_DB_FALLBACK      = DB_FILE
_ARCHIVE_FALLBACK = YT_ARCHIVE

def set_pasta_musicas(pasta):
    """Muda DB e archive para ficarem na pasta das músicas (no disco externo)."""
    global DB_FILE, YT_ARCHIVE
    if not pasta or not os.path.isdir(pasta):
        return
    novo_db      = os.path.join(pasta, ".voxly_db.json")
    novo_archive = os.path.join(pasta, ".voxly_yt_archive.txt")
    # Migra arquivos existentes do home caso ainda não existam no destino
    for src, dst in [(_DB_FALLBACK, novo_db), (_ARCHIVE_FALLBACK, novo_archive)]:
        if os.path.isfile(src) and not os.path.isfile(dst):
            try:
                import shutil
                shutil.copy2(src, dst)
            except Exception:
                pass
    DB_FILE    = novo_db
    YT_ARCHIVE = novo_archive

# ── Banco de dados ───────────────────────────────────────────
def db_carregar():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"renomeados": {}}

def db_salvar(db):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def db_buscar(id_hex):
    return db_carregar().get("renomeados", {}).get(id_hex)

def db_canal_salvar(url_canal, nome, videos):
    db = db_carregar()
    db.setdefault("canais", {})[url_canal] = {
        "nome":       nome,
        "total":      len(videos),
        "atualizado": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "videos":     videos,
    }
    db_salvar(db)

def db_canal_carregar(url_canal):
    return db_carregar().get("canais", {}).get(url_canal)

def db_musicas_salvar(pasta, titulos):
    db = db_carregar()
    db.setdefault("musicas_escaneadas", {})[pasta] = {
        "atualizado": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "titulos":    sorted(titulos),
    }
    db_salvar(db)

def db_musicas_titulos():
    """Retorna set de títulos normalizados de todas as pastas já escaneadas."""
    titulos = set()
    for d in db_carregar().get("musicas_escaneadas", {}).values():
        titulos.update(d.get("titulos", []))
    return titulos

def db_musicas_pastas():
    """Retorna dict {pasta: {atualizado, total}} para exibição."""
    return {
        p: {"atualizado": d.get("atualizado", ""), "total": len(d.get("titulos", []))}
        for p, d in db_carregar().get("musicas_escaneadas", {}).items()
    }

def archive_ids_carregar():
    try:
        with open(YT_ARCHIVE, encoding="utf-8") as f:
            return {ln.strip().split()[-1] for ln in f if ln.strip()}
    except Exception:
        return set()

_RE_RUIDO_TITULO = re.compile(
    r'\bkaraoke\b|\bversion\b|\bversão\b|\bofficial\b|\baudio\b|\blive\b'
    r'|\binstrumental\b|\blyrics\b|\bhd\b|\bhq\b|\b4k\b|\bsingking\b'
    r'|\bsing\s*king\b|\bno\s+lead\s+vocals?\b',
    re.IGNORECASE
)

def _normalizar_titulo(s):
    """Remove acentos, ruídos de karaokê e pontuação para comparar títulos com nomes de arquivo."""
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = _RE_RUIDO_TITULO.sub(' ', s)
    s = re.sub(r'[^a-z0-9\s]', ' ', s.lower())
    return re.sub(r'\s+', ' ', s).strip()

def nomes_pasta_carregar(pasta):
    """Retorna set de títulos normalizados de todos os arquivos de áudio/vídeo na pasta."""
    nomes = set()
    if not pasta or not os.path.isdir(pasta):
        return nomes
    for root, _, files in os.walk(pasta):
        for f in files:
            if os.path.splitext(f)[1].lower() in EXTS:
                nomes.add(_normalizar_titulo(os.path.splitext(f)[0]))
    return nomes

def _artista_do_titulo(titulo):
    """Extrai artista de títulos/nomes de arquivo tipo:
      'Ed Sheeran - Shape of You - [abc123]'   (renomeado pelo app)
      'Ed Sheeran - Shape of You (Karaoke Version)'  (título YouTube)
    Retorna a parte antes do primeiro ' - ' ou None.
    """
    if not titulo:
        return None
    # Remove sufixo hash gerado pelo app: ' - [abc123]' no final
    s = re.sub(r'\s*-\s*\[[0-9a-f]{6,}\]\s*$', '', titulo.strip())
    partes = s.split(' - ', 1)
    if len(partes) >= 2 and partes[0].strip() and partes[1].strip():
        return partes[0].strip()
    return None

def archive_adicionar(video_ids):
    """Adiciona IDs ao archive do yt-dlp para não baixar novamente."""
    try:
        with open(YT_ARCHIVE, "a", encoding="utf-8") as f:
            for vid_id in video_ids:
                f.write(f"youtube {vid_id}\n")
    except Exception:
        pass

def db_registrar(id_hex, nome_original, artista, musica, novo_nome):
    db = db_carregar()
    db.setdefault("renomeados", {})[id_hex] = {
        "nome_original": nome_original,
        "artista":       artista,
        "musica":        musica,
        "novo_nome":     novo_nome,
    }
    db_salvar(db)

PROVEDORES = {
    "Ollama": {
        "url": "http://localhost:11434/v1",
        "modelos": [
            "llama3.2:3b",
        ]
    },
    "Groq": {
        "url": "https://api.groq.com/openai/v1",
        "modelos": [
            # Produção
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            # Preview (rápido e confiável)
            "meta-llama/llama-4-scout-17b-16e-instruct",
            # Reasoning — podem retornar content vazio — fim da fila
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3-32b",
        ]
    },
    "OpenRouter": {
        "url": "https://openrouter.ai/api/v1",
        "modelos": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "cohere/north-mini-code:free",
            "poolside/laguna-m.1:free",
            "poolside/laguna-xs.2:free",
        ]
    },
}

EXTS = {'.mp4', '.mkv', '.avi', '.webm', '.mp3'}

# ── Config ──────────────────────────────────────────────────
def salvar_config(entradas):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"entradas": entradas}, f)

def carregar_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f).get("entradas", [])
    except Exception:
        return []

# ── Helpers ─────────────────────────────────────────────────
def gerar_id(nome):
    return hashlib.md5(nome.encode()).hexdigest()[:6]

def ja_tratado(nome):
    return bool(re.search(r'\[[0-9a-f]{6}\](\.[^.]+)?$', nome))

def extrair_ja_formatado(nome):
    """Se o arquivo já está no padrão 'Artista - Música - [id]', retorna (artista, musica, id). Senão None."""
    nome_sem_ext = os.path.splitext(os.path.basename(nome))[0]
    m = re.match(r'^(.+?)\s+-\s+(.+?)\s+-\s+\[([0-9a-f]{6})\]$', nome_sem_ext)
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3)
    return None

def listar_arquivos(pasta, recursivo=False):
    """Retorna caminhos relativos à pasta. Em modo recursivo inclui subpastas."""
    resultado = []
    if recursivo:
        for raiz, dirs, files in os.walk(pasta):
            dirs.sort()
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in EXTS:
                    resultado.append(os.path.relpath(os.path.join(raiz, f), pasta))
    else:
        resultado = sorted([
            f for f in os.listdir(pasta)
            if os.path.splitext(f)[1].lower() in EXTS
        ])
    return resultado

def limpar_nome(s):
    # Caracteres inválidos em NTFS/exFAT/FAT32
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', s)
    # Apóstrofos e aspas tipográficas (inválidos em exFAT no Linux)
    s = re.sub(r"['‘’‚‛`´]", '', s)
    s = re.sub(r'[“”„‟]', '', s)
    # exFAT não permite nome terminar com ponto ou espaço
    return s.strip().rstrip('.')

def e_sigla(palavra):
    p = palavra.strip()
    if re.match(r'^[A-Z]{2,5}$', p):
        return True
    if re.match(r'^([A-Z]\.){2,}$', p):
        return True
    if re.match(r'^[A-Z]{1,3}[/\\-][A-Z]{1,3}$', p):
        return True
    return False

def normalizar_case(s):
    if not s:
        return s
    letras = [ch for ch in s if ch.isalpha()]
    if not letras:
        return s
    pct = sum(1 for ch in letras if ch.isupper()) / len(letras)
    if pct <= 0.7:
        return s
    excecoes = {'de','do','da','dos','das','e','a','o','os','as',
                'em','no','na','nos','nas','por','para','com','sem',
                'the','an','of','in','on','at','by','for','and','or'}
    palavras = s.split()
    resultado = []
    for i, p in enumerate(palavras):
        if e_sigla(p):
            resultado.append(p)
        elif p.lower() in excecoes and i > 0:
            resultado.append(p.lower())
        else:
            resultado.append(p.capitalize())
    return ' '.join(resultado)

_RUIDOS = re.compile(
    r'\w{0,6}karaok[eê]'              # cckaraoke, karaoke, karaokê — para aqui, não come o que vem depois
    r'|playback|instrumental'
    r'|backing\s*track'
    r'|vers[aã]o|version'
    r'|oficial|official'
    r'|lyrics|legendado'
    r'|hd|4k|hq|fhd|1080p|720p|full'
    r'|\bcc\b|\bck\b',                # fragmentos de canal que sobram
    re.IGNORECASE
)

_CC_PREFIX = re.compile(r'^CC(?=[A-Z]{2,})', re.IGNORECASE)

def limpar_ruido(s):
    """Remove termos de karaokê/qualidade que o modelo às vezes deixa passar no artista/música."""
    s = _CC_PREFIX.sub('', s)   # CCBURNS→BURNS, CCAESPA→AESPA no output da IA
    s = _RUIDOS.sub('', s)
    s = re.sub(r'[\s\-–—_]{2,}', ' ', s)
    return s.strip(' -–—_')

def preparar_para_ia(nome_arquivo):
    """Limpa o nome antes de enviar à IA: remove subpasta, prefixo CC, colchetes e ruído."""
    nome = os.path.splitext(os.path.basename(nome_arquivo))[0]
    nome = re.sub(r'\[[^\]]*\]', ' ', nome)        # [qualquer coisa]
    nome = re.sub(r'\([^)]{0,30}\)', ' ', nome)    # (qualquer coisa curta)
    nome = _CC_PREFIX.sub('', nome)                 # CCAESPA→AESPA, CCBTS→BTS (mantém CCR)
    nome = _RUIDOS.sub(' ', nome)                   # karaoke, hd, cc standalone, playback…
    nome = re.sub(r'\s*-\s*', ' | ', nome)         # " - " explícito → " | " (preserva separador)
    nome = re.sub(r'[_/\\]+', ' ', nome)           # outros separadores → espaço
    nome = re.sub(r'\s{2,}', ' ', nome).strip()
    return nome

def formatar_nome(artista, musica, ext, nome_original=""):
    id_hex = gerar_id(nome_original or artista + musica)
    return f"{limpar_nome(normalizar_case(artista))} - {limpar_nome(normalizar_case(musica))} - [{id_hex}]{ext}"

_itunes_falhas = 0      # circuit breaker: falhas consecutivas
_ITUNES_MAX_FALHAS = 3  # desativa após N falhas seguidas

def buscar_itunes(query, timeout=3):
    """Busca artista/música na iTunes Search API. Desativa após 3 falhas seguidas."""
    global _itunes_falhas
    if _itunes_falhas >= _ITUNES_MAX_FALHAS:
        return None
    try:
        query_limpa = query.replace(" | ", " ").replace("|", " ").strip()
        params = urllib.parse.urlencode({
            "term": query_limpa, "entity": "song", "limit": 3, "country": "BR"
        })
        url = f"https://itunes.apple.com/search?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "VoxlyRenomeador/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        if not results:
            _itunes_falhas += 1
            return None
        _itunes_falhas = 0  # reset no sucesso
        top = results[0]
        return {"artista": top["artistName"], "musica": top["trackName"]}
    except Exception:
        _itunes_falhas += 1
        return None

def extrair_fonte(arquivo_relativo):
    """Usa subpasta como fonte (canal); se não houver, tenta extrair do nome do arquivo."""
    partes = arquivo_relativo.replace("\\", "/").split("/")
    if len(partes) > 1:
        return partes[0]
    nome  = os.path.splitext(partes[-1])[0]
    limpo = re.sub(r'\[[A-Za-z0-9_-]{11}\]', '', nome)
    limpo = re.sub(r'\[[0-9a-f]{6}\]',        '', limpo)
    colchetes = re.findall(r'\[([^\]]{2,40})\]', limpo)
    if colchetes:
        return colchetes[-1].strip()
    ignorar = re.compile(
        r'^(hd|4k|hq|fhd|1080p|720p|oficial|official|karaok[eê]|instrumental|'
        r'playback|lyrics|legendado|backing.?track|\d+)$', re.I)
    parens = [p.strip() for p in re.findall(r'\(([^)]{2,40})\)', limpo)
              if not ignorar.match(p.strip())]
    if parens:
        return parens[-1]
    return "Desconhecida"

PROMPT = """Você é especialista em música brasileira e internacional. \
Extraia o ARTISTA e o TÍTULO DA MÚSICA a partir do nome de arquivo abaixo. \
O nome já foi pré-processado: palavras como karaoke, playback, HD e nomes de canal foram removidas.

Nome: {nome}

REGRAS:
1. Retorne APENAS artista e música. Nunca inclua: karaoke, karaokê, playback, HD, 4K, oficial, \
instrumental, backing track, legendado, "CC" ou qualquer palavra que não seja artista ou título.
2. Se o nome ainda começar com "CC" seguido do artista (CCAESPA, CCBTS, CCCASH, CCBURNS, CCBEABADOOBEE), \
"CC" é o prefixo do canal — ignore-o e use apenas o nome do artista que vem depois.
3. Use a grafia OFICIAL do artista:
   - Siglas em maiúsculo: ABBA, AC/DC, RPM, NX Zero, RBD, CPM 22, KLB, É o Tchan
   - Nomes normais: Michael Jackson, Sandy & Junior, Exaltasamba, Chitãozinho & Xororó,
     Roberto Carlos, Caetano Veloso, Zeca Pagodinho, Thiaguinho, Luan Santana
   - Internacionais: Bee Gees, ZZ Top, R.E.M., KISS, INXS, U2, Guns N' Roses
4. Título da música em Title Case: "Evidências" não "EVIDÊNCIAS".
5. Se houver só o artista e nenhum título reconhecível, coloque musica como "Desconhecida".
6. A ORDEM no nome do arquivo pode ser "Artista Música" OU "Música Artista" — use seu conhecimento \
musical para identificar corretamente qual é o artista e qual é a música, INDEPENDENTE da posição. \
Nunca assuma que o primeiro elemento é sempre o artista.
7. Se o nome contém " | ", esse símbolo é um separador explícito entre dois campos. O formato pode ser \
"Artista | Música" ou "Música | Artista". Use seu conhecimento para identificar a ordem correta.

EXEMPLOS — ordem normal (artista primeiro):
"Exaltasamba Voce Me Completa"         → {{"artista": "Exaltasamba", "musica": "Você Me Completa"}}
"Sandy Junior Era Uma Vez"             → {{"artista": "Sandy & Junior", "musica": "Era Uma Vez"}}
"Queen Bohemian Rhapsody"              → {{"artista": "Queen", "musica": "Bohemian Rhapsody"}}
"ABBA Dancing Queen"                   → {{"artista": "ABBA", "musica": "Dancing Queen"}}
"BTS Butter"                           → {{"artista": "BTS", "musica": "Butter"}}
"AESPA Supernova"                      → {{"artista": "aespa", "musica": "Supernova"}}

EXEMPLOS — ordem invertida (música primeiro, artista depois):
"Bohemian Rhapsody Queen"              → {{"artista": "Queen", "musica": "Bohemian Rhapsody"}}
"Dancing Queen ABBA"                   → {{"artista": "ABBA", "musica": "Dancing Queen"}}
"Thriller Michael Jackson"             → {{"artista": "Michael Jackson", "musica": "Thriller"}}
"Highway to Hell AC DC"                → {{"artista": "AC/DC", "musica": "Highway to Hell"}}
"Evidencias Chitaozinho Xororo"        → {{"artista": "Chitãozinho & Xororó", "musica": "Evidências"}}
"Deixa A Vida Me Levar Zeca Pagodinho" → {{"artista": "Zeca Pagodinho", "musica": "Deixa a Vida Me Levar"}}
"Butter BTS"                           → {{"artista": "BTS", "musica": "Butter"}}
"Supernova AESPA"                      → {{"artista": "aespa", "musica": "Supernova"}}

EXEMPLOS — com separador | (pode ser qualquer ordem):
"Deus Perdoa | Filipe Ret"             → {{"artista": "Filipe Ret", "musica": "Deus Perdoa"}}
"Filipe Ret | Deus Perdoa"             → {{"artista": "Filipe Ret", "musica": "Deus Perdoa"}}
"Coldplay | The Scientist"             → {{"artista": "Coldplay", "musica": "The Scientist"}}
"The Scientist | Coldplay"             → {{"artista": "Coldplay", "musica": "The Scientist"}}

Responda APENAS com JSON válido, sem texto extra: {{"artista": "...", "musica": "..."}}"""

# ── Gerenciador de fallback ──────────────────────────────────
class GerenciadorFallback:
    """
    Estrutura:
      entradas = [
        { provedor, url, chave, modelos: [m1, m2, ...] },
        ...
      ]
    Tenta: entrada[0]/modelo[0] → entrada[0]/modelo[1] → ... → entrada[1]/modelo[0] → ...
    """
    def __init__(self, entradas):
        # Expande: uma entrada por (chave, modelo)
        self.slots = []
        for e in entradas:
            modelos = e.get("modelos_custom") or PROVEDORES.get(e["provedor"], {}).get("modelos", [])
            for modelo in modelos:
                self.slots.append({
                    "provedor": e["provedor"],
                    "url":      PROVEDORES[e["provedor"]]["url"],
                    "chave":    e["chave"],
                    "modelo":   modelo,
                })
        self.idx = 0
        self.log = []

    def interpretar(self, nome_arquivo, nome_limpo=None):
        if nome_limpo is None:
            nome_limpo = preparar_para_ia(nome_arquivo)
            self.log.append(f"📤 Enviado à IA: {nome_limpo!r}")
        total      = len(self.slots)
        inicio     = self.idx % total

        for i in range(total):
            idx_atual = (inicio + i) % total
            slot      = self.slots[idx_atual]
            try:
                cliente = OpenAI(api_key=slot["chave"], base_url=slot["url"])
                resp    = cliente.chat.completions.create(
                    model=slot["modelo"],
                    max_tokens=200,
                    temperature=0,
                    messages=[{"role": "user", "content": PROMPT.format(nome=nome_limpo)}]
                )
                texto = (resp.choices[0].message.content or "").strip()
                if not texto:
                    self.log.append(f"⚠️ Resposta vazia: {slot['provedor']}/{slot['modelo']}")
                    self.idx = (idx_atual + 1) % total
                    continue
                texto = re.sub(r'```[a-z]*', '', texto).replace('```', '').strip()
                texto = re.sub(r'\\([^"\\/bfnrtu])', r'\1', texto)  # remove escapes inválidos ex: \&
                resultado, _ = json.JSONDecoder().raw_decode(texto)
                self.log.append(f"✅ {slot['provedor']}/{slot['modelo']}: {nome_arquivo}")
                # Se o slot que funcionou é remoto, volta para slot 0 (local/Ollama) na próxima
                self.idx = 0 if "localhost" not in slot["url"] else idx_atual
                return resultado

            except RateLimitError:
                self.log.append(f"⚠️ Rate limit: {slot['provedor']}/{slot['modelo']}")
                self.idx = (idx_atual + 1) % total  # avança permanentemente

            except (json.JSONDecodeError, KeyError) as e:
                self.log.append(f"⚠️ JSON inválido: {slot['provedor']}/{slot['modelo']}: {e}")
                # não avança — JSON ruim pode ser erro pontual do modelo

            except APIError as e:
                self.log.append(f"⚠️ API error: {slot['provedor']}/{slot['modelo']}: {e}")
                self.idx = (idx_atual + 1) % total

            except Exception as e:
                self.log.append(f"❌ Erro: {slot['provedor']}/{slot['modelo']}: {e}")
                self.idx = (idx_atual + 1) % total

        raise Exception(f"Todos os {total} slots falharam para: {nome_arquivo}")

    def status(self):
        if not self.slots:
            return "Sem slots configurados"
        slot = self.slots[self.idx % len(self.slots)]
        return f"{slot['provedor']} / {slot['modelo']}"

# ── Verificador de Integridade ───────────────────────────────
class IntegridadeWindow(tk.Toplevel):
    def __init__(self, parent, pasta, download_callback):
        super().__init__(parent)
        self.pasta             = pasta
        self.download_callback = download_callback   # fn(urls) → inicia download
        self._corrompidos      = []   # [(filepath, url_ou_None, vid_id_ou_None)]
        self._vars_sel         = []
        self.title("🔍 Verificador de Integridade")
        self.geometry("920x640")
        self.minsize(700, 480)
        self.configure(bg="#0d0d1a")
        self.grab_set()
        self._build_ui()
        self.after(300, self._iniciar)

    def _build_ui(self):
        hdr = tk.Frame(self, bg="#16213e", pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🔍 Verificador de Integridade",
                 font=("Segoe UI", 13, "bold"), bg="#16213e", fg="#e040fb").pack(side="left", padx=20)
        tk.Label(hdr, text="usa ffmpeg -v error para detectar arquivos corrompidos",
                 font=("Segoe UI", 8), bg="#16213e", fg="#555577").pack(side="left", padx=4)

        # Status + barra
        frame_prog = tk.Frame(self, bg="#0d0d1a")
        frame_prog.pack(fill="x", padx=16, pady=(10, 4))
        self.lbl_status = tk.Label(frame_prog, text="Aguardando…",
                                    bg="#0d0d1a", fg="#aaaacc",
                                    font=("Segoe UI", 9), anchor="w")
        self.lbl_status.pack(fill="x")
        self.progress = ttk.Progressbar(frame_prog, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 0))

        # Lista de corrompidos
        tk.Label(self, text="Arquivos com erros detectados:",
                 bg="#0d0d1a", fg="#555577",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(8, 2))

        frame_lista = tk.Frame(self, bg="#0d0d1a")
        frame_lista.pack(fill="both", expand=True, padx=16)

        vsb = tk.Scrollbar(frame_lista)
        vsb.pack(side="right", fill="y")
        self._canvas = tk.Canvas(frame_lista, bg="#0d0d1a", highlightthickness=0,
                                  yscrollcommand=vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb.config(command=self._canvas.yview)
        self._frame_itens = tk.Frame(self._canvas, bg="#0d0d1a")
        self._canvas.create_window((0, 0), window=self._frame_itens, anchor="nw")
        self._frame_itens.bind("<Configure>",
                                lambda e: self._canvas.configure(
                                    scrollregion=self._canvas.bbox("all")))

        # Rodapé
        rod = tk.Frame(self, bg="#16213e", pady=8)
        rod.pack(fill="x", side="bottom")
        self.lbl_resumo = tk.Label(rod, text="", bg="#16213e", fg="#aaaacc",
                                    font=("Segoe UI", 9))
        self.lbl_resumo.pack(side="left", padx=16)
        tk.Button(rod, text="✅ Selecionar todos",
                  command=self._sel_todos,
                  bg="#1a1a2e", fg="#aaaacc", relief="flat",
                  font=("Segoe UI", 9), cursor="hand2",
                  padx=10, pady=5).pack(side="left", padx=8)
        self.btn_rebaixar = tk.Button(rod, text="⬇ Re-baixar selecionados",
                                       command=self._rebaixar,
                                       bg="#7c4dff", fg="white", relief="flat",
                                       font=("Segoe UI", 10, "bold"),
                                       cursor="hand2", padx=14, pady=5,
                                       state="disabled")
        self.btn_rebaixar.pack(side="right", padx=16)
        tk.Button(rod, text="Fechar", command=self.destroy,
                  bg="#37474f", fg="white", relief="flat",
                  font=("Segoe UI", 9), cursor="hand2",
                  padx=10, pady=5).pack(side="right", padx=4)

    def _iniciar(self):
        import queue as _queue
        q = _queue.Queue()

        def _poll():
            try:
                while True:
                    item = q.get_nowait()
                    t = item[0]
                    if t == "total":
                        self.progress.config(maximum=max(item[1], 1))
                        self.lbl_status.config(text=f"Verificando 0 / {item[1]} arquivos…")
                    elif t == "prog":
                        _, v, tot, nome = item
                        pct = int(v / tot * 100)
                        self.progress.config(value=pct)
                        self.lbl_status.config(text=f"Verificando {v}/{tot} — {nome[:70]}")
                    elif t == "erro":
                        _, fp, url, vid_id, detalhes = item
                        self._adicionar_item(fp, url, vid_id, detalhes)
                    elif t == "fim":
                        n = len(self._corrompidos)
                        self.lbl_status.config(text=f"✅ Concluído — {n} arquivo(s) com erro" if n else "✅ Todos os arquivos estão íntegros")
                        self.progress.config(value=self.progress["maximum"])
                        self.lbl_resumo.config(text=f"{n} corrompido(s) encontrado(s)")
                        if n:
                            self.btn_rebaixar.config(state="normal")
                        return
            except _queue.Empty:
                pass
            self.after(120, _poll)

        def _run():
            arquivos = []
            for root, _, files in os.walk(self.pasta):
                for f in files:
                    if os.path.splitext(f)[1].lower() in EXTS:
                        arquivos.append(os.path.join(root, f))

            q.put(("total", len(arquivos)))

            # Índice título→(url, id) do DB para re-download
            db = db_carregar()
            idx_titulo = {}   # titulo_norm → (url, vid_id)
            for canal_data in db.get("canais", {}).values():
                for v in canal_data.get("videos", []):
                    tn = _normalizar_titulo(v.get("titulo", ""))
                    if tn:
                        idx_titulo[tn] = (v.get("url", ""), v.get("id", ""))

            for i, fp in enumerate(arquivos):
                nome = os.path.basename(fp)
                q.put(("prog", i + 1, len(arquivos), nome))

                # ffmpeg -v error: decodifica o arquivo e reporta erros de stream
                try:
                    res = subprocess.run(
                        ["ffmpeg", "-v", "error", "-i", fp, "-f", "null", "-"],
                        capture_output=True, text=True, timeout=120
                    )
                    erros = [ln.strip() for ln in res.stderr.splitlines()
                             if re.search(r'error|invalid|corrupt|truncat|missing|moov', ln, re.I)
                             and "encoder" not in ln.lower()]
                except subprocess.TimeoutExpired:
                    erros = ["Timeout — arquivo muito longo ou corrompido"]
                except FileNotFoundError:
                    q.put(("fim",))
                    return
                except Exception as e:
                    erros = [str(e)]

                if erros:
                    # Tenta encontrar URL no DB pelo título normalizado
                    tnorm = _normalizar_titulo(os.path.splitext(nome)[0])
                    url, vid_id = None, None
                    for tn, (u, vid) in idx_titulo.items():
                        if len(tnorm) > 8 and len(tn) > 8 and (tn in tnorm or tnorm in tn):
                            url, vid_id = u, vid
                            break
                    q.put(("erro", fp, url, vid_id, erros[:3]))

            q.put(("fim",))

        self.after(120, _poll)
        threading.Thread(target=_run, daemon=True).start()

    def _adicionar_item(self, fp, url, vid_id, detalhes):
        var = tk.BooleanVar(value=bool(url))
        self._vars_sel.append((var, fp, url, vid_id))
        self._corrompidos.append((fp, url, vid_id))

        row = tk.Frame(self._frame_itens, bg="#16213e", pady=4)
        row.pack(fill="x", pady=2, padx=4)

        tk.Checkbutton(row, variable=var,
                        bg="#16213e", fg="white", selectcolor="#0d0d1a",
                        activebackground="#16213e",
                        command=self._atualizar_btn).pack(side="left", padx=(4, 0))

        info = tk.Frame(row, bg="#16213e")
        info.pack(side="left", fill="x", expand=True, padx=6)
        cor = "#00e676" if url else "#ff7043"
        tk.Label(info, text=os.path.basename(fp),
                 bg="#16213e", fg="#e8e8ff",
                 font=("Consolas", 8, "bold"), anchor="w").pack(fill="x")
        tk.Label(info, text=detalhes[0] if detalhes else "",
                 bg="#16213e", fg="#ff5252",
                 font=("Consolas", 7), anchor="w").pack(fill="x")
        tk.Label(info,
                 text=f"🔗 {url}" if url else "⚠️ URL não encontrada no DB — re-baixe manualmente",
                 bg="#16213e", fg=cor,
                 font=("Segoe UI", 7, "italic"), anchor="w").pack(fill="x")

    def _sel_todos(self):
        for var, *_ in self._vars_sel:
            var.set(True)
        self._atualizar_btn()

    def _atualizar_btn(self):
        sel = sum(1 for var, _, url, __ in self._vars_sel if var.get() and url)
        self.btn_rebaixar.config(
            state="normal" if sel else "disabled",
            text=f"⬇ Re-baixar {sel} selecionado(s)" if sel else "⬇ Re-baixar selecionados"
        )

    def _rebaixar(self):
        selecionados = [(fp, url, vid_id)
                        for var, fp, url, vid_id in self._vars_sel
                        if var.get() and url]
        if not selecionados:
            return

        # Remove do archive para permitir re-download
        ids_remover = {vid_id for _, __, vid_id in selecionados if vid_id}
        if ids_remover:
            try:
                with open(YT_ARCHIVE, encoding="utf-8") as fh:
                    linhas = fh.readlines()
                novas = [ln for ln in linhas
                         if ln.strip() and ln.strip().split()[-1] not in ids_remover]
                with open(YT_ARCHIVE, "w", encoding="utf-8") as fh:
                    fh.writelines(novas)
            except FileNotFoundError:
                pass

        # Apaga os arquivos corrompidos antes de re-baixar
        for fp, _, __ in selecionados:
            try:
                os.remove(fp)
            except Exception:
                pass

        urls = [url for _, url, __ in selecionados]
        self.destroy()
        self.download_callback(urls)


# ── Janela de Duplicatas ─────────────────────────────────────
class DuplicatasWindow(tk.Toplevel):
    def __init__(self, parent, pasta, grupos):
        """grupos: {(artista, musica): [resultado_dict, ...]}"""
        super().__init__(parent)
        self.parent = parent
        self.pasta  = pasta
        self.grupos = grupos
        self._vars  = {}  # {chave: IntVar(índice a manter)}

        self.title("🔁 Duplicatas")
        self.geometry("1100x700")
        self.configure(bg="#0d0d1a")
        self.grab_set()
        self._build_ui()

    def _build_ui(self):
        n_g = len(self.grupos)
        n_a = sum(len(v) for v in self.grupos.values())

        hdr = tk.Frame(self, bg="#16213e", pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"🔁 Duplicatas — {n_g} grupo(s), {n_a} arquivo(s)",
                 font=("Segoe UI", 13, "bold"), bg="#16213e", fg="#e040fb").pack(side="left", padx=20)
        tk.Label(hdr, text="Selecione a versão a MANTER em cada grupo. As demais serão excluídas.",
                 font=("Segoe UI", 8), bg="#16213e", fg="#555577").pack(side="left", padx=8)

        container = tk.Frame(self, bg="#0d0d1a")
        container.pack(fill="both", expand=True, padx=8, pady=6)

        canvas = tk.Canvas(container, bg="#0d0d1a", highlightthickness=0)
        vsb    = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner  = tk.Frame(canvas, bg="#0d0d1a")
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))

        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll( 1, "units"))
        self.protocol("WM_DELETE_WINDOW", lambda: (
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>"),
            self.destroy()
        ))

        for chave, versoes in self.grupos.items():
            self._build_grupo(inner, chave, versoes)

        rod = tk.Frame(self, bg="#16213e", pady=10)
        rod.pack(fill="x", side="bottom")
        self.lbl_resumo = tk.Label(rod, text="", bg="#16213e", fg="#7a7a9d",
                                    font=("Segoe UI", 9))
        self.lbl_resumo.pack(side="left", padx=16)
        tk.Button(rod, text="🗑 Excluir Todas as Não Selecionadas",
                  command=self._excluir_todas,
                  bg="#c62828", fg="white", font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=16, pady=6, cursor="hand2").pack(side="right", padx=16)
        self._atualizar_resumo()

    def _build_grupo(self, parent, chave, versoes):
        artista, musica = chave
        frame_g = tk.LabelFrame(parent,
                                 text=f"  {artista} — {musica}  ({len(versoes)} versões)  ",
                                 bg="#16213e", fg="#e040fb",
                                 font=("Segoe UI", 10, "bold"),
                                 pady=6, padx=10)
        frame_g.pack(fill="x", padx=8, pady=6, ipadx=4)

        var = tk.IntVar(value=0)
        self._vars[chave] = var

        for i, r in enumerate(versoes):
            fonte = extrair_fonte(r["arquivo"])
            try:
                tam     = os.path.getsize(os.path.join(self.pasta, r["arquivo"]))
                tam_str = f"{tam / 1_048_576:.1f} MB"
            except Exception:
                tam_str = "—"

            linha = tk.Frame(frame_g, bg="#1a1a2e", pady=4)
            linha.pack(fill="x", pady=2, padx=2)

            tk.Radiobutton(linha, variable=var, value=i,
                           bg="#1a1a2e", fg="#e8e8ff",
                           selectcolor="#2a2a4e",
                           activebackground="#1a1a2e",
                           command=self._atualizar_resumo).pack(side="left", padx=(6, 2))

            info = tk.Frame(linha, bg="#1a1a2e")
            info.pack(side="left", fill="x", expand=True, padx=4)

            tk.Label(info, text=r["arquivo"],
                     font=("Segoe UI", 9, "bold"),
                     bg="#1a1a2e", fg="#e8e8ff", anchor="w").pack(fill="x")

            sub = tk.Frame(info, bg="#1a1a2e")
            sub.pack(fill="x")
            tk.Label(sub, text=f"Fonte: {fonte}",
                     font=("Segoe UI", 8), bg="#1a1a2e", fg="#7c4dff").pack(side="left")
            tk.Label(sub, text=f"   {tam_str}",
                     font=("Segoe UI", 8), bg="#1a1a2e", fg="#555577").pack(side="left")

            caminho = os.path.join(self.pasta, r["arquivo"])
            tk.Button(linha, text="▶ Play",
                      command=lambda p=caminho: self._play(p),
                      bg="#0d0d1a", fg="#00e676",
                      font=("Segoe UI", 8, "bold"),
                      relief="flat", padx=8, pady=3,
                      cursor="hand2").pack(side="right", padx=6)

        tk.Button(frame_g,
                  text="🗑 Excluir não selecionadas deste grupo",
                  command=lambda c=chave, v=versoes, fw=frame_g: self._excluir_grupo(c, v, fw),
                  bg="#37474f", fg="white",
                  font=("Segoe UI", 8, "bold"),
                  relief="flat", padx=8, pady=3,
                  cursor="hand2").pack(anchor="e", pady=(6, 2))

    def _play(self, caminho):
        try:
            if os.name == "nt":
                os.startfile(caminho)
            else:
                subprocess.Popen(["xdg-open", caminho])
        except Exception as e:
            messagebox.showerror("Erro ao reproduzir",
                                  f"Não foi possível abrir:\n{caminho}\n\n{e}", parent=self)

    def _excluir_grupo(self, chave, versoes, frame_widget):
        idx_keep  = self._vars[chave].get()
        a_excluir = [r for i, r in enumerate(versoes) if i != idx_keep]
        if not a_excluir:
            return
        nomes = "\n".join(f"  • {r['arquivo']}" for r in a_excluir)
        if not messagebox.askyesno("Confirmar exclusão",
                                    f"Excluir {len(a_excluir)} arquivo(s)?\n\n{nomes}",
                                    parent=self):
            return
        ok = erros = 0
        for r in a_excluir:
            try:
                os.remove(os.path.join(self.pasta, r["arquivo"]))
                self._marcar_excluido(r)
                ok += 1
            except Exception as e:
                messagebox.showerror("Erro", f"{r['arquivo']}\n{e}", parent=self)
                erros += 1
        frame_widget.destroy()
        del self.grupos[chave]
        del self._vars[chave]
        self._atualizar_resumo()
        if ok:
            messagebox.showinfo("Concluído",
                                 f"✅ {ok} excluído(s)  ❌ {erros} erro(s)", parent=self)

    def _excluir_todas(self):
        a_excluir = []
        for chave, versoes in self.grupos.items():
            idx_keep = self._vars[chave].get()
            a_excluir.extend(r for i, r in enumerate(versoes) if i != idx_keep)
        if not a_excluir:
            messagebox.showinfo("Nada a fazer", "Nenhum arquivo para excluir.", parent=self)
            return
        if not messagebox.askyesno("Confirmar exclusão",
                                    f"Excluir {len(a_excluir)} arquivo(s) não selecionados?",
                                    parent=self):
            return
        ok = erros = 0
        for r in a_excluir:
            try:
                os.remove(os.path.join(self.pasta, r["arquivo"]))
                self._marcar_excluido(r)
                ok += 1
            except Exception as e:
                erros += 1
        messagebox.showinfo("Concluído",
                             f"✅ {ok} excluído(s)  ❌ {erros} erro(s)", parent=self)
        self.destroy()

    def _marcar_excluido(self, r):
        try:
            idx = self.parent.resultados.index(r)
            r["status"] = "🗑 Excluído"
            r["sel"]    = False
            self.parent.tree.item(
                str(idx),
                values=("☐", r["arquivo"], r["artista"], r["musica"], r["novo_nome"], "🗑 Excluído"),
                tags=("erro",))
        except (ValueError, tk.TclError):
            pass

    def _atualizar_resumo(self):
        total   = sum(len(v) for v in self.grupos.values())
        manter  = len(self.grupos)
        excluir = total - manter
        self.lbl_resumo.config(
            text=f"{manter} grupo(s) · {total} arquivo(s) · {excluir} para excluir")


# ── Download Window ─────────────────────────────────────────
import shutil as _shutil
_FFMPEG_OK = bool(_shutil.which("ffmpeg"))

_RE_STREAM_TEMP  = re.compile(r'\.f\d+\.')   # ex: Song.f137.mp4 — stream intermediária antes do merge
_RE_ANSI         = re.compile(r'(\x1b\[|\[)[0-9;]*m')  # códigos de cor ANSI / yt-dlp bare brackets
_RE_ARCHIVE_SKIP = re.compile(r'\[download\]\s+\S+:\s+(.+?)\s+has already been (recorded|downloaded)')

def _strip_ansi(s):
    return _RE_ANSI.sub('', s)

class _YDLCancelavel(yt_dlp.YoutubeDL):
    """Subclasse de YoutubeDL com cancelamento, progresso de playlist e log de skips."""
    cancelar             = False
    on_playlist_progress = None   # callback(index, total)
    on_skip              = None   # callback(msg) para vídeos pulados

    def report_progress(self, s):
        if self.cancelar:
            raise yt_dlp.utils.DownloadCancelled("Cancelado pelo usuário")
        super().report_progress(s)

    def to_screen(self, message, *args, **kwargs):
        if callable(self.on_skip):
            limpa = _strip_ansi(message)
            low   = limpa.lower()
            if "already been recorded" in low or "has already been downloaded" in low:
                # Extrai só o título do vídeo da mensagem
                m = _RE_ARCHIVE_SKIP.search(limpa)
                self.on_skip(m.group(1) if m else limpa.strip())
        super().to_screen(message, *args, **kwargs)

    def process_video_result(self, info_dict, download=True):
        """Captura índice/total da playlist antes de baixar cada vídeo."""
        if callable(self.on_playlist_progress):
            idx   = info_dict.get("playlist_index")
            total = info_dict.get("n_entries") or info_dict.get("playlist_count")
            if idx and total:
                self.on_playlist_progress(int(idx), int(total))
        return super().process_video_result(info_dict, download=download)

class DownloadWindow(tk.Frame):
    # (label, formato_yt_dlp, postprocessors, requer_ffmpeg)
    QUALIDADES = [
        ("📺 Vídeo — 1080p",                   "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",   [],                                                                               True),
        ("📺 Vídeo — 720p",                    "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",     [],                                                                               True),
        ("📺 Vídeo — Melhor disponível",        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",           [],                                                                               True),
        ("📺 Vídeo — 480p",                    "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",     [],                                                                               True),
        ("📺 Vídeo — 360p",                    "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",     [],                                                                               True),
        ("📺 Vídeo — sem ffmpeg (até 720p)",   "best[ext=mp4]/best",                                                       [],                                                                               False),
        ("🎵 Áudio — mp3 192k",                "bestaudio/best",                                                            [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}],  True),
        ("🎵 Áudio — webm (sem conversão)",    "bestaudio[ext=webm]/bestaudio/best",                                        [],                                                                               False),
    ]

    def __init__(self, parent, app):
        super().__init__(parent, bg="#0d0d1a")
        self.parent       = app   # compatibilidade com código existente
        self._cancelar    = False
        self._ydl_ativo   = None

        if app.pasta:
            set_pasta_musicas(app.pasta)

        self._build_ui()

    def _build_ui(self):
        hdr = tk.Frame(self, bg="#16213e", pady=6)
        hdr.pack(fill="x")
        cor_ff = "#00e676" if _FFMPEG_OK else "#ff1744"
        txt_ff = "ffmpeg OK" if _FFMPEG_OK else "⚠ ffmpeg não instalado"
        tk.Label(hdr, text="⬇ Download — yt-dlp",
                 font=("Segoe UI", 11, "bold"), bg="#16213e", fg="#e040fb").pack(side="left", padx=16)
        tk.Label(hdr, text=txt_ff, font=("Segoe UI", 8), bg="#16213e", fg=cor_ff).pack(side="left", padx=8)

        # URLs — lista dinâmica de entradas
        frame_url = tk.LabelFrame(self, text=" Links para download ",
                                   bg="#16213e", fg="#aaaacc",
                                   font=("Segoe UI", 9, "bold"), pady=8, padx=10)
        frame_url.pack(fill="x", padx=16, pady=(10, 6))

        self._frame_lista_urls = tk.Frame(frame_url, bg="#16213e")
        self._frame_lista_urls.pack(fill="x")
        self._url_vars = []   # list of tk.StringVar

        # linha com botão +
        frame_add = tk.Frame(frame_url, bg="#16213e")
        frame_add.pack(fill="x", pady=(6, 0))
        tk.Button(frame_add, text="＋ Adicionar link",
                  command=self._adicionar_url,
                  bg="#1a1a2e", fg="#7c4dff", relief="flat",
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=10, pady=3).pack(side="left")

        # começa com uma entrada
        self._adicionar_url()

        # Qualidade + Destino
        frame_opts = tk.Frame(self, bg="#0d0d1a")
        frame_opts.pack(fill="x", padx=16, pady=6)
        frame_opts.columnconfigure(1, weight=1)

        tk.Label(frame_opts, text="Qualidade:", bg="#0d0d1a", fg="#aaaacc",
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", padx=(0,8), pady=5)
        self.var_qualidade = tk.StringVar()
        self._cb_qualidade = ttk.Combobox(
            frame_opts, textvariable=self.var_qualidade,
            values=[q[0] for q in self.QUALIDADES],
            state="readonly", width=42, font=("Segoe UI", 9))
        self._cb_qualidade.grid(row=0, column=1, sticky="w", pady=5)
        self._cb_qualidade.current(0)
        self._cb_qualidade.bind("<<ComboboxSelected>>", self._checar_ffmpeg)

        tk.Label(frame_opts, text="Destino:", bg="#0d0d1a", fg="#aaaacc",
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", padx=(0,8), pady=5)
        frame_pasta = tk.Frame(frame_opts, bg="#0d0d1a")
        frame_pasta.grid(row=1, column=1, sticky="ew", pady=5)
        self.var_pasta = tk.StringVar(value=self.parent.pasta or os.path.expanduser("~"))
        tk.Entry(frame_pasta, textvariable=self.var_pasta, bg="#1a1a2e", fg="#e8e8ff",
                 insertbackground="white", relief="flat", font=("Segoe UI", 9),
                 width=44).pack(side="left")
        tk.Button(frame_pasta, text="📁 Alterar", command=self._escolher_pasta,
                  bg="#37474f", fg="white", relief="flat", padx=8, pady=2,
                  font=("Segoe UI", 8), cursor="hand2").pack(side="left", padx=(6,0))
        tk.Button(frame_pasta, text="🗂 Reorganizar por artista",
                  command=self._reorganizar_retroativo,
                  bg="#1a1a3a", fg="#b388ff", relief="flat", padx=8, pady=2,
                  font=("Segoe UI", 8), cursor="hand2").pack(side="left", padx=(6,0))

        tk.Label(frame_opts,
                 text="Todos os arquivos vão direto para a pasta selecionada (sem subpastas por canal)",
                 bg="#0d0d1a", fg="#555577",
                 font=("Segoe UI", 8, "italic")).grid(row=2, column=1, sticky="w")

        # Cookies do navegador (necessário para canais com 1000+ vídeos)
        frame_cookies = tk.Frame(self, bg="#0d0d1a")
        frame_cookies.pack(fill="x", padx=16, pady=(8, 2))
        self.var_usar_cookies = tk.BooleanVar(value=True)
        tk.Checkbutton(frame_cookies,
                       text="🍪 Cookies do navegador",
                       variable=self.var_usar_cookies,
                       bg="#0d0d1a", fg="#ffab40", selectcolor="#1a1a2e",
                       activebackground="#0d0d1a", activeforeground="#e8e8ff",
                       font=("Segoe UI", 9, "bold"), cursor="hand2",
                       command=self._toggle_cookies).pack(side="left")
        self.var_navegador = tk.StringVar(value="chrome")
        self._cb_navegador = ttk.Combobox(
            frame_cookies, textvariable=self.var_navegador,
            values=["chrome", "firefox", "chromium", "edge", "opera", "brave", "vivaldi", "safari"],
            state="readonly", width=12, font=("Segoe UI", 9))
        self._cb_navegador.pack(side="left", padx=4)

        # Opções de pós-processamento — linha única com checkboxes
        frame_opts2 = tk.Frame(self, bg="#0d0d1a")
        frame_opts2.pack(fill="x", padx=16, pady=(4, 2))

        self.var_renomear = tk.BooleanVar(value=True)
        tk.Checkbutton(frame_opts2,
                       text="✅ Renomear (IA)",
                       variable=self.var_renomear,
                       bg="#0d0d1a", fg="#00e676", selectcolor="#1a1a2e",
                       activebackground="#0d0d1a", activeforeground="#e8e8ff",
                       font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left")

        self.var_organizar_artista = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_opts2,
                       text="📂 Organizar por artista",
                       variable=self.var_organizar_artista,
                       bg="#0d0d1a", fg="#40c4ff", selectcolor="#1a1a2e",
                       activebackground="#0d0d1a", activeforeground="#e8e8ff",
                       font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left", padx=(16, 0))

        self.var_incremental = tk.BooleanVar(value=False)
        frame_inc = tk.Frame(self, bg="#0d0d1a")
        frame_inc.pack(anchor="w", padx=16, pady=(0, 2))
        tk.Checkbutton(frame_inc,
                       text="⚡ Modo incremental",
                       variable=self.var_incremental,
                       bg="#0d0d1a", fg="#ffab40", selectcolor="#1a1a2e",
                       activebackground="#0d0d1a", activeforeground="#e8e8ff",
                       font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left")
        tk.Label(frame_inc,
                 text="— para ao encontrar o 1º vídeo já baixado (ideal para sincronizar novidades)",
                 bg="#0d0d1a", fg="#555577",
                 font=("Segoe UI", 8, "italic")).pack(side="left", padx=(4, 0))

        # Botões
        frame_btns = tk.Frame(self, bg="#0d0d1a")
        frame_btns.pack(fill="x", padx=16, pady=4)
        self.btn_baixar = tk.Button(frame_btns, text="⬇ Baixar",
                                     command=self._iniciar,
                                     bg="#7c4dff", fg="white", relief="flat",
                                     padx=16, pady=7,
                                     font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.btn_baixar.pack(side="left")
        self.btn_parar = tk.Button(frame_btns, text="⏹ Parar",
                                    command=self._parar,
                                    bg="#c62828", fg="white", relief="flat",
                                    padx=16, pady=7,
                                    font=("Segoe UI", 10, "bold"), cursor="hand2",
                                    state="disabled")
        self.btn_parar.pack(side="left", padx=8)
        self.btn_mapear = tk.Button(frame_btns, text="🗺 Mapear canal",
                                     command=self._iniciar_mapeamento,
                                     bg="#1a3a4a", fg="#40c4ff", relief="flat",
                                     padx=12, pady=7,
                                     font=("Segoe UI", 9), cursor="hand2")
        self.btn_mapear.pack(side="left")
        self.lbl_mapa_status = tk.Label(frame_btns, text="",
                                         bg="#0d0d1a", fg="#00e676",
                                         font=("Segoe UI", 8, "italic"))
        self.lbl_mapa_status.pack(side="left", padx=(8, 0))

        self.btn_scan = tk.Button(frame_btns, text="🎵 Escanear músicas",
                                   command=self._escanear_musicas,
                                   bg="#1a2a1a", fg="#00e676", relief="flat",
                                   padx=12, pady=7,
                                   font=("Segoe UI", 9), cursor="hand2")
        self.btn_scan.pack(side="left", padx=(12, 0))
        self._atualizar_lbl_scan()

        tk.Button(frame_btns, text="🔄 Sincronizar archive",
                  command=self._sincronizar_archive,
                  bg="#2a1a1a", fg="#ff7043", relief="flat",
                  padx=12, pady=7,
                  font=("Segoe UI", 9), cursor="hand2").pack(side="left", padx=(8, 0))

        tk.Button(frame_btns, text="🔍 Verificar integridade",
                  command=self._abrir_verificador,
                  bg="#1a1a2e", fg="#e040fb", relief="flat",
                  padx=12, pady=7,
                  font=("Segoe UI", 9), cursor="hand2").pack(side="left", padx=(8, 0))

        # Progresso — linha da fila de URLs
        self.lbl_fila = tk.Label(self, text="",
                                  bg="#0d0d1a", fg="#7c4dff",
                                  font=("Segoe UI", 8, "bold"), anchor="w")
        self.lbl_fila.pack(fill="x", padx=16, pady=(8, 0))

        # Progresso geral — contadores coloridos
        frame_geral = tk.Frame(self, bg="#0d0d1a")
        frame_geral.pack(fill="x", padx=16, pady=(6, 0))

        # verde — novos baixados nesta sessão
        self.lbl_cnt_novos = tk.Label(frame_geral, text="",
                                      bg="#0d0d1a", fg="#00e676",
                                      font=("Segoe UI", 9, "bold"))
        self.lbl_cnt_novos.pack(side="left")

        tk.Label(frame_geral, text="  |  ", bg="#0d0d1a", fg="#333355",
                 font=("Segoe UI", 9)).pack(side="left")

        # azul — já baixados (archive)
        self.lbl_cnt_arch = tk.Label(frame_geral, text="",
                                     bg="#0d0d1a", fg="#40c4ff",
                                     font=("Segoe UI", 9, "bold"))
        self.lbl_cnt_arch.pack(side="left")

        tk.Label(frame_geral, text="  |  ", bg="#0d0d1a", fg="#333355",
                 font=("Segoe UI", 9)).pack(side="left")

        # cinza — total e restantes
        self.lbl_cnt_total = tk.Label(frame_geral, text="",
                                      bg="#0d0d1a", fg="#7a7a9d",
                                      font=("Segoe UI", 9))
        self.lbl_cnt_total.pack(side="left")

        self.lbl_pct_total = tk.Label(frame_geral, text="",
                                      bg="#0d0d1a", fg="#555577",
                                      font=("Segoe UI", 8), anchor="e")
        self.lbl_pct_total.pack(side="right")

        self.progress_total = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress_total.pack(fill="x", padx=16, pady=(3, 0))

        # Progresso do arquivo atual
        self.lbl_status = tk.Label(self, text="Aguardando URLs...",
                                    bg="#0d0d1a", fg="#7a7a9d",
                                    font=("Segoe UI", 9), anchor="w")
        self.lbl_status.pack(fill="x", padx=16, pady=(6, 0))
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=16, pady=(2, 2))

        # Log — cabeçalho com botão copiar
        frame_log_hdr = tk.Frame(self, bg="#0d0d1a")
        frame_log_hdr.pack(fill="x", padx=16, pady=(6, 0))
        tk.Label(frame_log_hdr, text="Log", bg="#0d0d1a", fg="#555577",
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Button(frame_log_hdr, text="📋 Copiar log",
                  command=self._copiar_log,
                  bg="#1a1a2e", fg="#aaaacc", relief="flat",
                  font=("Segoe UI", 8), cursor="hand2",
                  padx=8, pady=1).pack(side="right")

        self.txt_log = tk.Text(self, bg="#0d0d1a", fg="#e8e8ff", relief="flat",
                                font=("Consolas", 8), state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=16, pady=(2, 14))
        self.txt_log.tag_configure("ok",    foreground="#00e676")
        self.txt_log.tag_configure("erro",  foreground="#ff1744")
        self.txt_log.tag_configure("info",  foreground="#7a7a9d")
        self.txt_log.tag_configure("canal", foreground="#7c4dff")

    def _checar_ffmpeg(self, _=None):
        idx = self._cb_qualidade.current()
        if idx < 0:
            return
        requer = self.QUALIDADES[idx][3]
        if requer and not _FFMPEG_OK:
            messagebox.showwarning(
                "ffmpeg necessário",
                "Esta opção requer ffmpeg.\n\nInstale com:\n  sudo apt install ffmpeg\n\n"
                "Use 'Áudio webm' ou 'Vídeo sem ffmpeg' por enquanto.",
                parent=self)
            self._cb_qualidade.current(0)
            self.var_qualidade.set(self.QUALIDADES[0][0])

    def _adicionar_url(self, url=""):
        var = tk.StringVar(value=url)
        self._url_vars.append(var)
        idx = len(self._url_vars) - 1

        row = tk.Frame(self._frame_lista_urls, bg="#16213e")
        row.pack(fill="x", pady=2)

        num = tk.Label(row, text=f"{idx + 1}.", width=3,
                       bg="#16213e", fg="#555577",
                       font=("Segoe UI", 9))
        num.pack(side="left")

        entry = tk.Entry(row, textvariable=var,
                         bg="#0d0d1a", fg="#e8e8ff",
                         insertbackground="white", relief="flat",
                         font=("Consolas", 9))
        entry.pack(side="left", fill="x", expand=True, ipady=4)

        def _remover(r=row, v=var):
            if len(self._url_vars) <= 1:
                v.set("")
                return
            self._url_vars.remove(v)
            r.destroy()
            # renumera os labels visíveis
            for i, child in enumerate(self._frame_lista_urls.winfo_children()):
                lbl = child.winfo_children()[0]
                lbl.config(text=f"{i + 1}.")

        tk.Button(row, text="−", command=_remover,
                  bg="#37474f", fg="#ff5252", relief="flat",
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  width=2, pady=2).pack(side="left", padx=(6, 0))

        var.trace_add("write", lambda *_: self.after(300, self._atualizar_btn_mapear))
        entry.focus_set()

    def _atualizar_btn_mapear(self):
        urls = [v.get().strip() for v in self._url_vars
                if v.get().strip().lower().startswith("http")]
        canais = [u for u in urls if self._RE_COLECAO.search(u)]
        if not canais:
            self.btn_mapear.config(state="normal")
            self.lbl_mapa_status.config(text="")
            return
        # Verifica se todos os canais já estão no DB
        todos_mapeados = True
        infos = []
        for url in canais:
            c = db_canal_carregar(url)
            if c is None:
                todos_mapeados = False
                break
            data = c.get("atualizado", "")[:10]
            infos.append(f"{c['nome']} ({c['total']} vídeos · {data})")
        if todos_mapeados:
            self.btn_mapear.config(state="disabled")
            self.lbl_mapa_status.config(text="✅ " + " · ".join(infos))
        else:
            self.btn_mapear.config(state="normal")
            self.lbl_mapa_status.config(text="")

    def _toggle_cookies(self):
        if self.var_usar_cookies.get():
            self._cb_navegador.config(state="readonly")
        else:
            self._cb_navegador.config(state="disabled")

    def _escolher_pasta(self):
        pasta = filedialog.askdirectory(parent=self, title="Pasta de destino para downloads")
        if pasta:
            self.var_pasta.set(pasta)
            set_pasta_musicas(pasta)

    def _log(self, msg, tag="info"):
        def _inner():
            self.txt_log.config(state="normal")
            self.txt_log.insert("end", msg + "\n", tag)
            self.txt_log.see("end")
            self.txt_log.config(state="disabled")
        self.after(0, _inner)

    def _copiar_log(self):
        texto = self.txt_log.get("1.0", "end").strip()
        if texto:
            self.clipboard_clear()
            self.clipboard_append(texto)

    def _iniciar(self):
        urls = [v.get().strip() for v in self._url_vars
                if v.get().strip().lower().startswith("http")]
        if not urls:
            messagebox.showwarning("Atenção", "Nenhuma URL válida encontrada.\nVerifique se os links começam com http.", parent=self)
            return
        pasta = self.var_pasta.get().strip()
        os.makedirs(pasta, exist_ok=True)
        if not os.path.isdir(pasta):
            messagebox.showwarning("Atenção", f"Pasta inválida:\n{pasta}", parent=self)
            return

        idx_q = self._cb_qualidade.current()
        if idx_q < 0:
            idx_q = 0
        if self.QUALIDADES[idx_q][3] and not _FFMPEG_OK:
            self._checar_ffmpeg()
            return
        _, fmt, ppost, _ = self.QUALIDADES[idx_q]

        self._cancelar = False
        self.btn_baixar.config(state="disabled")
        self.btn_parar.config(state="normal")
        self.progress["value"] = 0
        self._log(f"▶ Iniciando download — {self.QUALIDADES[idx_q][0]}", "canal")

        usar_cookies = self.var_usar_cookies.get()
        navegador    = self.var_navegador.get() if usar_cookies else None
        incremental  = self.var_incremental.get()
        threading.Thread(target=self._thread, args=(urls, pasta, fmt, ppost, navegador, incremental), daemon=True).start()

    def _parar(self):
        self._cancelar = True
        if self._ydl_ativo is not None:
            self._ydl_ativo.cancelar = True
        self.after(0, lambda: self.lbl_status.config(text="⏹ Cancelando..."))

    def _make_logger(self):
        win = self
        class _L:
            def debug(self, msg):
                if "[debug]" in msg:
                    return
                msg = _strip_ansi(msg)
                # skip messages já são capturadas em to_screen com prefixo ⏭
                if "already been recorded" in msg or "already been downloaded" in msg:
                    return
                win._log(msg, "info")
            def info(self, msg):
                msg = _strip_ansi(msg)
                if "already been recorded" in msg or "already been downloaded" in msg:
                    return
                win._log(msg, "info")
            def warning(self, msg):
                win._log(f"⚠️ {_strip_ansi(msg)}", "info")
            def error(self, msg):
                win._log(f"❌ {_strip_ansi(msg)}", "erro")
        return _L()

    # Padrões de URL que indicam canal/playlist (sem chamada de rede)
    _RE_COLECAO = re.compile(
        r'youtube\.com/(@[^/?]+/?$|channel/|c/|user/|playlist\?)'
        r'|youtu\.be/.*[?&]list='
        r'|soundcloud\.com/[^/]+/?$',
        re.IGNORECASE
    )

    def _renomear_arquivo_baixado(self, filepath, pasta_base, organizar_artista=False):
        """Identifica artista/música, renomeia e opcionalmente move para subpasta do artista."""
        try:
            ext      = os.path.splitext(filepath)[1].lower()
            relativo = os.path.relpath(filepath, pasta_base)
            id_hex   = gerar_id(relativo)

            entrada = db_buscar(id_hex)
            if entrada:
                artista = entrada["artista"]
                musica  = entrada["musica"]
                self._log(f"  📋 DB cache: {artista} — {musica}", "info")
            else:
                nome_limpo = preparar_para_ia(filepath)
                res = buscar_itunes(nome_limpo)
                if res:
                    self._log(f"  🍎 iTunes OK", "info")
                elif self.parent.gerenciador:
                    res = self.parent.gerenciador.interpretar(filepath, nome_limpo=nome_limpo)
                    if res:
                        self._log(f"  🤖 IA OK", "info")
                else:
                    res = None
                if not res:
                    self._log(f"  ⚠️ Não identificado — mantendo nome original", "info")
                    return
                artista = normalizar_case(limpar_ruido(res.get("artista", "").strip()))
                musica  = normalizar_case(limpar_ruido(res.get("musica",  "").strip()))

            if not artista or not musica:
                self._log(f"  ⚠️ Campos vazios — mantendo nome original", "info")
                return

            nome_original = os.path.splitext(os.path.basename(filepath))[0]
            novo_nome     = formatar_nome(artista, musica, ext, relativo)

            if organizar_artista:
                pasta_dest = os.path.join(os.path.dirname(filepath), limpar_nome(artista))
                os.makedirs(pasta_dest, exist_ok=True)
            else:
                pasta_dest = os.path.dirname(filepath)

            new_path = os.path.join(pasta_dest, novo_nome)

            if os.path.abspath(filepath) == os.path.abspath(new_path):
                self._log(f"  ✅ Já no padrão", "ok")
                return

            os.rename(filepath, new_path)
            db_registrar(id_hex, nome_original, artista, musica, novo_nome)
            self._log(f"  ✏️ {artista}/{novo_nome}" if organizar_artista else f"  ✏️ {novo_nome}", "ok")
        except Exception as e:
            self._log(f"  ❌ Erro ao renomear: {e}", "erro")

    def _reorganizar_retroativo(self):
        """Percorre cada subpasta (canal) dentro do destino e move arquivos para subpastas de artista."""
        pasta_principal = self.var_pasta.get().strip()
        if not pasta_principal or not os.path.isdir(pasta_principal):
            messagebox.showwarning("Atenção", "Selecione uma pasta de destino válida primeiro.", parent=self)
            return

        if not messagebox.askyesno(
            "Reorganizar por artista",
            f"Vai reorganizar:\n{pasta_principal}\n\n"
            "Cada arquivo será movido para uma subpasta com o nome do artista "
            "dentro da pasta do canal.\n\nContinuar?",
            parent=self
        ):
            return

        import queue as _queue
        q = _queue.Queue()   # thread → UI: (tipo, dados)
        # tipos: "total" (int), "prog" (v, t, canal, arq), "log" (msg, tag), "fim" ()

        def _poll():
            try:
                while True:
                    item = q.get_nowait()
                    tipo = item[0]
                    if tipo == "total":
                        self.progress_total.config(maximum=max(item[1], 1), value=0)
                        self.lbl_fila.config(text=f"🗂 0 / {item[1]} arquivos…")
                    elif tipo == "prog":
                        _, v, t, canal, arq = item
                        self.progress_total.config(value=v)
                        self.lbl_fila.config(text=f"🗂 {v}/{t}  {canal} — {arq[:55]}")
                    elif tipo == "log":
                        _, msg, tag = item
                        self.txt_log.config(state="normal")
                        self.txt_log.insert("end", msg + "\n", tag)
                        self.txt_log.see("end")
                        self.txt_log.config(state="disabled")
                    elif tipo == "fim":
                        self.progress_total.config(value=0)
                        self.lbl_fila.config(text="")
                        return
            except _queue.Empty:
                pass
            self.after(120, _poll)

        def _run():
            def log(msg, tag="info"):
                q.put(("log", msg, tag))

            log(f"🗂 Reorganizando: {pasta_principal}", "canal")

            try:
                canais = sorted(
                    d for d in os.listdir(pasta_principal)
                    if os.path.isdir(os.path.join(pasta_principal, d))
                    and not d.startswith('.')
                )
            except Exception as e:
                log(f"❌ Não foi possível listar a pasta: {e}", "erro")
                q.put(("fim",))
                return

            todos_arquivos = []
            for canal in canais:
                pasta_canal = os.path.join(pasta_principal, canal)
                try:
                    for f in os.listdir(pasta_canal):
                        if (os.path.isfile(os.path.join(pasta_canal, f))
                                and os.path.splitext(f)[1].lower() in EXTS):
                            todos_arquivos.append((canal, pasta_canal, f))
                except Exception:
                    pass

            total_arqs    = len(todos_arquivos)
            total_movidos = 0
            sem_artista   = 0
            erros         = []
            q.put(("total", total_arqs))

            canal_atual      = None
            movidos_canal    = 0
            proc_canal       = 0   # arquivos processados no canal atual
            _t_prog          = [0.0]

            for i, (canal, pasta_canal, f) in enumerate(todos_arquivos):
                if canal != canal_atual:
                    if canal_atual is not None:
                        resumo = f"  ✅ {movidos_canal} movido(s) de {proc_canal} arquivo(s)"
                        log(resumo, "ok")
                    canal_atual   = canal
                    movidos_canal = 0
                    proc_canal    = 0
                    # Conta quantos arquivos tem neste canal
                    n_canal = sum(1 for x in todos_arquivos if x[0] == canal)
                    log(f"📁 {canal}  ({n_canal} arquivos)", "info")

                proc_canal += 1
                idx = i + 1

                # Progresso na barra (throttle 150ms)
                agora = time.time()
                if agora - _t_prog[0] >= 0.15:
                    _t_prog[0] = agora
                    q.put(("prog", idx, total_arqs, canal, f))

                # Log a cada 100 arquivos dentro do canal para mostrar que está vivo
                if proc_canal % 100 == 0:
                    log(f"  ↳ {proc_canal} arquivos processados, {movidos_canal} movidos…", "info")

                fpath   = os.path.join(pasta_canal, f)
                nome_s  = os.path.splitext(f)[0]
                artista = _artista_do_titulo(nome_s)

                if not artista:
                    sem_artista += 1
                    continue

                pasta_art = os.path.join(pasta_canal, limpar_nome(artista))
                try:
                    os.makedirs(pasta_art, exist_ok=True)
                except OSError:
                    # Fallback: remove qualquer caractere não-ASCII restante
                    artista_safe = re.sub(r'[^\w\s\-]', '', artista).strip()
                    pasta_art = os.path.join(pasta_canal, limpar_nome(artista_safe) or "Outros")
                    os.makedirs(pasta_art, exist_ok=True)

                destino = os.path.join(pasta_art, f)

                if os.path.exists(destino):
                    continue

                try:
                    os.rename(fpath, destino)
                    movidos_canal += 1
                    total_movidos += 1
                except Exception as e:
                    erros.append(f"{canal}/{f}: {e}")

            if canal_atual is not None and movidos_canal:
                log(f"  ✅ {movidos_canal} movido(s)", "ok")
            if sem_artista:
                log(f"⚠️ {sem_artista} arquivo(s) sem padrão 'Artista - Música' (não movidos)", "info")
            for err in erros[:10]:
                log(f"❌ {err}", "erro")
            log(f"🎉 Concluído — {total_movidos} movido(s) de {total_arqs} total", "canal")
            q.put(("fim",))

        self.after(120, _poll)
        threading.Thread(target=_run, daemon=True).start()

    def _sincronizar_archive(self):
        """Remove do archive IDs cujos arquivos não existem mais em nenhuma pasta configurada."""
        if not messagebox.askyesno(
            "Sincronizar archive",
            "Vai escanear todas as pastas configuradas e remover do archive\n"
            "os vídeos cujos arquivos foram deletados do disco.\n\n"
            "Isso permite re-baixar canais que você apagou.\n\nContinuar?",
            parent=self
        ):
            return

        import queue as _queue
        q = _queue.Queue()

        def _poll():
            try:
                while True:
                    item = q.get_nowait()
                    if item[0] == "log":
                        _, msg, tag = item
                        self.txt_log.config(state="normal")
                        self.txt_log.insert("end", msg + "\n", tag)
                        self.txt_log.see("end")
                        self.txt_log.config(state="disabled")
                    elif item[0] == "prog":
                        self.lbl_fila.config(text=item[1])
                    elif item[0] == "fim":
                        self.lbl_fila.config(text="")
                        return
            except _queue.Empty:
                pass
            self.after(120, _poll)

        def _run():
            def log(msg, tag="info"):
                q.put(("log", msg, tag))

            # 1. Coletar todas as pastas a verificar
            pastas = set()
            pasta_base = self.var_pasta.get().strip()
            if pasta_base and os.path.isdir(pasta_base):
                pastas.add(pasta_base)
                for d in os.listdir(pasta_base):
                    full = os.path.join(pasta_base, d)
                    if os.path.isdir(full):
                        pastas.add(full)
            for p in db_carregar().get("musicas_escaneadas", {}).keys():
                if os.path.isdir(p):
                    pastas.add(p)

            log(f"🔍 Verificando {len(pastas)} pasta(s) no disco…", "canal")
            q.put(("prog", "🔍 Indexando arquivos…"))

            # 2. Construir conjunto de títulos normalizados existentes
            titulos_disco = set()
            for pasta in pastas:
                titulos_disco |= nomes_pasta_carregar(pasta)
            log(f"  {len(titulos_disco)} arquivo(s) encontrado(s) no disco", "info")

            # 3. Carregar archive
            try:
                with open(YT_ARCHIVE, encoding="utf-8") as fh:
                    linhas_archive = fh.readlines()
            except FileNotFoundError:
                log("⚠️ Archive inexistente — nada a fazer.", "info")
                q.put(("fim",))
                return

            # Mapear id → linha original
            ids_linha = {}
            for ln in linhas_archive:
                partes = ln.strip().split()
                if len(partes) >= 2:
                    ids_linha[partes[-1]] = ln

            total_ids = len(ids_linha)
            log(f"  {total_ids} ID(s) no archive", "info")

            # 4. Mapear video_id → título normalizado (via canal DB)
            db = db_carregar()
            id_para_tnorm = {}
            for canal_data in db.get("canais", {}).values():
                for v in canal_data.get("videos", []):
                    vid = v.get("id")
                    tit = v.get("titulo", "")
                    if vid and tit:
                        id_para_tnorm[vid] = _normalizar_titulo(tit)

            log(f"  {len(id_para_tnorm)} ID(s) com título no DB de canais", "info")
            q.put(("prog", f"🔍 Cruzando {total_ids} IDs com arquivos no disco…"))

            # 5. Verificar quais IDs têm arquivo no disco
            manter  = set()
            remover = set()
            sem_info = 0

            for i, vid_id in enumerate(ids_linha):
                if i % 200 == 0:
                    q.put(("prog", f"🔍 Verificando {i}/{total_ids} IDs…"))

                tnorm = id_para_tnorm.get(vid_id)
                if not tnorm or len(tnorm) < 8:
                    # Título desconhecido → manter por segurança
                    manter.add(vid_id)
                    sem_info += 1
                    continue

                existe = any(
                    tnorm in td or td in tnorm
                    for td in titulos_disco if len(td) >= 8
                )
                if existe:
                    manter.add(vid_id)
                else:
                    remover.add(vid_id)

            log(f"  {len(remover)} ID(s) sem arquivo no disco → serão removidos", "info")
            if sem_info:
                log(f"  {sem_info} ID(s) sem título no DB → mantidos por segurança", "info")

            if not remover:
                log("✅ Archive já está sincronizado.", "canal")
                q.put(("fim",))
                return

            # 6. Reescrever archive removendo os IDs ausentes
            novas = [ln for ln in linhas_archive
                     if ln.strip() and ln.strip().split()[-1] not in remover]
            with open(YT_ARCHIVE, "w", encoding="utf-8") as fh:
                fh.writelines(novas)

            log(f"🗑 {len(remover)} ID(s) removido(s) do archive", "ok")
            log(f"✅ Archive sincronizado — {len(novas)} ID(s) restantes", "canal")
            q.put(("fim",))

        self.after(120, _poll)
        threading.Thread(target=_run, daemon=True).start()

    def _abrir_verificador(self):
        pasta = self.var_pasta.get().strip()
        if not pasta or not os.path.isdir(pasta):
            messagebox.showwarning("Atenção", "Selecione uma pasta válida primeiro.", parent=self)
            return

        def _rebaixar(urls):
            for url in urls:
                self._adicionar_url(url)
            self.lift()

        IntegridadeWindow(self, pasta, _rebaixar)

    def _atualizar_lbl_scan(self):
        pastas = db_musicas_pastas()
        if pastas:
            total = sum(d["total"] for d in pastas.values())
            txt = f"({len(pastas)} pasta{'s' if len(pastas)>1 else ''} · {total} músicas indexadas)"
            self.btn_scan.config(text=f"🎵 Escanear músicas  {txt}")
        else:
            self.btn_scan.config(text="🎵 Escanear músicas")

    def _escanear_musicas(self):
        pasta = filedialog.askdirectory(title="Selecione a pasta de músicas para indexar", parent=self)
        if not pasta:
            return
        self.btn_scan.config(state="disabled")
        self._log(f"🎵 Escaneando: {pasta}", "canal")

        def _run():
            titulos = nomes_pasta_carregar(pasta)
            db_musicas_salvar(pasta, titulos)
            self._log(f"✅ {len(titulos)} músicas indexadas — {os.path.basename(pasta)}", "canal")
            self.after(0, lambda: (
                self.btn_scan.config(state="normal"),
                self._atualizar_lbl_scan(),
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _mapear_canal(self, url, cookies_opts):
        """Enumera todos os vídeos de um canal/playlist sem baixar e salva no DB."""
        self._log("🗺 Mapeando canal — aguarde, isso pode levar alguns minutos...", "canal")
        url_mapa = url
        if re.search(r'/@[^/]+$|/channel/[^/]+$|/c/[^/]+$', url):
            url_mapa = url.rstrip('/') + '/videos'

        win = self
        _RE_ITEM_MAPA   = re.compile(r'\[download\] Downloading item (\d+) of (\d+)')
        _RE_DEBUG_PREFIX = re.compile(r'^\[debug\]\s*')

        class _LogMapa:
            def debug(self, msg):
                pass  # ignorar verbose do yt-dlp durante mapeamento
            def info(self, msg):
                msg = _strip_ansi(msg)
                m = _RE_ITEM_MAPA.search(msg)
                if m:
                    atual, total = int(m.group(1)), int(m.group(2))
                    win.after(0, lambda a=atual, t=total:
                        win.lbl_fila.config(text=f"🗺 Mapeando: {a} / {t}"))
                    if atual % 500 == 0 or atual == total:
                        win._log(f"🗺 Mapeando: {atual} / {total}", "info")
                elif msg.strip():
                    win._log(msg.strip(), "info")
            def warning(self, msg):
                win._log(f"⚠️ {_strip_ansi(msg)}", "info")
            def error(self, msg):
                win._log(f"❌ {_strip_ansi(msg)}", "erro")

        opts_flat = {
            "extract_flat": True,
            "quiet":        False,
            "no_warnings":  False,
            "logger":       _LogMapa(),
        }
        opts_flat.update(cookies_opts)

        try:
            with yt_dlp.YoutubeDL(opts_flat) as ydl:
                info = ydl.extract_info(url_mapa, download=False)
        except Exception as e:
            self._log(f"❌ Erro ao mapear: {e}", "erro")
            return None

        if not info:
            self._log("❌ Nenhuma informação retornada pelo canal.", "erro")
            return None

        videos = []
        for entry in (info.get("entries") or []):
            if entry and entry.get("id"):
                videos.append({
                    "id":     entry["id"],
                    "titulo": entry.get("title", ""),
                    "url":    f"https://www.youtube.com/watch?v={entry['id']}",
                })

        nome = info.get("channel") or info.get("uploader") or info.get("title") or url
        db_canal_salvar(url, nome, videos)
        self._log(f"✅ Mapa salvo: {len(videos)} vídeos — {nome}", "canal")
        return db_canal_carregar(url)

    def _iniciar_mapeamento(self):
        urls = [v.get().strip() for v in self._url_vars if v.get().strip().lower().startswith("http")]
        if not urls:
            messagebox.showwarning("Atenção", "Nenhuma URL válida.", parent=self)
            return
        usar_cookies = self.var_usar_cookies.get()
        navegador    = self.var_navegador.get() if usar_cookies else None
        cookies_opts = {"cookiesfrombrowser": (navegador,)} if navegador else {}

        self.btn_mapear.config(state="disabled")
        self.btn_baixar.config(state="disabled")

        def _run():
            for url in urls:
                if self._RE_COLECAO.search(url):
                    self._mapear_canal(url, cookies_opts)
                else:
                    self._log(f"ℹ️ {url[:80]} — não é canal/playlist, ignorado.", "info")
            self.after(0, lambda: (
                self.btn_baixar.config(state="normal"),
                self._atualizar_btn_mapear(),
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _thread(self, urls, pasta_base, fmt, ppost, navegador=None, incremental=False):
        total_urls    = len(urls)
        baixados      = 0
        erros         = 0
        pulados       = 0
        _canal_logado = set()
        logger        = self._make_logger()

        for idx, url in enumerate(urls):
            if self._cancelar:
                break

            self.after(0, lambda n=idx, t=total_urls, u=url:
                       self.lbl_fila.config(text=f"[{n+1}/{t}] {u[:90]}"))

            eh_colecao = bool(self._RE_COLECAO.search(url))
            outtmpl    = os.path.join(pasta_base, "%(title)s.%(ext)s")

            _feitos        = [0]
            _total_canal   = [0]
            _renomeados    = set()
            _pulados       = [0]
            fazer_rename        = self.var_renomear.get()
            organizar_artista   = self.var_organizar_artista.get()

            def _atualizar_geral(_fi, _tot):
                feitos  = _fi[0]
                total   = _tot[0]
                pulados = _pulados[0]
                if total:
                    processados = feitos + pulados
                    faltam = max(0, total - processados)
                    pct    = int(processados / total * 100)
                    self.after(0, lambda f=feitos, t=total, r=faltam, p=pct, pu=pulados: (
                        self.lbl_cnt_novos.config(text=f"⬇ {f} novos"),
                        self.lbl_cnt_arch.config(text=f"💾 {pu} já baixados"),
                        self.lbl_cnt_total.config(text=f"{r} restantes  /  {t}"),
                        self.lbl_pct_total.config(text=f"{p}%"),
                        self.progress_total.configure(value=p),
                    ))
                else:
                    self.after(0, lambda f=feitos, pu=pulados: (
                        self.lbl_cnt_novos.config(text=f"⬇ {f} novos" if f else ""),
                        self.lbl_cnt_arch.config(text=f"💾 {pu} já baixados" if pu else ""),
                        self.lbl_cnt_total.config(text=""),
                    ))

            def _on_playlist_progress(pl_idx, pl_total, _tot=_total_canal):
                """Chamado por _YDLCancelavel.process_video_result antes de cada vídeo."""
                # Quando baixando do mapa, pl_total é o nº de pendentes — não sobrescreve
                # o total do canal (6021) que já foi definido a partir do DB.
                if not usando_mapa and pl_total != _tot[0]:
                    _tot[0] = pl_total
                _atualizar_geral(_feitos, _total_canal)
                self.after(0, lambda i=pl_idx, t=pl_total, u=idx+1, tu=total_urls: (
                    self.lbl_fila.config(
                        text=f"[URL {u}/{tu}]  vídeo {i} de {t}"),
                ))

            def hook(d, _logado=_canal_logado, _fi=_feitos,
                     _tot=_total_canal, _ren=_renomeados):
                if self._cancelar:
                    raise _CancelarDownload()
                if d["status"] == "downloading":
                    nome  = os.path.basename(d.get("filename", ""))
                    pct_s = d.get("_percent_str", "").strip()
                    spd   = d.get("_speed_str",   "").strip()
                    eta   = d.get("_eta_str",      "").strip()
                    canal = d.get("uploader") or d.get("channel") or ""
                    try:
                        val = float(pct_s.replace("%", ""))
                    except (ValueError, AttributeError):
                        val = 0
                    if canal and canal not in _logado:
                        _logado.add(canal)
                        self._log(f"📁 Canal: {canal}", "canal")
                    self.after(0, lambda nm=nome, p=pct_s, s=spd, e=eta, v=val: (
                        self.lbl_status.config(text=f"↓ {nm}   {p}   {s}   ETA {e}"),
                        self.progress.configure(value=v),
                    ))
                elif d["status"] == "finished":
                    # Só atualiza a barra do arquivo atual; o rename e contagem ficam no pp_hook
                    self.after(0, lambda: self.progress.configure(value=100))

            def pp_hook(d, _ren=_renomeados, _fi=_feitos, _tot=_total_canal):
                # yt-dlp passa info_copy (snapshot pré-run) para o hook.
                # FFmpegMergerPP finished → arquivo mesclado foi criado (path existe).
                # MoveFilesAfterDownload started → arquivo ainda no local original (antes do move).
                # MoveFilesAfterDownload finished → arquivo já foi movido, path antigo não existe.
                pp = d.get("postprocessor", "")
                st = d.get("status", "")
                if not (pp in ("Merger", "MoveFiles") and st == "finished"):
                    return
                info = d.get("info_dict", {})
                fn = None
                for key in ("filepath", "_filename"):
                    v = info.get(key, "")
                    if v and os.path.isfile(v):
                        fn = v
                        break
                if not fn or fn in _ren:
                    return
                ext_fn = os.path.splitext(fn)[1].lower()
                if ext_fn not in EXTS:
                    return
                _ren.add(fn)
                _fi[0] += 1
                self._log(f"⬇ {os.path.basename(fn)}", "ok")
                _atualizar_geral(_fi, _tot)
                # Rename/organização roda em thread separada para não bloquear o download
                if fazer_rename:
                    threading.Thread(
                        target=self._renomear_arquivo_baixado,
                        args=(fn, pasta_base, organizar_artista),
                        daemon=True
                    ).start()
                elif organizar_artista:
                    titulo_yt  = d.get("info_dict", {}).get("title", "")
                    artista_yt = _artista_do_titulo(titulo_yt)
                    if artista_yt:
                        def _mover_artista(f=fn, a=artista_yt):
                            pasta_art = os.path.join(os.path.dirname(f), limpar_nome(a))
                            os.makedirs(pasta_art, exist_ok=True)
                            destino = os.path.join(pasta_art, os.path.basename(f))
                            try:
                                os.rename(f, destino)
                                self._log(f"  📂 {a}/", "info")
                            except Exception as e:
                                self._log(f"  ⚠️ Não foi possível mover: {e}", "info")
                        threading.Thread(target=_mover_artista, daemon=True).start()

            def _on_skip(msg, _p=_pulados):
                _p[0] += 1
                self._log(f"⏭ {msg}", "info")

            opts = {
                "format":                  fmt,
                "postprocessors":          ppost,
                "outtmpl":                 outtmpl,
                "quiet":                   True,
                "no_warnings":             False,
                "progress_hooks":          [hook],
                "postprocessor_hooks":     [pp_hook],
                "download_archive":        YT_ARCHIVE,
                "nooverwrites":            True,
                "ignoreerrors":            True,
                "logger":                  logger,
                "extractor_args": {
                    "youtubetab": {"skip": ["authcheck"]},
                },
                "js_runtimes":        {"node": {}},  # Node.js v22 instalado
                "sleep_interval":     2,
                "max_sleep_interval": 5,
            }
            cookies_opts = {}
            if navegador:
                cookies_opts["cookiesfrombrowser"] = (navegador,)

            if incremental:
                opts["break_on_existing"] = True
                self._log("⚡ Modo incremental — para ao encontrar vídeo já baixado", "canal")

            # Para canais: verifica mapa no DB para pular vídeos já baixados
            urls_para_dl = [url]
            usando_mapa  = False
            if eh_colecao and not incremental:
                canal_db = db_canal_carregar(url)
                if canal_db is None:
                    if navegador:
                        self._log(f"🍪 Usando cookies do {navegador} para mapear", "canal")
                    canal_db = self._mapear_canal(url, cookies_opts)
                if canal_db is not None:
                    arquivo_ids  = archive_ids_carregar()
                    pendentes    = [v for v in canal_db["videos"] if v["id"] not in arquivo_ids]

                    # Cruza com arquivos existentes na pasta + coleção escaneada no DB
                    nomes_existentes = nomes_pasta_carregar(pasta_base) | db_musicas_titulos()
                    if nomes_existentes:
                        achou_na_pasta = []
                        ainda_pendentes = []
                        for v in pendentes:
                            tn = _normalizar_titulo(v["titulo"])
                            if len(tn) > 12 and any(
                                tn in ne or ne in tn
                                for ne in nomes_existentes if len(ne) > 12
                            ):
                                achou_na_pasta.append(v["id"])
                            else:
                                ainda_pendentes.append(v)
                        if achou_na_pasta:
                            archive_adicionar(achou_na_pasta)
                            self._log(
                                f"📁 {len(achou_na_pasta)} vídeos encontrados na pasta → adicionados ao archive",
                                "canal"
                            )
                        pendentes = ainda_pendentes

                    ja_dl        = canal_db["total"] - len(pendentes)
                    _total_canal[0] = canal_db["total"]
                    _pulados[0]     = ja_dl
                    _atualizar_geral(_feitos, _total_canal)
                    self._log(
                        f"📋 {canal_db['total']} no mapa · {ja_dl} já baixados · {len(pendentes)} pendentes",
                        "canal"
                    )
                    if not pendentes:
                        self._log("✅ Todos os vídeos já foram baixados.", "canal")
                        continue
                    urls_para_dl = [v["url"] for v in pendentes]
                    usando_mapa  = True

            if navegador:
                opts["cookiesfrombrowser"] = (navegador,)
                self._log(f"🍪 Usando cookies do {navegador}", "canal")

            try:
                with _YDLCancelavel(opts) as ydl:
                    self._ydl_ativo          = ydl
                    ydl.on_playlist_progress = _on_playlist_progress
                    ydl.on_skip              = _on_skip
                    ret = ydl.download(urls_para_dl)
                n_feitos = _feitos[0]
                if n_feitos > 0:
                    baixados += n_feitos
                elif ret == 0:
                    self._log(f"ℹ️ Todos os vídeos já foram baixados anteriormente.", "info")
                else:
                    erros += 1
                    self._log(f"⚠️ Sem arquivo baixado: {url}", "erro")
            except yt_dlp.utils.DownloadCancelled:
                self._log("⏹ Download interrompido pelo usuário.", "info")
                break
            except Exception as e:
                erros += 1
                self._log(f"❌ {url}\n   {e}", "erro")
            finally:
                self._ydl_ativo = None
                pulados += _pulados[0]

        cancelado = self._cancelar
        msg = f"{'Cancelado' if cancelado else 'Concluído'} — {baixados} baixado(s)"
        if pulados:
            msg += f", {pulados} já existia(m)"
        if erros:
            msg += f", {erros} erro(s)"

        self.after(0, lambda: self.lbl_fila.config(text=""))
        self.after(0, lambda: self.lbl_status.config(text=msg))
        self.after(0, lambda: (
            self.lbl_cnt_novos.config(text=""),
            self.lbl_cnt_arch.config(text=""),
            self.lbl_cnt_total.config(text=""),
            self.lbl_pct_total.config(text=""),
        ))
        self.after(0, lambda: self.lbl_pct_total.config(text=""))
        self.after(0, lambda: self.btn_baixar.config(state="normal"))
        self.after(0, lambda: self.btn_parar.config(state="disabled"))
        self.after(0, lambda: self.progress.configure(value=0))
        self.after(0, lambda: self.progress_total.configure(value=0))

        _notificar("Voxly Download", msg)

        # Só atualiza pasta no app; análise automática é desnecessária pois rename já ocorreu
        if baixados > 0 and not cancelado:
            if self.parent.pasta != pasta_base:
                self.parent.pasta = pasta_base
                self.after(0, lambda p=pasta_base: self.parent.lbl_pasta.config(
                    text=f"  {p}", fg="#e8e8ff"))


# ── App ─────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Normalizador de Músicas — Voxly")
        try:
            self.attributes("-zoomed", True)
        except Exception:
            self.state("zoomed")
        self.configure(bg="#0d0d1a")

        self.pasta          = None
        self.arquivos       = []
        self.resultados     = []
        self.pausado        = False
        self._cancelar      = False
        self._auto_renomear = False
        self.recursivo      = tk.BooleanVar(value=False)
        self.entradas    = carregar_config()
        self.gerenciador = GerenciadorFallback(self.entradas) if self.entradas else None

        self._build_ui()

        if not self.entradas:
            self.after(500, lambda: messagebox.showinfo(
                "Bem-vindo",
                "Configure ao menos uma API Key clicando em 🔑 API Key."
            ))

    # ── UI ──────────────────────────────────────────────────

    def _build_ui(self):
        # Barra superior global
        hdr = tk.Frame(self, bg="#16213e", pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🎵 Voxly",
                 font=("Segoe UI", 13, "bold"), bg="#16213e", fg="#e040fb").pack(side="left", padx=16)
        self.lbl_slot = tk.Label(hdr, text="Sem API configurada",
                                  bg="#16213e", fg="#555577", font=("Segoe UI", 8))
        self.lbl_slot.pack(side="left", padx=8)
        self.btn_pausa = tk.Button(hdr, text="⏸ Pausar", command=self._toggle_pausa,
                                    bg="#37474f", fg="white", font=("Segoe UI", 9, "bold"),
                                    relief="flat", padx=10, pady=4, cursor="hand2")
        self.btn_pausa.pack(side="right", padx=4)
        tk.Button(hdr, text="📋 Log", command=self._ver_log,
                  bg="#1a1a2e", fg="#e8e8ff", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(side="right", padx=4)
        tk.Button(hdr, text="🔑 API Key", command=self._configurar_apikey,
                  bg="#ff6d00", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(side="right", padx=8)

        # Notebook — tabs principais
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook",        background="#0d0d1a", borderwidth=0)
        style.configure("TNotebook.Tab",    background="#16213e", foreground="#aaaacc",
                        font=("Segoe UI", 10, "bold"), padding=[16, 6])
        style.map("TNotebook.Tab",
                  background=[("selected", "#0d0d1a")],
                  foreground=[("selected", "#e040fb")])
        style.configure("Treeview",        background="#1a1a2e", foreground="#e8e8ff",
                        fieldbackground="#1a1a2e", font=("Segoe UI", 9), rowheight=28)
        style.configure("Treeview.Heading", background="#16213e", foreground="#7a7a9d",
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#2a2a4e")])

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # ── Tab 1: Download ──────────────────────────────────
        self.tab_dl = DownloadWindow(nb, self)
        nb.add(self.tab_dl, text="  ⬇ Download  ")

        # ── Tab 2: Renomear ──────────────────────────────────
        tab_ren = tk.Frame(nb, bg="#0d0d1a")
        nb.add(tab_ren, text="  ✏️ Renomear  ")

        # Barra pasta (dentro do tab Renomear)
        ctrl = tk.Frame(tab_ren, bg="#1a1a2e", pady=8)
        ctrl.pack(fill="x")
        self.lbl_pasta = tk.Label(ctrl, text="  Nenhuma pasta selecionada",
                                   bg="#1a1a2e", fg="#7a7a9d", font=("Segoe UI", 9), anchor="w")
        self.lbl_pasta.pack(side="left", fill="x", expand=True, padx=8)
        tk.Checkbutton(ctrl, text="Incluir subpastas", variable=self.recursivo,
                       bg="#1a1a2e", fg="#7a7a9d", selectcolor="#0d0d1a",
                       activebackground="#1a1a2e", activeforeground="#e8e8ff",
                       font=("Segoe UI", 9), cursor="hand2",
                       command=self._atualizar_contagem_pasta).pack(side="right", padx=8)
        for txt, cmd, cor in [
            ("📁 Selecionar Pasta",  self.selecionar_pasta, "#7c4dff"),
            ("🔍 Analisar Arquivos", self.analisar,         "#e040fb"),
        ]:
            tk.Button(ctrl, text=txt, command=cmd, bg=cor, fg="white",
                      font=("Segoe UI", 9, "bold"), relief="flat",
                      padx=12, pady=5, cursor="hand2").pack(side="right", padx=4)

        # Status + barra de progresso
        self.lbl_status = tk.Label(tab_ren, text="Selecione uma pasta para começar.",
                                    bg="#0d0d1a", fg="#7a7a9d", font=("Segoe UI", 9), anchor="w")
        self.lbl_status.pack(fill="x", padx=16, pady=(8, 2))
        self.progress = ttk.Progressbar(tab_ren, mode="determinate")
        self.progress.pack(fill="x", padx=16, pady=(0, 6))

        # Tabela
        frame_t = tk.Frame(tab_ren, bg="#0d0d1a")
        frame_t.pack(fill="both", expand=True, padx=16)

        cols = ("sel", "original", "artista", "musica", "novo_nome", "status")
        self.tree = ttk.Treeview(frame_t, columns=cols, show="headings")
        self.tree.heading("sel",       text="✓",        anchor="center")
        self.tree.heading("original",  text="Nome Original")
        self.tree.heading("artista",   text="Artista")
        self.tree.heading("musica",    text="Música")
        self.tree.heading("novo_nome", text="Novo Nome")
        self.tree.heading("status",    text="Status",   anchor="center")
        self.tree.column("sel",       width=36,  stretch=False, anchor="center")
        self.tree.column("original",  width=220)
        self.tree.column("artista",   width=160)
        self.tree.column("musica",    width=180)
        self.tree.column("novo_nome", width=260)
        self.tree.column("status",    width=100, stretch=False, anchor="center")

        self.tree.tag_configure("ok",      foreground="#00e676")
        self.tree.tag_configure("erro",    foreground="#ff1744")
        self.tree.tag_configure("igual",   foreground="#555577")
        self.tree.tag_configure("editado", foreground="#ff9100")

        vsb = ttk.Scrollbar(frame_t, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(frame_t, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.bind("<ButtonRelease-1>", self._toggle_sel)
        self.tree.bind("<Double-1>",        self._editar)

        # Rodapé
        rod = tk.Frame(tab_ren, bg="#16213e", pady=10)
        rod.pack(fill="x", side="bottom")
        self.lbl_contagem = tk.Label(rod, text="0 selecionado(s)",
                                      bg="#16213e", fg="#7a7a9d", font=("Segoe UI", 9))
        self.lbl_contagem.pack(side="left", padx=16)
        for txt, cmd in [("☑ Marcar Todos", self.marcar_todos), ("☐ Desmarcar Todos", self.desmarcar_todos)]:
            tk.Button(rod, text=txt, command=cmd, bg="#1a1a2e", fg="#e8e8ff",
                      relief="flat", padx=10, pady=4, cursor="hand2").pack(side="left", padx=4)
        tk.Button(rod, text="✅ Renomear Selecionados", command=self.renomear,
                  bg="#00e676", fg="#000000", font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=20, pady=6, cursor="hand2").pack(side="right", padx=16)
        self.btn_duplicatas = tk.Button(rod, text="🔁 Buscar Duplicatas",
                  command=self._abrir_duplicatas,
                  bg="#1565c0", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=12, pady=6, cursor="hand2", state="disabled")
        self.btn_duplicatas.pack(side="right", padx=4)
        self.btn_retentar = tk.Button(rod, text="🔄 Retentar Erros",
                  command=self._retentar_erros,
                  bg="#37474f", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=12, pady=6, cursor="hand2", state="disabled")
        self.btn_retentar.pack(side="right", padx=4)

        tk.Button(rod, text="📤 Migrar para Flat",
                  command=self._migrar_para_flat,
                  bg="#e65100", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=12, pady=6, cursor="hand2").pack(side="left", padx=8)

    # ── Ações ───────────────────────────────────────────────

    def _toggle_pausa(self):
        self.pausado = not self.pausado
        if self.pausado:
            self.btn_pausa.config(text="▶ Retomar", bg="#00897b")
            self.lbl_status.config(text="⏸ Pausado. Clique em Retomar para continuar.")
        else:
            self.btn_pausa.config(text="⏸ Pausar", bg="#37474f")

    def _migrar_para_flat(self):
        if not self.pasta or not os.path.isdir(self.pasta):
            messagebox.showwarning("Migrar", "Selecione a pasta de músicas primeiro.")
            return
        resp = messagebox.askyesno(
            "Migrar para estrutura flat",
            f"Isso moverá TODOS os arquivos de subpastas para:\n{self.pasta}\n\n"
            "As subpastas (exceto a que contém o banco de dados) serão excluídas.\n\n"
            "Deseja continuar?"
        )
        if not resp:
            return

        self.lbl_status.config(text="⏳ Migrando arquivos…")
        self.progress["value"] = 0
        self.update_idletasks()

        def _run():
            import shutil
            raiz = self.pasta
            # pasta que contém o banco de dados — não apagar
            db_dir = os.path.dirname(DB_FILE) if os.path.isfile(DB_FILE) else None

            AUDIO_VIDEO_EXT = {
                ".mp4", ".webm", ".mkv", ".avi", ".mov", ".flv",
                ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac"
            }

            # Coleta todos os arquivos em subpastas
            para_mover = []
            for dirpath, dirnames, filenames in os.walk(raiz):
                if os.path.abspath(dirpath) == os.path.abspath(raiz):
                    continue
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in AUDIO_VIDEO_EXT:
                        para_mover.append(os.path.join(dirpath, fname))

            total = len(para_mover)
            self.after(0, lambda: self.progress.configure(maximum=max(total, 1), value=0))
            self.after(0, lambda: self.lbl_status.config(
                text=f"⏳ Movendo {total} arquivo(s)…"))

            movidos = 0
            erros   = []
            for src in para_mover:
                fname   = os.path.basename(src)
                destino = os.path.join(raiz, fname)
                # evita sobrescrever: adiciona sufixo
                if os.path.exists(destino) and os.path.abspath(src) != os.path.abspath(destino):
                    base, ext2 = os.path.splitext(fname)
                    n = 1
                    while os.path.exists(destino):
                        destino = os.path.join(raiz, f"{base} ({n}){ext2}")
                        n += 1
                try:
                    shutil.move(src, destino)
                    movidos += 1
                except Exception as e:
                    erros.append(f"{fname}: {e}")
                self.after(0, lambda v=movidos: self.progress.configure(value=v))

            # Remove subpastas vazias (exceto a do DB)
            removidas = []
            for nome in os.listdir(raiz):
                sub = os.path.join(raiz, nome)
                if not os.path.isdir(sub):
                    continue
                if db_dir and os.path.abspath(sub) == os.path.abspath(db_dir):
                    continue
                try:
                    shutil.rmtree(sub)
                    removidas.append(nome)
                except Exception as e:
                    erros.append(f"rmdir {nome}: {e}")

            def _fim():
                msg = f"✅ {movidos} arquivo(s) movido(s). {len(removidas)} pasta(s) removida(s)."
                if erros:
                    msg += f"\n⚠️ {len(erros)} erro(s):\n" + "\n".join(erros[:10])
                self.lbl_status.config(text=msg)
                self.progress["value"] = 0
                messagebox.showinfo("Migração concluída", msg)
            self.after(0, _fim)

        threading.Thread(target=_run, daemon=True).start()

    def _ver_log(self):
        if not self.gerenciador:
            messagebox.showinfo("Log", "Nenhuma análise realizada ainda.")
            return
        win = tk.Toplevel(self)
        win.title("Log de Fallback")
        win.geometry("700x440")
        win.configure(bg="#0d0d1a")
        txt = tk.Text(win, bg="#0d0d1a", fg="#e8e8ff", font=("Consolas", 9), wrap="word")
        txt.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        conteudo = "\n".join(self.gerenciador.log) or "Sem entradas no log."
        txt.insert("end", conteudo)
        txt.config(state="disabled")
        rod = tk.Frame(win, bg="#0d0d1a")
        rod.pack(fill="x", padx=10, pady=(0, 8))
        def copiar():
            win.clipboard_clear()
            win.clipboard_append(conteudo)
            btn_copiar.config(text="✅ Copiado!")
            win.after(2000, lambda: btn_copiar.config(text="📋 Copiar tudo"))
        btn_copiar = tk.Button(rod, text="📋 Copiar tudo", command=copiar,
                               bg="#1a1a2e", fg="#e8e8ff", relief="flat",
                               padx=10, pady=4, cursor="hand2", font=("Segoe UI", 9))
        btn_copiar.pack(side="right")

    def _configurar_apikey(self):
        win = tk.Toplevel(self)
        win.title("Configurar API Keys")
        win.geometry("700x620")
        win.minsize(700, 600)
        win.configure(bg="#0d0d1a")
        win.grab_set()

        tk.Label(win, text="Gerenciar API Keys",
                 bg="#0d0d1a", fg="#e040fb", font=("Segoe UI", 12, "bold")).pack(pady=(14,2))
        tk.Label(win, text="Fallback automático: tenta todos os modelos de cada key antes de trocar de provedor.",
                 bg="#0d0d1a", fg="#555577", font=("Segoe UI", 8)).pack(pady=(0,8))

        frame_form = tk.LabelFrame(win, text=" Nova entrada ", bg="#16213e", fg="#aaaacc",
                                    font=("Segoe UI", 9, "bold"), pady=10, padx=10)
        frame_form.pack(fill="x", padx=16, pady=(0,8))

        tk.Label(frame_form, text="Provedor:", bg="#16213e", fg="#bbb",
                 font=("Segoe UI", 9)).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        var_prov = tk.StringVar(value="Groq")
        cb_prov  = ttk.Combobox(frame_form, textvariable=var_prov, width=16,
                                 values=list(PROVEDORES.keys()), state="readonly", font=("Segoe UI", 9))
        cb_prov.grid(row=0, column=1, padx=4, pady=6, sticky="w")

        tk.Label(frame_form, text="API Key:", bg="#16213e", fg="#bbb",
                 font=("Segoe UI", 9)).grid(row=1, column=0, padx=8, pady=6, sticky="w")
        var_chave = tk.StringVar()
        tk.Entry(frame_form, textvariable=var_chave, width=50, bg="#0d0d1a", fg="#e8e8ff",
                 insertbackground="white", relief="flat", font=("Segoe UI", 9), show="*"
                 ).grid(row=1, column=1, padx=4, pady=6, sticky="ew")

        tk.Label(frame_form, text="Modelos\n(um por linha):", bg="#16213e", fg="#aaaacc",
                 font=("Segoe UI", 8), justify="right").grid(row=2, column=0, padx=8, pady=4, sticky="ne")
        self._txt_modelos = tk.Text(frame_form, height=5, width=46,
                                     bg="#0d0d1a", fg="#e8e8ff", insertbackground="white",
                                     relief="flat", font=("Consolas", 8))
        self._txt_modelos.grid(row=2, column=1, padx=4, pady=4, sticky="ew")

        def _preencher_modelos(prov):
            self._txt_modelos.delete("1.0", "end")
            self._txt_modelos.insert("end", "\n".join(PROVEDORES[prov]["modelos"]))

        def on_prov(*_):
            _preencher_modelos(var_prov.get())
        var_prov.trace_add("write", on_prov)
        _preencher_modelos("Groq")

        entradas_tmp = list(self.entradas)

        tk.Label(win, text="Ordem de prioridade (1º = principal, demais = fallback):",
                 bg="#0d0d1a", fg="#7c4dff", font=("Segoe UI", 8, "italic")
                 ).pack(anchor="w", padx=16, pady=(0, 2))

        cols = ("prioridade", "provedor", "n_modelos", "chave")
        lista = ttk.Treeview(win, columns=cols, show="headings", height=6)
        lista.heading("prioridade", text="#")
        lista.heading("provedor",   text="Provedor")
        lista.heading("n_modelos",  text="Modelos")
        lista.heading("chave",      text="API Key")
        lista.column("prioridade", width=30,  stretch=False, anchor="center")
        lista.column("provedor",   width=100, stretch=False)
        lista.column("n_modelos",  width=120, stretch=False, anchor="center")
        lista.column("chave",      width=300)

        def atualizar_lista(manter_idx=None):
            lista.delete(*lista.get_children())
            for i, e in enumerate(entradas_tmp):
                k    = e["chave"]
                mask = k[:8] + "..." + k[-4:] if len(k) > 12 else k
                modelos = e.get("modelos_custom") or PROVEDORES.get(e["provedor"], {}).get("modelos", [])
                label_prio = "★ 1º" if i == 0 else f"  {i+1}º"
                lista.insert("", "end", values=(label_prio, e["provedor"], f"{len(modelos)} modelo(s)", mask))
            if manter_idx is not None:
                filhos = lista.get_children()
                if filhos and 0 <= manter_idx < len(filhos):
                    lista.selection_set(filhos[manter_idx])
                    lista.see(filhos[manter_idx])

        def adicionar():
            chave = var_chave.get().strip()
            prov  = var_prov.get()
            if not chave:
                messagebox.showwarning("Atenção", "Digite uma API Key.", parent=win)
                return
            modelos_custom = [m.strip() for m in self._txt_modelos.get("1.0", "end").splitlines()
                              if m.strip()]
            if not modelos_custom:
                messagebox.showwarning("Atenção", "Adicione ao menos um modelo.", parent=win)
                return
            entradas_tmp.append({"provedor": prov, "chave": chave, "modelos_custom": modelos_custom})
            var_chave.set("")
            _preencher_modelos(prov)
            atualizar_lista(manter_idx=len(entradas_tmp)-1)

        tk.Button(frame_form, text="➕ Adicionar", command=adicionar,
                  bg="#7c4dff", fg="white", relief="flat", padx=12, pady=5,
                  font=("Segoe UI", 9, "bold"), cursor="hand2").grid(row=3, column=1, padx=4, pady=8, sticky="w")

        lista.pack(fill="both", expand=True, padx=16, pady=(0,4))
        atualizar_lista()

        frame_btns = tk.Frame(win, bg="#0d0d1a")
        frame_btns.pack(fill="x", padx=16, pady=8)

        def mover(delta):
            sel = lista.selection()
            if not sel:
                return
            idx = lista.index(sel[0])
            novo = idx + delta
            if novo < 0 or novo >= len(entradas_tmp):
                return
            entradas_tmp[idx], entradas_tmp[novo] = entradas_tmp[novo], entradas_tmp[idx]
            atualizar_lista(manter_idx=novo)

        def remover():
            sel = lista.selection()
            if not sel:
                return
            entradas_tmp.pop(lista.index(sel[0]))
            atualizar_lista()

        def salvar():
            if not entradas_tmp:
                messagebox.showwarning("Atenção", "Adicione ao menos uma entrada.", parent=win)
                return
            self.entradas    = entradas_tmp
            self.gerenciador = GerenciadorFallback(self.entradas)
            salvar_config(self.entradas)
            slots = len(self.gerenciador.slots)
            self.lbl_slot.config(text=f"Slot ativo: {self.gerenciador.status()} ({slots} slots total)", fg="#00e676")
            self.lbl_status.config(text=f"✅ {len(self.entradas)} provedor(es) configurado(s) — {slots} combinações de fallback.")
            win.destroy()

        tk.Button(frame_btns, text="▲ Subir",   command=lambda: mover(-1),
                  bg="#37474f", fg="white", relief="flat", padx=10, pady=5,
                  font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left", padx=4)
        tk.Button(frame_btns, text="▼ Descer",  command=lambda: mover(+1),
                  bg="#37474f", fg="white", relief="flat", padx=10, pady=5,
                  font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left", padx=2)
        tk.Button(frame_btns, text="🗑 Remover", command=remover,
                  bg="#c62828", fg="white", relief="flat", padx=10, pady=5,
                  font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="left", padx=8)
        tk.Button(frame_btns, text="💾 Salvar e Fechar", command=salvar,
                  bg="#00897b", fg="white", relief="flat", padx=10, pady=5,
                  font=("Segoe UI", 9, "bold"), cursor="hand2").pack(side="right", padx=4)

    def selecionar_pasta(self):
        pasta = filedialog.askdirectory(title="Selecione a pasta de músicas")
        if not pasta:
            return
        pasta = pasta.replace("'", "").replace('"', '').strip()
        if not os.path.isdir(pasta):
            messagebox.showerror("Erro", f"Caminho não encontrado:\n{pasta}")
            return
        self.pasta = pasta.strip("'\" ")
        self.lbl_pasta.config(text=f"  {pasta}", fg="#e8e8ff")
        self._atualizar_contagem_pasta()

    def _atualizar_contagem_pasta(self):
        if not self.pasta:
            return
        n = len(listar_arquivos(self.pasta, self.recursivo.get()))
        sub = " (subpastas incluídas)" if self.recursivo.get() else ""
        self.lbl_status.config(text=f"{n} arquivo(s) encontrado(s){sub}. Clique em 'Analisar Arquivos'.")

    def analisar(self):
        if not self.gerenciador:
            messagebox.showwarning("Atenção", "Configure ao menos uma API Key (🔑 API Key).")
            return
        if not self.pasta:
            messagebox.showwarning("Atenção", "Selecione uma pasta primeiro.")
            return
        # Cancela qualquer análise em andamento e reseta o estado
        self._cancelar = True
        self.pausado   = False
        self.btn_pausa.config(text="⏸ Pausar", bg="#37474f")
        self.btn_duplicatas.config(state="disabled")
        self.btn_retentar.config(state="disabled")

        self.arquivos = listar_arquivos(self.pasta, self.recursivo.get())
        if not self.arquivos:
            messagebox.showinfo("Vazio", "Nenhum arquivo novo encontrado.")
            return
        self.tree.delete(*self.tree.get_children())
        self.resultados  = []
        self._cancelar   = False
        self.progress["maximum"] = len(self.arquivos)
        self.progress["value"]   = 0
        self._atualizar_contagem()
        threading.Thread(target=self._analisar_thread, daemon=True).start()

    def _analisar_thread(self):
        try:
            for i, arquivo in enumerate(self.arquivos):
                if self._cancelar:
                    return
                while self.pausado:
                    if self._cancelar:
                        return
                    time.sleep(0.3)

                self.after(0, lambda a=arquivo, n=i: self.lbl_status.config(
                    text=f"[{n+1}/{len(self.arquivos)}] {a} — slot: {self.gerenciador.status()}"))
                self.after(0, lambda: self.lbl_slot.config(
                    text=f"Slot: {self.gerenciador.status()}", fg="#ff9100"))

                ext = os.path.splitext(arquivo)[1].lower()
                ja  = None
                try:
                    ja = extrair_ja_formatado(arquivo)
                    if ja and (_RUIDOS.search(ja[0]) or _RUIDOS.search(ja[1])):
                        ja = None
                    if ja:
                        artista, musica, id_hex = ja
                        artista_novo = normalizar_case(artista)
                        musica_nova  = normalizar_case(musica)
                        novo = f"{limpar_nome(artista_novo)} - {limpar_nome(musica_nova)} - [{id_hex}]{ext}"
                        igual  = os.path.basename(arquivo) == novo
                        status = "Igual" if igual else "Case"
                        tag    = "igual" if igual else "editado"
                        sel    = not igual
                    else:
                        nome_limpo = preparar_para_ia(arquivo)
                        # Verifica banco de dados antes de chamar iTunes/IA
                        id_hex_check = gerar_id(arquivo)
                        entrada_db   = db_buscar(id_hex_check)
                        if entrada_db and entrada_db.get("artista") and entrada_db.get("musica"):
                            artista = entrada_db["artista"]
                            musica  = entrada_db["musica"]
                            self.gerenciador.log.append(f"📋 DB: {os.path.basename(arquivo)}")
                        else:
                            self.gerenciador.log.append(f"📤 Enviado à IA: {nome_limpo!r}")
                            res = buscar_itunes(nome_limpo)
                            if res:
                                self.gerenciador.log.append(f"🍎 iTunes: {os.path.basename(arquivo)}")
                            else:
                                res = self.gerenciador.interpretar(arquivo, nome_limpo=nome_limpo)
                            artista = normalizar_case(limpar_ruido(res.get("artista", "").strip()))
                            musica  = normalizar_case(limpar_ruido(res.get("musica",  "").strip()))
                        if not artista or not musica:
                            raise ValueError("Campos vazios")
                        novo   = formatar_nome(artista, musica, ext, arquivo)
                        igual  = os.path.basename(arquivo) == novo
                        status = "Igual" if igual else "Pronto"
                        tag    = "igual" if igual else "ok"
                        sel    = not igual
                except Exception as ex:
                    artista, musica, novo = "", "", os.path.basename(arquivo)
                    status, tag, sel = "Erro", "erro", False
                    self.gerenciador.log.append(f"❌ Erro em {os.path.basename(arquivo)}: {ex}")

                r = {"arquivo": arquivo, "artista": artista, "musica": musica,
                     "novo_nome": novo, "status": status, "tag": tag, "sel": sel}
                self.resultados.append(r)
                self.after(0, lambda r=r, t=tag: self._inserir_linha(r, t))
                self.after(0, lambda v=i+1: self.progress.configure(value=v))
                self.after(0, self._atualizar_contagem)
                if not ja:
                    time.sleep(0.05)

            iguais = sum(1 for r in self.resultados if r["status"] == "Igual")
            erros  = sum(1 for r in self.resultados if r["status"] == "Erro")
            msg = f"{len(self.arquivos)} arquivo(s) analisados"
            if iguais: msg += f" · {iguais} já no padrão"
            if erros:  msg += f" · {erros} com erro"
            self.after(0, lambda ig=iguais: self.lbl_status.config(
                text=f"✅ Análise concluída — {len(self.arquivos)} arquivo(s)"
                     + (f" · {ig} já no padrão (ocultados)" if ig else "") +
                     ". Revise e renomeie."))
            self.after(0, lambda: self.lbl_slot.config(
                text=f"Slot: {self.gerenciador.status()}", fg="#00e676"))
            _notificar("Voxly Renomeador", msg)
        finally:
            self.after(0, lambda: self.btn_duplicatas.config(state="normal"))
            tem_erros = any(r["status"] == "Erro" for r in self.resultados)
            self.after(0, lambda te=tem_erros: self.btn_retentar.config(
                state="normal" if te else "disabled"))
            if self._auto_renomear:
                self._auto_renomear = False
                self.after(300, self._renomear_auto)

    def _renomear_auto(self):
        selecionados = [
            (i, r) for i, r in enumerate(self.resultados)
            if r["status"] in ("Pronto", "Case", "Editado")
            and os.path.basename(r["arquivo"]) != r["novo_nome"]
        ]
        if not selecionados:
            self.lbl_status.config(text="✅ Download e análise concluídos. Nenhum arquivo para renomear.")
            return
        ok = erros = 0
        for idx, r in selecionados:
            try:
                subdir       = os.path.dirname(r["arquivo"])
                relativo_ant = r["arquivo"]
                old_path     = os.path.join(self.pasta, relativo_ant)
                new_path     = os.path.join(self.pasta, subdir, r["novo_nome"])
                os.rename(old_path, new_path)
                db_registrar(gerar_id(relativo_ant),
                             os.path.splitext(os.path.basename(relativo_ant))[0],
                             r["artista"], r["musica"], r["novo_nome"])
                r["arquivo"] = os.path.join(subdir, r["novo_nome"]) if subdir else r["novo_nome"]
                r["status"]  = "✅ Renomeado"
                r["sel"]     = False
                self.tree.item(str(idx),
                               values=("☐", r["arquivo"], r["artista"], r["musica"],
                                       r["novo_nome"], "✅ Renomeado"),
                               tags=("igual",))
                ok += 1
            except Exception as e:
                r["status"] = f"❌ {e}"
                self.tree.item(str(idx),
                               values=("☐", r["arquivo"], r["artista"], r["musica"],
                                       r["novo_nome"], "❌ Erro"),
                               tags=("erro",))
                erros += 1
        self.lbl_status.config(
            text=f"✅ Auto-renomeado: {ok} arquivo(s)" + (f" · ❌ {erros} erro(s)" if erros else ""))
        self._atualizar_contagem()
        _notificar("Voxly Renomeador", f"Renomeado: {ok} arquivo(s)")

    def _inserir_linha(self, r, tag):
        if r["status"] == "Igual":
            return
        iid = str(len(self.resultados) - 1)
        sel = "☑" if r["sel"] else "☐"
        self.tree.insert("", "end", iid=iid, tags=(tag,),
                         values=(sel, r["arquivo"], r["artista"], r["musica"], r["novo_nome"], r["status"]))

    def _toggle_sel(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        try:
            idx = int(item)
        except ValueError:
            return
        if idx >= len(self.resultados):
            return
        r = self.resultados[idx]
        if r["status"] in ("Igual", "Renomeado"):
            return
        r["sel"] = not r["sel"]
        vals      = list(self.tree.item(item, "values"))
        vals[0]   = "☑" if r["sel"] else "☐"
        self.tree.item(item, values=vals)
        self._atualizar_contagem()

    def _editar(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        try:
            idx = int(item)
        except ValueError:
            return
        if idx >= len(self.resultados):
            return
        r = self.resultados[idx]

        win = tk.Toplevel(self)
        win.title("Editar")
        win.geometry("540x190")
        win.configure(bg="#0d0d1a")
        win.grab_set()

        entries = {}
        for row, lbl, key in [(0, "Artista", "artista"), (1, "Música", "musica")]:
            tk.Label(win, text=lbl+":", bg="#0d0d1a", fg="#bbb",
                     font=("Segoe UI", 10)).grid(row=row, column=0, padx=14, pady=10, sticky="w")
            e = tk.Entry(win, width=52, bg="#1a1a2e", fg="#e8e8ff",
                         insertbackground="white", relief="flat", font=("Segoe UI", 10))
            e.insert(0, r[key])
            e.grid(row=row, column=1, padx=8, pady=10)
            entries[key] = e

        def salvar():
            ext           = os.path.splitext(r["arquivo"])[1].lower()
            r["artista"]  = entries["artista"].get().strip()
            r["musica"]   = entries["musica"].get().strip()
            r["novo_nome"]= formatar_nome(r["artista"], r["musica"], ext, r["arquivo"])
            r["status"]   = "Editado"
            r["sel"]      = True
            self.tree.item(item,
                           values=("☑", r["arquivo"], r["artista"], r["musica"], r["novo_nome"], "Editado"),
                           tags=("editado",))
            self._atualizar_contagem()
            win.destroy()

        tk.Button(win, text="💾 Salvar", command=salvar,
                  bg="#7c4dff", fg="white", relief="flat", padx=14, pady=6,
                  font=("Segoe UI", 10, "bold")).grid(row=2, column=1, pady=10, sticky="e", padx=8)

    def marcar_todos(self):
        for item in self.tree.get_children():
            try:
                idx = int(item)
            except ValueError:
                continue
            if idx >= len(self.resultados):
                break
            r = self.resultados[idx]
            if r["status"] in ("Igual", "Renomeado"):
                continue
            r["sel"] = True
            vals = list(self.tree.item(item, "values"))
            vals[0] = "☑"
            self.tree.item(item, values=vals)
        self._atualizar_contagem()

    def desmarcar_todos(self):
        for item in self.tree.get_children():
            try:
                idx = int(item)
            except ValueError:
                continue
            if idx >= len(self.resultados):
                break
            self.resultados[idx]["sel"] = False
            vals = list(self.tree.item(item, "values"))
            vals[0] = "☐"
            self.tree.item(item, values=vals)
        self._atualizar_contagem()

    def _atualizar_contagem(self):
        n = sum(1 for r in self.resultados if r["sel"])
        self.lbl_contagem.config(text=f"{n} selecionado(s)")

    def renomear(self):
        selecionados = [(i, r) for i, r in enumerate(self.resultados)
                        if r["sel"] and os.path.basename(r["arquivo"]) != r["novo_nome"]]
        if not selecionados:
            messagebox.showinfo("Nada a fazer", "Nenhum arquivo selecionado para renomear.")
            return
        if not messagebox.askyesno("Confirmar", f"Renomear {len(selecionados)} arquivo(s)?"):
            return
        ok = erros = 0
        for idx, r in selecionados:
            try:
                subdir       = os.path.dirname(r["arquivo"])
                relativo_ant = r["arquivo"]
                old_path     = os.path.join(self.pasta, relativo_ant)
                new_path     = os.path.join(self.pasta, subdir, r["novo_nome"])
                os.rename(old_path, new_path)
                db_registrar(gerar_id(relativo_ant),
                             os.path.splitext(os.path.basename(relativo_ant))[0],
                             r["artista"], r["musica"], r["novo_nome"])
                r["arquivo"] = os.path.join(subdir, r["novo_nome"]) if subdir else r["novo_nome"]
                r["status"]  = "✅ Renomeado"
                r["sel"]     = False
                self.tree.item(str(idx), values=(
                    "☐", r["arquivo"], r["artista"], r["musica"], r["novo_nome"], "✅ Renomeado"),
                    tags=("igual",))
                ok += 1
            except Exception as e:
                r["status"] = f"❌ {e}"
                self.tree.item(str(idx), values=(
                    "☐", r["arquivo"], r["artista"], r["musica"], r["novo_nome"], f"❌ Erro"),
                    tags=("erro",))
                erros += 1
        messagebox.showinfo("Concluído", f"✅ {ok} renomeado(s)\n❌ {erros} erro(s)")
        self.lbl_status.config(text=f"✅ {ok} arquivo(s) renomeado(s).")
        self._atualizar_contagem()

    def _retentar_erros(self):
        erros = [(i, r) for i, r in enumerate(self.resultados) if r["status"] == "Erro"]
        if not erros:
            return
        self.btn_retentar.config(state="disabled")
        self.btn_duplicatas.config(state="disabled")
        self._cancelar = False
        self.progress["maximum"] = len(erros)
        self.progress["value"]   = 0
        threading.Thread(target=self._retentar_thread, args=(erros,), daemon=True).start()

    def _retentar_thread(self, erros):
        for progresso, (idx, r) in enumerate(erros):
            if self._cancelar:
                return
            arquivo = r["arquivo"]
            self.after(0, lambda a=arquivo, p=progresso, t=len(erros): self.lbl_status.config(
                text=f"[{p+1}/{t}] Retentando: {a}"))
            ext = os.path.splitext(arquivo)[1].lower()
            try:
                res     = self.gerenciador.interpretar(arquivo)
                artista = normalizar_case(limpar_ruido(res.get("artista", "").strip()))
                musica  = normalizar_case(limpar_ruido(res.get("musica",  "").strip()))
                if not artista or not musica:
                    raise ValueError("Campos vazios")
                novo   = formatar_nome(artista, musica, ext, arquivo)
                igual  = os.path.basename(arquivo) == novo
                status = "Igual" if igual else "Pronto"
                tag    = "igual" if igual else "ok"
                sel    = not igual
                r.update(artista=artista, musica=musica, novo_nome=novo, status=status, tag=tag, sel=sel)
                if not igual:
                    self.after(0, lambda i=idx, rv=r, tg=tag: self.tree.item(
                        str(i), values=("☑", rv["arquivo"], rv["artista"], rv["musica"], rv["novo_nome"], rv["status"]),
                        tags=(tg,)))
                else:
                    self.after(0, lambda i=idx: self.tree.delete(str(i)) if str(i) in self.tree.get_children() else None)
            except Exception:
                pass
            self.after(0, lambda v=progresso+1: self.progress.configure(value=v))
            self.after(0, self._atualizar_contagem)
            time.sleep(0.2)

        tem_erros = any(r["status"] == "Erro" for r in self.resultados)
        self.after(0, lambda te=tem_erros: self.btn_retentar.config(
            state="normal" if te else "disabled"))
        self.after(0, lambda: self.btn_duplicatas.config(state="normal"))
        self.after(0, lambda: self.lbl_status.config(text="✅ Retentativa concluída."))
        _notificar("Voxly Renomeador", "Retentativa concluída.")

    def _abrir_duplicatas(self):
        try:
            grupos = {}
            for r in self.resultados:
                if not r["artista"] or not r["musica"] or r["status"] in ("Erro", "🗑 Excluído"):
                    continue
                chave = (r["artista"].strip(), r["musica"].strip())
                grupos.setdefault(chave, []).append(r)
            duplicatas = {k: v for k, v in grupos.items() if len(v) > 1}
            if not duplicatas:
                messagebox.showinfo("Sem Duplicatas",
                                    "Nenhuma duplicata encontrada nos arquivos analisados.",
                                    parent=self)
                return
            DuplicatasWindow(self, self.pasta, duplicatas)
        except Exception as e:
            messagebox.showerror("Erro ao buscar duplicatas", str(e), parent=self)


def main():
    App().mainloop()

if __name__ == "__main__":
    main()
