import xbmc, xbmcvfs, os, json, re, threading, sqlite3

ADDON_DATA    = xbmcvfs.translatePath("special://profile/addon_data/script.context.smartlibrary/")
LIB_BASE      = os.path.join(ADDON_DATA, "Library/")
TVSHOWS_DIR   = os.path.join(LIB_BASE, "TVShows/")
MOVIES_DIR    = os.path.join(LIB_BASE, "Movies/")
METADATA_FILE = os.path.join(ADDON_DATA, "metadata.json")
SOURCES_FILE  = xbmcvfs.translatePath("special://profile/sources.xml")
LOG_TAG       = "[SmartLibrary]"
ADDON_ID      = "script.context.smartlibrary"

EPISODE_PATTERNS = [
    r'[Ss](\d{1,2})[Ee](\d{1,3})',
    r'(\d{1,2})[xX](\d{2,3})',
    r'\b(?:ep(?:isodio)?\.?\s*)(\d+)',
    r'\b(?:cap(?:[íi]tulo)?\.?\s*)(\d+)',
]
PROMO_KEYWORDS = re.compile(
    r'\b(trailer|teaser|adelanto|avance|promo|clip|preview|featurette|making.?of|behind.the.scenes|extra|bonus)\b',
    re.IGNORECASE
)

# ── Ruta a la BD de Kodi ──────────────────────────────────────────────────────
DB_PATH = xbmcvfs.translatePath("special://database/")
KODI_DB = None
for f in sorted(os.listdir(DB_PATH), reverse=True):
    if f.startswith("MyVideos") and f.endswith(".db"):
        KODI_DB = os.path.join(DB_PATH, f)
        break

log = lambda msg: xbmc.log(f"{LOG_TAG} {msg}", xbmc.LOGINFO)


def is_promo(label):
    return bool(PROMO_KEYWORDS.search(label or ''))


def get_items(path, media="video"):
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


def get_items_with_timeout(path, media="video", timeout=20):
    result = {}

    def worker():
        result['files'] = get_items(path, media)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log(f"  TIMEOUT ({timeout}s) esperando a: {path[:70]} — se omite esta temporada")
        return []
    return result.get('files', [])


def get_episode_number(ep, fallback_idx):
    n = ep.get('episode', -1)
    if isinstance(n, int) and n > 0:
        return n
    lbl = ep.get('label', '') or ''
    for pat in EPISODE_PATTERNS:
        m = re.search(pat, lbl, re.IGNORECASE)
        if m:
            try:
                return int(m.group(2) if 'Ee' in pat or 'xX' in pat else m.group(1))
            except (ValueError, IndexError):
                pass
    # Fallback: extraer numero del filepath
    for pat in EPISODE_PATTERNS:
        m = re.search(pat, ep.get('file', ''), re.IGNORECASE)
        if m:
            try:
                return int(m.group(2) if 'Ee' in pat or 'xX' in pat else m.group(1))
            except (ValueError, IndexError):
                pass
    return fallback_idx + 1


# ── Metadatos ─────────────────────────────────────────────────────────────────

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


# ── Inyección directa en la BD de Kodi ───────────────────────────────────────

def db_connect():
    """Conecta a la BD de Kodi (MyVideos*.db) en modo WAL para lectura."""
    if not KODI_DB or not os.path.exists(KODI_DB):
        log(f"ERROR: BD no encontrada en {DB_PATH}")
        return None
    try:
        conn = sqlite3.connect(KODI_DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except Exception as e:
        log(f"ERROR conectando BD: {e}")
        return None


def ensure_path(conn, path_str, content="tvshows", scraper="metadata.tvshows.themoviedb.org.python"):
    """Crea o recupera el idPath para un directorio."""
    cur = conn.cursor()
    cur.execute("SELECT idPath FROM path WHERE strPath=?", (path_str,))
    row = cur.fetchone()
    if row:
        # Actualizar scraper si está vacío
        cur.execute("UPDATE path SET strContent=COALESCE(NULLIF(strContent,''),?), strScraper=COALESCE(NULLIF(strScraper,''),?) WHERE idPath=?",
                    (content, scraper, row[0]))
        conn.commit()
        return row[0]
    cur.execute(
        "INSERT INTO path (strPath, strContent, strScraper, scanRecursive, useFolderNames, noUpdate) VALUES (?,?,?,0,0,0)",
        (path_str, content, scraper))
    conn.commit()
    return cur.lastrowid


def ensure_tvshow(conn, show_name):
    """Crea o recupera el idShow para una serie."""
    cur = conn.cursor()
    cur.execute("SELECT idShow FROM tvshow WHERE c00=?", (show_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO tvshow (c00, userrating) VALUES (?, 0)",
        (show_name,))
    conn.commit()
    return cur.lastrowid


def ensure_season(conn, id_show, season_num):
    """Crea o recupera el idSeason."""
    cur = conn.cursor()
    cur.execute("SELECT idSeason FROM seasons WHERE idShow=? AND season=?",
                (id_show, season_num))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO seasons (idShow, season, name, userrating) VALUES (?,?,?,0)",
        (id_show, season_num, f"Season {season_num}"))
    conn.commit()
    return cur.lastrowid


def ensure_file(conn, id_path, filename):
    """Crea o recupera el idFile para un archivo .strm."""
    cur = conn.cursor()
    cur.execute("SELECT idFile FROM files WHERE idPath=? AND strFilename=?",
                (id_path, filename))
    row = cur.fetchone()
    if row:
        return row[0]
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "INSERT INTO files (idPath, strFilename, dateAdded) VALUES (?,?,?)",
        (id_path, filename, now))
    conn.commit()
    return cur.lastrowid


