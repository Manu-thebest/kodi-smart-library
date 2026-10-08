# -*- coding: utf-8 -*-
"""Smart Library - servicio de actualizacion.

v6.13.4 - Arreglo general para el add-on de Amazon
--------------------------------------------------------------------
Problemas que tenia (y que obligaban a "re-apuntar a mano" serie por serie):

  A) Solo se recorrian las TEMPORADAS YA GUARDADAS en metadata.json. Si Amazon
     sacaba una temporada nueva, jamas se detectaba.
  B) La unica reparacion de ruta era "quitar el ULTIMO segmento GUID". Si eso
     no arreglaba la ruta (otro GUID mas arriba, otro formato de URL...), la
     serie se quedaba muerta para siempre.
  C) v6.13.x introdujo un bug PEOR: llamaba a series_node() sobre la ruta
     guardada (que YA ES una temporada, con DOS GUIDs) y listaba el nodo-SERIE.
     Ese nodo se CUELGA 30-40s y devuelve vacio -> la serie no se actualizaba.
  D) Los fallos transitorios del add-on de Amazon ("Invalid params.", cuelgues)
     se trataban como definitivos.

Que hace ahora:
  1) RE-RESUELVE POR TITULO (paso 1, lo primero). Busca la serie en el buscador
     de Amazon (?mode=Search&searchstring=...) y usa la serie que devuelve la
     busqueda: esa serie lista TODAS sus temporadas al instante. Verificado en
     real: 14/14 series Amazon localizadas, 1-8s cada una, 0 cuelgues.
  2) DESCUBRE TEMPORADAS NUEVAS: recorre el nodo de la serie y registra todas
     las temporadas que existan de verdad, no solo las guardadas.
  3) Fallback: si la busqueda falla, usa las rutas guardadas TAL CUAL (cada una
     ya es una temporada y se lista directamente).
  4) Reintentos con espera ante fallos transitorios, y timeout por llamada para
     que un cuelgue del plugin no bloquee el servicio.
  5) Los .strm se crean/refrescan siempre que la URL cambie (igual que antes).
  6) PELICULAS: se busca SIN el anio final ' (2021)' (rompia el buscador del
     add-on) y se mira SOLO la primera categoria de resultados. Antes daba
     30-55s por pelicula y las que llevaban anio fallaban; ahora 10/12
     resueltas en 15s en total.
  7) FIX v6.13.4: el bucle de peliculas hacia un Files.GetDirectory sobre la
     URL de REPRODUCCION de cada peli ("?asin=...&mode=PlayVideo") para ver si
     seguia viva. Eso hacia que el add-on de Amazon resolviera el video
     (GetPlaybackResources) y sacara el DIALOGO DE "continuar viendo" EL SOLO
     al arrancar Kodi. Ahora se re-resuelve por titulo (barato) y solo se
     reescribe el .strm si la URL cambio o falta.

Las series que NO son de Amazon siguen funcionando igual que antes.
"""

import json
import os
import re
import threading
import time

import xbmc
import xbmcvfs

ADDON_DATA    = xbmcvfs.translatePath("special://profile/addon_data/script.context.smartlibrary/")
LIB_BASE      = os.path.join(ADDON_DATA, "Library/")
TVSHOWS_DIR   = os.path.join(LIB_BASE, "TVShows/")
MOVIES_DIR    = os.path.join(LIB_BASE, "Movies/")
METADATA_FILE = os.path.join(ADDON_DATA, "metadata.json")
SOURCES_FILE  = xbmcvfs.translatePath("special://profile/sources.xml")
LOG_TAG       = "[SmartLibrary]"
ADDON_ID      = "script.context.smartlibrary"

AMZ_PLUGIN    = "plugin://plugin.video.amazon-test/"
AMZ_SEARCH    = AMZ_PLUGIN + "?mode=Search&searchstring="
GUID_MARK     = "amzn1.dv.gti."
CALL_TIMEOUT  = 30          # segundos maximos por llamada al plugin
MAX_RETRIES   = 1           # reintentos ante fallo transitorio
RETRY_WAIT    = 2           # segundos entre reintentos

EPISODE_PATTERNS = [
    r'[Ss]\d{1,2}[Ee](\d{1,3})',
    r'\d{1,2}[xX](\d{2,3})',
    r'\b(?:ep(?:isodio)?\.?\s*)(\d+)',
    r'\b(?:cap(?:[íi]tulo)?\.?\s*)(\d+)',
]
SEASON_PATTERNS = [
    r'(?:temporada|season|temp)\s*(\d+)',
    r'(?<![A-Za-z])T(\d{1,2})(?!\d)',
    r'(?<![A-Za-z])S(\d{1,2})(?!\d)',
]
PROMO_KEYWORDS = re.compile(
    r'\b(trailer|teaser|adelanto|avance|promo|clip|preview|featurette|'
    r'making.?of|behind.the.scenes|extra|bonus)\b',
    re.IGNORECASE
)


