import xbmc, xbmcvfs, os, json, re

ADDON_DATA    = xbmcvfs.translatePath("special://profile/addon_data/script.context.smartlibrary/")
LIB_BASE      = os.path.join(ADDON_DATA, "Library/")
TVSHOWS_DIR   = os.path.join(LIB_BASE, "TVShows/")
MOVIES_DIR    = os.path.join(LIB_BASE, "Movies/")
METADATA_FILE = os.path.join(ADDON_DATA, "metadata.json")
SOURCES_FILE  = xbmcvfs.translatePath("special://profile/sources.xml")
LOG_TAG       = "[SmartLibrary]"
ADDON_ID      = "script.context.smartlibrary"

EPISODE_PATTERNS = [
    r'[Ss]\d{1,2}[Ee](\d{1,3})',
    r'\d{1,2}[xX](\d{2,3})',
    r'\b(?:ep(?:isodio)?\.?\s*)(\d+)',
    r'\b(?:cap(?:[íi]tulo)?\.?\s*)(\d+)',
]
PROMO_KEYWORDS = re.compile(
    r'\b(trailer|teaser|adelanto|avance|promo|clip|preview|featurette|making.?of|behind.the.scenes|extra|bonus)\b',
    re.IGNORECASE
)


def log(msg):
    xbmc.log(f"{LOG_TAG} {msg}", xbmc.LOGINFO)

def is_promo(label):
    return bool(PROMO_KEYWORDS.search(label or ''))

def get_items(path, media="video"):
    """Intenta obtener items de un path. Prueba 'video' y 'files' si falla."""
    for m in [media, "files"]:
        req = json.dumps({
            "jsonrpc": "2.0", "method": "Files.GetDirectory",
            "params": {"directory": path, "media": m,
                       "properties": ["season", "episode", "title", "file"]},
            "id": 1
        })
        try:
            res  = json.loads(xbmc.executeJSONRPC(req))
            err  = res.get('error')
            files = res.get('result', {}).get('files', [])
            if err:
                log(f"  get_items [{m}] error: {err.get('message','?')} — {path[:60]}")
                continue
            if files:
                log(f"  get_items [{m}] OK: {len(files)} items")
                return files
            log(f"  get_items [{m}] 0 items para: {path[:60]}")
        except Exception as e:
            log(f"  get_items excepcion [{m}]: {e}")
    return []