def episode_exists(conn, id_file):
    """Comprueba si un idFile ya tiene entrada en episode."""
    cur = conn.cursor()
    cur.execute("SELECT idEpisode FROM episode WHERE idFile=?", (id_file,))
    return cur.fetchone() is not None


def add_episode_to_db(show_dir, strm_filename, show_name, s_num, e_num, ep_title=""):
    """
    Inyecta un episodio directamente en la BD de Kodi para que aparezca
    en la videoteca sin depender del scraper.
    """
    if not KODI_DB:
        log("  add_episode: sin BD, abortando")
        return False
    conn = db_connect()
    if not conn:
        return False
    try:
        full_path = os.path.join(show_dir, strm_filename)
        # 1. Asegurar path del directorio de la serie
        id_path = ensure_path(conn, show_dir + "/", "tvshows", "metadata.tvshows.themoviedb.org.python")
        # 2. Asegurar TV show
        id_show = ensure_tvshow(conn, show_name)
        # 3. Asegurar season
        id_season = ensure_season(conn, id_show, s_num)
        # 4. Asegurar file
        id_file = ensure_file(conn, id_path, strm_filename)
        # 5. Si ya existe el episodio, salir
        if episode_exists(conn, id_file):
            log(f"  add_episode: ya en BD: {strm_filename}")
            conn.close()
            return True
        # 6. Insertar episodio
        cur = conn.cursor()
        title = ep_title or f"{show_name} S{s_num:02d}E{e_num:02d}"
        cur.execute(
            """INSERT INTO episode (idFile, c00, c12, c13, idShow, idSeason)
               VALUES (?,?,?,?,?,?)""",
            (id_file, title, str(s_num), str(e_num), id_show, id_season))
        conn.commit()
        log(f"  ✓ Episodio inyectado en BD: {strm_filename}")
        conn.close()
        return True
    except Exception as e:
        log(f"  ERROR add_episode: {e}")
        try:
            conn.close()
        except:
            pass
        return False


def add_movie_to_db(movie_dir, strm_filename, movie_title):
    """Inyecta una película directamente en la BD de Kodi."""
    if not KODI_DB:
        return False
    conn = db_connect()
    if not conn:
        return False
    try:
        full_path = os.path.join(movie_dir, strm_filename)
        id_path = ensure_path(conn, movie_dir + "/", "movies", "metadata.tvshows.themoviedb.org.python")
        id_file = ensure_file(conn, id_path, strm_filename)
        cur = conn.cursor()
        cur.execute("SELECT idMovie FROM movie WHERE idFile=?", (id_file,))
        if cur.fetchone():
            conn.close()
            return True
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            "INSERT INTO movie (idFile, c00, premiered, dateAdded) VALUES (?,?,?,?)",
            (id_file, movie_title, now[:10], now))
        conn.commit()
        log(f"  ✓ Película inyectada en BD: {strm_filename}")
        conn.close()
        return True
    except Exception as e:
        log(f"ERROR add_movie: {e}")
        try:
            conn.close()
        except:
            pass
        return False


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
        try:
            if not xbmcvfs.exists(d):
                xbmcvfs.mkdirs(d)
        except Exception as e:
            log(f"ensure_library_sources error creando {d}: {e}")
    ensure_source(TVSHOWS_DIR, "Smart Library – Series")
    ensure_source(MOVIES_DIR,  "Smart Library – Películas")