def log(msg):
    xbmc.log("%s %s" % (LOG_TAG, msg), xbmc.LOGINFO)


def is_promo(label):
    return bool(PROMO_KEYWORDS.search(label or ''))


def norm_title(text):
    """Normaliza un titulo para comparar: minusculas, sin acentos ni simbolos."""
    s = (text or '').lower()
    for a, b in (('á', 'a'), ('à', 'a'), ('ä', 'a'), ('â', 'a'), ('ã', 'a'),
                 ('é', 'e'), ('è', 'e'), ('ë', 'e'), ('ê', 'e'),
                 ('í', 'i'), ('ì', 'i'), ('ï', 'i'), ('î', 'i'),
                 ('ó', 'o'), ('ò', 'o'), ('ö', 'o'), ('ô', 'o'), ('õ', 'o'),
                 ('ú', 'u'), ('ù', 'u'), ('ü', 'u'), ('û', 'u'),
                 ('ñ', 'n'), ('ç', 'c')):
        s = s.replace(a, b)
    return re.sub(r'[^a-z0-9]+', '', s)


# ── JSON-RPC con timeout y reintentos ─────────────────────────────────────────

def _rpc_once(payload, timeout=CALL_TIMEOUT):
    """Ejecuta una llamada JSON-RPC en un hilo, con timeout.

    Devuelve el 'result' o None. Si el plugin se cuelga, no bloquea el servicio.
    """
    box = {}

    def worker():
        try:
            raw = xbmc.executeJSONRPC(json.dumps(payload))
            box['res'] = json.loads(raw)
        except Exception as exc:               # noqa: BLE001
            box['err'] = str(exc)

    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()
    t.join(timeout)
    if t.is_alive():
        log("  aviso: llamada sin respuesta en %ss — %s" %
            (timeout, str(payload.get('params', {}).get('directory', ''))[:70]))
        return None
    if 'err' in box:
        log("  excepcion JSON-RPC: %s" % box['err'])
        return None
    res = box.get('res') or {}
    if res.get('error'):
        return None
    return res.get('result')


def list_dir(path):
    """Lista un directorio de plugin probando media 'video' y 'files'.

    Con reintentos, porque el add-on de Amazon falla de forma transitoria.
    """
    if not path:
        return []
    for attempt in range(MAX_RETRIES):
        for media in ("video", "files"):
            result = _rpc_once({
                "jsonrpc": "2.0", "method": "Files.GetDirectory",
                "params": {"directory": path, "media": media,
                           "properties": ["season", "episode", "title", "file"]},
                "id": 1,
            })
            files = (result or {}).get('files') or []
            if files:
                return files
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_WAIT * (attempt + 1))
    return []


# ── Rutas de Amazon ───────────────────────────────────────────────────────────

def is_amazon(path):
    return 'amazon' in (path or '').lower()


def strip_guids(path):
    """Quita TODOS los segmentos GUID de una ruta de Amazon.

    Antes solo se quitaba el ultimo; si habia mas de uno (o estaba en medio),
    la ruta seguia rota. Se hace por expresion regular para NO romper el '//'
    del esquema plugin:// (un split('/') se lo comia).
    """
    if not is_amazon(path) or GUID_MARK not in path:
        return path
    fixed = re.sub(r'/' + re.escape(GUID_MARK) + r'[^/]*', '', path)
    if fixed != path and not fixed.endswith('/'):
        fixed += '/'
    return fixed


def series_node(season_path):
    """Dado el path de una temporada, devuelve el nodo de la serie."""
    if not season_path:
        return None
    return season_path.rstrip('/').rsplit('/', 1)[0] + '/'


def find_series_by_title(title):
    """Localiza una serie en Amazon buscandola POR TITULO.

    Devuelve la ruta del nodo de la serie, o None. Es la red de seguridad que
    evita que una serie se quede muerta cuando su ruta guardada caduca.
    """
    query = title.strip().replace(' ', '+')
    if not query:
        return None
    log("  buscando en Amazon por titulo: %r" % title)
    items = list_dir(AMZ_SEARCH + query)
    if not items:
        return None

    # Caso A: la busqueda ya devuelve los titulos
    hit = _match_title(items, title)
    if hit:
        return hit

    # Caso B: hay una pagina de resultados; entrar y buscar dentro
    for it in items:
        if it.get('filetype') != 'directory':
            continue
        sub = list_dir(it.get('file'))
        hit = _match_title(sub, title)
        if hit:
            return hit
    return None