def get_episode_number(ep, fallback_idx):
    n = ep.get('episode', -1)
    if isinstance(n, int) and n > 0:
        return n
    lbl = ep.get('label', '') or ''
    for pat in EPISODE_PATTERNS:
        m = re.search(pat, lbl, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return fallback_idx + 1

def load_metadata():
    if not xbmcvfs.exists(METADATA_FILE):
        log("metadata.json no existe aún")
        return {"tvshows": {}, "movies": {}}
    try:
        with xbmcvfs.File(METADATA_FILE, 'r') as f:
            data = f.read()
        meta = json.loads(data)
        log(f"Metadata: {len(meta.get('tvshows',{}))} series, {len(meta.get('movies',{}))} pelis")
        return meta
    except Exception as e:
        log(f"load_metadata error: {e}")
        return {"tvshows": {}, "movies": {}}

def save_metadata(meta):
    try:
        with xbmcvfs.File(METADATA_FILE, 'w') as f:
            f.write(json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f"save_metadata error: {e}")


# ── Fuentes ───────────────────────────────────────────────────────────────────

def ensure_source(path, name):
    try:
        if not xbmcvfs.exists(SOURCES_FILE):
            xml = '<?xml version="1.0" encoding="utf-8"?>\n<sources>\n</sources>'
        else:
            with xbmcvfs.File(SOURCES_FILE, 'r') as f:
                xml = f.read()

        path_clean = path.rstrip('/\\').replace('\\', '/')
        if path_clean in xml:
            return

        log(f"Añadiendo fuente: {name}")
        block = (f'\n        <source>\n'
                 f'            <name>{name}</name>\n'
                 f'            <path pathversion="1">{path}</path>\n'
                 f'            <allowsharing>true</allowsharing>\n'
                 f'        </source>')

        if '<video>' in xml:
            xml = xml.replace('</video>', block + '\n    </video>', 1)
        else:
            xml = xml.replace('</sources>',
                f'\n    <video>\n        <default pathversion="1"></default>'
                + block + '\n    </video>\n</sources>')

        with xbmcvfs.File(SOURCES_FILE, 'w') as f:
            f.write(xml)
        log(f"Fuente añadida OK: {path}")
    except Exception as e:
        log(f"ensure_source error: {e}")

def ensure_library_sources():
    for d in [TVSHOWS_DIR, MOVIES_DIR]:
        if not xbmcvfs.exists(d):
            xbmcvfs.mkdirs(d)
    ensure_source(TVSHOWS_DIR, "Smart Library – Series")
    ensure_source(MOVIES_DIR,  "Smart Library – Películas")


# ── Scan ──────────────────────────────────────────────────────────────────────

def scan_library():
    log(f"Iniciando scan: {TVSHOWS_DIR}")
    req = json.dumps({
        "jsonrpc": "2.0", "method": "VideoLibrary.Scan",
        "params": {"directory": TVSHOWS_DIR, "showdialogs": False},
        "id": 1
    })
    res = json.loads(xbmc.executeJSONRPC(req))
    log(f"Scan resultado: {res.get('result', res.get('error', '?'))}")


# ── Comprobación ──────────────────────────────────────────────────────────────

def check_and_update():
    meta    = load_metadata()
    total_new = 0

    for show, seasons in meta.get("tvshows", {}).items():
        show_dir = os.path.join(TVSHOWS_DIR, show)

        if not xbmcvfs.exists(show_dir):
            log(f"Recreando carpeta: {show_dir}")
            xbmcvfs.mkdirs(show_dir)

        log(f"Comprobando: {show}")

        for s_num_str, season_path in seasons.items():
            if not season_path:
                continue
            s_num = int(s_num_str)
            log(f"  T{s_num} path: {season_path[:80]}")

            episodes = [ep for ep in get_items(season_path)
                        if ep.get('filetype') != 'directory'
                        and not is_promo(ep.get('label', ''))]

            log(f"  T{s_num}: {len(episodes)} episodio(s) encontrado(s)")

            for i, ep in enumerate(episodes):
                ep_url = ep.get('file', '') or ''
                if not ep_url:
                    continue
                e_num    = get_episode_number(ep, i)
                filename = f"{show} S{s_num:02d}E{e_num:02d}.strm"
                filepath = os.path.join(show_dir, filename)

                if not xbmcvfs.exists(filepath):
                    log(f"  → Nuevo: {filename}")
                    with xbmcvfs.File(filepath, 'w') as f:
                        f.write(ep_url)
                    total_new += 1
                else:
                    log(f"  Ya existe: {filename}")

    return total_new


# ── Servicio ──────────────────────────────────────────────────────────────────

class UpdateService(xbmc.Monitor):

    def onNotification(self, sender, method, data):
        if sender == ADDON_ID or ADDON_ID in method:
            if 'Updated' in method:
                log("Notificación recibida — lanzando check")
                xbmc.sleep(2000)
                self._run_check("(notificación)")

    def _run_check(self, context=""):
        log(f"=== Check {context} ===")
        try:
            total_new = check_and_update()
            if total_new > 0:
                log(f"{total_new} ep(s) nuevo(s) — escaneando librería")
                scan_library()
            else:
                log("Sin episodios nuevos")
        except Exception as e:
            log(f"Error en _run_check: {e}")
        log(f"=== Fin check {context} ===")

    def run(self):
        log("Servicio iniciado")
        # Esperar a que Kodi y los plugins estén listos
        if self.waitForAbort(30):
            return

        ensure_library_sources()
        self._run_check("(arranque)")

        # Comprobación periódica cada 6 horas
        while not self.waitForAbort(6 * 3600):
            self._run_check("(periódica)")


if __name__ == '__main__':
    UpdateService().run()