# ── Comprobación ──────────────────────────────────────────────────────────────

def check_and_update():
    meta    = load_metadata()
    total_new = 0

    # Saltar series eliminadas manualmente por el usuario
    removed = meta.get("removed_series", [])
    active_shows = {s: seas for s, seas in meta.get("tvshows", {}).items()
                    if s not in removed}
    for show, seasons in active_shows.items():
        try:
            show_dir = os.path.join(TVSHOWS_DIR, show)

            if not xbmcvfs.exists(show_dir):
                log(f"Recreando carpeta: {show_dir}")
                xbmcvfs.mkdirs(show_dir)

            log(f"Comprobando: {show}")

            for s_num_str, season_path in seasons.items():
                try:
                    if not season_path:
                        continue
                    s_num = int(s_num_str)
                    log(f"  T{s_num} path: {season_path[:80]}")

                    episodes = [ep for ep in get_items_with_timeout(season_path)
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

                        # Ya no inyectamos en BD - el scanner de Kodi lo hara

                        pass

                except Exception as e:
                    log(f"  ERROR en {show} T{s_num_str}, se omite esta temporada: {e}")
                    continue
        except Exception as e:
            log(f"ERROR comprobando '{show}', se omite esta serie: {e}")
            continue

    # También inyectar películas pendientes
    for movie_title, movie_path in meta.get("movies", {}).items():
        if movie_title in removed:
            continue
        try:
            movie_dir = os.path.join(MOVIES_DIR, movie_title)
            strm_name = f"{movie_title}.strm"
            strm_path = os.path.join(movie_dir, strm_name)

            # Movies tambien se dejan al scanner
            if xbmcvfs.exists(strm_path):
                pass
        except Exception as e:
            log(f"ERROR inyectando película '{movie_title}': {e}")
            continue

    return total_new


# ── Servicio ──────────────────────────────────────────────────────────────────

class UpdateService(xbmc.Monitor):

    def onNotification(self, sender, method, data):
        if ADDON_ID in method and 'Updated' in method:
            log("Notificación recibida — lanzando check")
            xbmc.sleep(2000)
            self._run_check("(notificación)")

    def _run_check(self, context=""):
        log(f"=== Check {context} ===")
        try:
            total_new = check_and_update()
            if total_new > 0:
                log(f"{total_new} ep(s) nuevo(s) creados en disco")
            else:
                log("Sin episodios nuevos en disco")
                pass

        except Exception as e:
            log(f"Error en _run_check: {e}")
        log(f"=== Fin check {context} ===")

    def run(self):
        log("Servicio iniciado")
        if self.waitForAbort(30):
            return

        try:
            ensure_library_sources()
        except Exception as e:
            log(f"ERROR en ensure_library_sources (continuo igualmente): {e}")

        # Pasar todos los episodios/pelis existentes a la BD
        # (por si se añadieron antes de tener esta versión)
        log("Inyectando episodios existentes en la BD de Kodi...")
        check_and_update()

        # Limpiar entradas huérfanas de la BD de Kodi (directorios que ya no
        # existen pero siguen en la tabla path)
        try:
            conn = db_connect()
            if conn:
                cur = conn.cursor()
                # Paths de series que ya no existen en disco
                cur.execute("""
                    SELECT p.idPath, p.strPath FROM path p
                    WHERE p.strPath LIKE '%smartlibrary%TVShows/%'
                    AND p.strContent IS NULL
                    AND p.idPath NOT IN (
                        SELECT p2.idPath FROM path p2
                        JOIN files f ON f.idPath = p2.idPath
                    )
                """)
                orphan_paths = cur.fetchall()
                for pid, ppath in orphan_paths:
                    if not os.path.exists(ppath):
                        log(f"Limpiando path huérfano: {ppath}")
                        cur.execute("DELETE FROM path WHERE idPath=?", (pid,))
                conn.commit()
                conn.close()
                log(f"Limpieza de paths completada")
        except Exception as e:
            log(f"Error en limpieza de paths: {e}")

        log("Solo .strm - sin inyeccion en biblioteca")

        # Comprobación periódica cada 6 horas
        while not self.waitForAbort(6 * 3600):
            self._run_check("(periódica)")


if __name__ == '__main__':
    UpdateService().run()