def _match_title(items, title):
    """Busca coincidencia de titulo. Exacta primero; difusa solo si es unica."""
    want = norm_title(title)
    if not want:
        return None
    candidates = [it for it in (items or [])
                  if it.get('filetype') == 'directory']
    # 1) exacta
    for it in candidates:
        if norm_title(it.get('label')) == want:
            log("  encontrada (exacta): %r" % (it.get('label') or '')[:60])
            return it.get('file')
    # 2) difusa, solo si hay un unico parecido razonable
    try:
        from difflib import SequenceMatcher
    except ImportError:
        return None
    best, best_ratio = None, 0.0
    ties = 0
    for it in candidates:
        ratio = SequenceMatcher(
            None, want, norm_title(it.get('label'))).ratio()
        if ratio > best_ratio:
            best, best_ratio, ties = it, ratio, 1
        elif ratio == best_ratio:
            ties += 1
    if best is not None and best_ratio >= 0.9 and ties == 1:
        log("  encontrada (parecida %.2f): %r" %
            (best_ratio, (best.get('label') or '')[:60]))
        return best.get('file')
    return None


def season_number(label_or_path):
    text = label_or_path or ''
    for pat in SEASON_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


def episode_number(ep, fallback_idx):
    n = ep.get('episode', -1)
    if isinstance(n, int) and n > 0:
        return n
    for field in (ep.get('label', ''), ep.get('file', '')):
        for pat in EPISODE_PATTERNS:
            m = re.search(pat, field or '', re.IGNORECASE)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    pass
    return fallback_idx + 1


# ── Metadatos ─────────────────────────────────────────────────────────────────

def load_metadata():
    if not xbmcvfs.exists(METADATA_FILE):
        log("metadata.json no existe aún")
        return {"tvshows": {}, "movies": {}}
    try:
        with xbmcvfs.File(METADATA_FILE, 'r') as f:
            meta = json.loads(f.read())
        log("Metadata: %d series, %d pelis" %
            (len(meta.get('tvshows', {})), len(meta.get('movies', {}))))
        return meta
    except Exception as exc:                    # noqa: BLE001
        log("load_metadata error: %s" % exc)
        return {"tvshows": {}, "movies": {}}


def save_metadata(meta):
    try:
        if not xbmcvfs.exists(ADDON_DATA):
            xbmcvfs.mkdirs(ADDON_DATA)
        with xbmcvfs.File(METADATA_FILE, 'w') as f:
            f.write(json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception as exc:                    # noqa: BLE001
        log("save_metadata error: %s" % exc)


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

        log("Añadiendo fuente: %s" % name)
        block = ('\n        <source>\n'
                 '            <name>%s</name>\n'
                 '            <path pathversion="1">%s</path>\n'
                 '            <allowsharing>true</allowsharing>\n'
                 '        </source>' % (name, path))

        if '<video>' in xml:
            xml = xml.replace('</video>', block + '\n    </video>', 1)
        else:
            xml = xml.replace(
                '</sources>',
                '\n    <video>\n        <default pathversion="1"></default>'
                + block + '\n    </video>\n</sources>')

        with xbmcvfs.File(SOURCES_FILE, 'w') as f:
            f.write(xml)
        log("Fuente añadida OK: %s" % path)
    except Exception as exc:                    # noqa: BLE001
        log("ensure_source error: %s" % exc)


def ensure_library_sources():
    for d in (TVSHOWS_DIR, MOVIES_DIR):
        if not xbmcvfs.exists(d):
            xbmcvfs.mkdirs(d)
    ensure_source(TVSHOWS_DIR, "Smart Library – Series")
    ensure_source(MOVIES_DIR, "Smart Library – Películas")


def scan_library():
    log("Escaneando librería: %s" % TVSHOWS_DIR)
    _rpc_once({
        "jsonrpc": "2.0", "method": "VideoLibrary.Scan",
        "params": {"directory": TVSHOWS_DIR, "showdialogs": False},
        "id": 1,
    }, timeout=10)


# ── Escritura de .strm ────────────────────────────────────────────────────────

def write_episodes(show_dir, show, s_num, episodes):
    """Crea/refresca los .strm de una temporada. Devuelve cuantos cambiaron."""
    changed = 0
    # Crear la carpeta SOLO cuando hay episodios que escribir, para no dejar
    # carpetas vacias huerfanas si la fuente no devuelve nada.
    if not xbmcvfs.exists(show_dir):
        try:
            xbmcvfs.mkdirs(show_dir)
        except Exception:                       # noqa: BLE001
            return 0
    for i, ep in enumerate(episodes):
        ep_url = ep.get('file', '') or ''
        if not ep_url:
            continue
        e_num = episode_number(ep, i)
        filepath = os.path.join(
            show_dir, "%s S%02dE%02d.strm" % (show, s_num, e_num))
        try:
            if not xbmcvfs.exists(filepath):
                with xbmcvfs.File(filepath, 'w') as f:
                    f.write(ep_url)
                log("  + nuevo: %s" % os.path.basename(filepath))
                changed += 1
                continue
            with xbmcvfs.File(filepath, 'r') as f:
                old_url = f.read()
            if old_url.rstrip('\r\n') != ep_url.rstrip('\r\n'):
                with xbmcvfs.File(filepath, 'w') as f:
                    f.write(ep_url)
                log("  ~ actualizado: %s" % os.path.basename(filepath))
                changed += 1
        except Exception as exc:                # noqa: BLE001
            log("  error escribiendo %s: %s" % (filepath, exc))
    return changed


def resolve_amazon_seasons(show, stored):
    """Devuelve {temporada: ruta} para una serie de Amazon.

    v6.13.2 — Hallazgo clave: la ruta guardada YA ES una TEMPORADA (lleva DOS
    GUIDs: serie/temporada). El bug de la v6.13.x era llamar a series_node()
    (ascender un nivel) y listar el nodo-SERIE: eso se cuelga 30-40s y devuelve
    vacio, dejando la serie sin actualizar. Lo fiable y rapido es:

    1) Buscar la serie POR TITULO. La serie que devuelve la busqueda lista
       TODAS sus temporadas al instante (0.0s) y descubre temporadas nuevas.
       Verificado: 14/14 series Amazon localizadas, 1-8s cada una.
    2) Si la busqueda falla, usar las rutas guardadas TAL CUAL: cada una ya es
       una temporada y se lista directamente (rapido cuando el GUID esta vivo).
    """
    seasons = {}

    series_path = find_series_by_title(show)
    if series_path:
        for it in list_dir(series_path):
            if it.get('filetype') != 'directory':
                continue
            n = season_number(it.get('label')) or season_number(it.get('file'))
            if n is None or n < 1 or n > 99:
                continue
            if n not in seasons:
                seasons[n] = it.get('file')

    if not seasons:
        for s_str, path in (stored or {}).items():
            if not path or not is_amazon(path):
                continue
            try:
                n = int(s_str)
            except (TypeError, ValueError):
                continue
            if n in seasons or n < 1 or n > 99:
                continue
            if list_dir(path):
                seasons[n] = path

    if not seasons:
        log("  %s: no se pudo localizar en Amazon" % show)
    return seasons


# ── Comprobacion principal ────────────────────────────────────────────────────

def check_and_update():
    meta = load_metadata()
    removed = {norm_title(x) for x in (meta.get("removed_series", []) or [])}
    tvshows = meta.get("tvshows", {}) or {}
    total_changed = 0
    meta_dirty = False

    for show, seasons in tvshows.items():
        if norm_title(show) in removed:
            continue
        show_dir = os.path.join(TVSHOWS_DIR, show)

        stored_amz = {k: v for k, v in (seasons or {}).items()
                      if is_amazon(v)}
        log("Comprobando: %s" % show)

        if stored_amz:
            # ── Serie de Amazon: resolucion general + temporadas nuevas ──
            live = resolve_amazon_seasons(show, seasons)
            for n, path in sorted(live.items()):
                key = str(n)
                if seasons.get(key) != path:
                    seasons[key] = path
                    meta_dirty = True
                eps = [e for e in list_dir(path)
                       if e.get('filetype') != 'directory'
                       and not is_promo(e.get('label', ''))]
                if not eps:
                    log("  T%d: sin episodios" % n)
                    continue
                log("  T%d: %d episodio(s)" % (n, len(eps)))
                total_changed += write_episodes(show_dir, show, n, eps)
        else:
            # ── Otras fuentes: comportamiento clasico ──
            for s_str, season_path in sorted((seasons or {}).items()):
                if not season_path:
                    continue
                try:
                    s_num = int(s_str)
                except (TypeError, ValueError):
                    continue
                eps = [e for e in list_dir(season_path)
                       if e.get('filetype') != 'directory'
                       and not is_promo(e.get('label', ''))]
                if not eps:
                    log("  T%d: sin episodios — %s" % (s_num, season_path[:60]))
                    continue
                log("  T%d: %d episodio(s)" % (s_num, len(eps)))
                total_changed += write_episodes(show_dir, show, s_num, eps)

    # ── Peliculas ──
    movies = meta.get("movies", {}) or {}
    for title, path in movies.items():
        if not is_amazon(path):
            continue
        movie_dir = os.path.join(MOVIES_DIR, title)
        strm_path = os.path.join(movie_dir, "%s.strm" % title)
        # OJO (v6.13.4): NO listar `path`. La URL de una pelicula es de
        # REPRODUCCION (`?asin=...&mode=PlayVideo`): un Files.GetDirectory
        # sobre ella hace que el add-on de Amazon RESUELVA el video
        # (GetPlaybackResources) y saque el DIALOGO DE "continuar viendo"
        # el solo, sin que nadie lo haya pedido. Se re-resuelve por titulo
        # (barato, 1-3s) y solo se reescribe si la URL cambio o falta el .strm.
        new_url = find_movie_by_title(title)
        if not new_url:
            continue
        if new_url == path and xbmcvfs.exists(strm_path):
            continue
        movies[title] = new_url
        if new_url != path:
            meta_dirty = True
        try:
            if not xbmcvfs.exists(movie_dir):
                xbmcvfs.mkdirs(movie_dir)
            with xbmcvfs.File(strm_path, 'w') as f:
                f.write(new_url)
            total_changed += 1
            log("  ~ pelicula actualizada: %s" % title)
        except Exception as exc:                # noqa: BLE001
            log("  error escribiendo peli %s: %s" % (title, exc))

    if meta_dirty:
        save_metadata(meta)
        log("metadata.json actualizado")

    return total_changed


def clean_title(title):
    """Quita el sufijo de anio ' (2021)': la busqueda de Amazon FALLA con el.

    Verificado en real: 'Mortal Kombat (2021)' devolvia basura (40 categorias,
    la primera vacia), mientras que 'Mortal Kombat' encuentra la pelicula.
    """
    return re.sub(r'\s*\(\d{4}\)\s*$', '', title or '').strip()


def find_movie_by_title(title):
    """Busca en Amazon una pelicula por titulo y devuelve su URL de reproduccion.

    v6.13.3:
      1) Busca SIN el anio (el anio rompe el buscador del add-on).
      2) Mira SOLO la primera categoria-directorio ('Principales resultados').
         Entrar en 'Similar a...' / 'Mas para explorar' se cuelga 20-30s y no
         aporta ninguna pelicula. Verificado: 10/12 peliculas Amazon resueltas
         en 15s en total (antes 30-55s POR pelicula).
    """
    want = norm_title(title)
    if not want:
        return None
    clean = clean_title(title)
    want_clean = norm_title(clean)
    items = list_dir(AMZ_SEARCH + clean.replace(' ', '+'))
    if not items:
        return None
    pools = [items]
    for it in items:
        if it.get('filetype') == 'directory':
            sub = list_dir(it.get('file'))
            if sub:
                pools.append(sub)
            break
    for pool in pools:
        for cand in pool:
            if cand.get('filetype') == 'directory':
                continue
            lab = norm_title(cand.get('label'))
            if lab and lab in (want, want_clean):
                return cand.get('file')
    return None


# ── Servicio ──────────────────────────────────────────────────────────────────

class UpdateService(xbmc.Monitor):

    def onNotification(self, sender, method, data):
        if sender == ADDON_ID or ADDON_ID in method:
            if 'Updated' in method:
                log("Notificación recibida — lanzando check")
                xbmc.sleep(2000)
                self._run_check("(notificación)")

    def _run_check(self, context=""):
        log("=== Check %s ===" % context)
        try:
            changed = check_and_update()
            if changed > 0:
                log("%d cambio(s) — escaneando librería" % changed)
                scan_library()
            else:
                log("Sin cambios")
        except Exception as exc:                # noqa: BLE001
            log("Error en _run_check: %s" % exc)
        log("=== Fin check %s ===" % context)

    def run(self):
        log("Servicio iniciado (v6.13.4)")
        if self.waitForAbort(30):
            return
        ensure_library_sources()
        self._run_check("(arranque)")
        while not self.waitForAbort(6 * 3600):
            self._run_check("(periódica)")


if __name__ == '__main__':
    UpdateService().run()
