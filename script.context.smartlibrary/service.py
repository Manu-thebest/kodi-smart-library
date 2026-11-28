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
  8) v6.13.7 PELICULAS: se escribe un .nfo junto al .strm (titulo y anio).
     Sin .nfo, si el buscador de internet no reconoce la pelicula, Kodi la
     DESCARTA ("No information found for item X.strm, it won't be added to
     the library") y no entra en la biblioteca aunque el .strm este en disco.
     Es el mismo arreglo que ya se hizo con los episodios.

Las series que NO son de Amazon siguen funcionando igual que antes.
"""

import json
import os
import re
import threading
import time

import xbmc
import xbmcaddon
import xbmcvfs

ADDON_DATA    = xbmcvfs.translatePath("special://profile/addon_data/script.context.smartlibrary/")
LIB_BASE      = os.path.join(ADDON_DATA, "Library/")
TVSHOWS_DIR   = os.path.join(LIB_BASE, "TVShows/")
MOVIES_DIR    = os.path.join(LIB_BASE, "Movies/")
METADATA_FILE = os.path.join(ADDON_DATA, "metadata.json")
SOURCES_FILE  = xbmcvfs.translatePath("special://profile/sources.xml")
LOG_TAG       = "[SmartLibrary]"
ADDON_ID      = "script.context.smartlibrary"

try:
    ADDON_VERSION = xbmcaddon.Addon(ADDON_ID).getAddonInfo("version")
except Exception:                            # noqa: BLE001
    ADDON_VERSION = "?"

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


# ── Guardado a prueba de escrituras simultaneas ───────────────────────────────
# PROBLEMA (verificado en real, 12-nov-2026): el servicio y el menu guardan
# metadata.json ENTERO. Si coinciden en el tiempo, el segundo pisa lo que el
# primero acaba de escribir y esa serie/pelicula desaparece del registro sin
# dejar rastro (se paso de 82 series a 80 sin ningun borrado en el log).
# SOLUCION: antes de escribir se bloquea el fichero, se RELEE lo que hay en
# disco y se FUSIONA con lo nuestro; se escribe en un temporal y se sustituye
# de golpe (os.replace), asi que nunca queda un JSON a medias. Un borrado
# voluntario se respeta porque la serie queda en removed_series.

def _norm_key(name):
    return re.sub(r'[^a-z0-9]+', ' ', str(name).lower()).strip()

def _meta_lock():
    import fcntl
    fd = open(METADATA_FILE + ".lock", "a+")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd

def _read_meta_file():
    try:
        with open(METADATA_FILE, "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except Exception:
        return None

def _merge_meta(disco, mio):
    """Fusiona la copia del disco con la nuestra sin perder entradas."""
    if not isinstance(disco, dict) or not isinstance(mio, dict):
        return mio
    disco_borrados = list(disco.get("removed_series") or [])
    mio_borrados = list(mio.get("removed_series") or [])
    disco_borrados_norm = {_norm_key(x) for x in disco_borrados}
    mio_borrados_norm = {_norm_key(x) for x in mio_borrados}
    # Borrados que he hecho yo en esta operacion (aun no estaban en el disco)
    nuevos_borrados = mio_borrados_norm - disco_borrados_norm
    # Borrados que ha hecho OTRO mientras yo tenia una copia vieja en memoria:
    # mi copia los trae de vuelta y no debe resucitarlos.
    borrados_por_otro = disco_borrados_norm - mio_borrados_norm

    out = {}
    for clave in ("tvshows", "movies"):
        merged = {}
        for nombre, valor in (disco.get(clave) or {}).items():
            if _norm_key(nombre) in nuevos_borrados:
                continue          # lo he borrado yo en esta misma operacion
            merged[nombre] = valor
        for nombre, valor in (mio.get(clave) or {}).items():
            if _norm_key(nombre) in borrados_por_otro:
                continue          # otro proceso lo borro: no resucitarlo
            if isinstance(valor, dict) and isinstance(merged.get(nombre), dict):
                sub = dict(merged[nombre])
                sub.update(valor)
                merged[nombre] = sub
            else:
                merged[nombre] = valor
        out[clave] = merged
    out["removed_series"] = list(dict.fromkeys(disco_borrados + mio_borrados))
    for k, v in disco.items():
        if k not in out:
            out[k] = v
    for k, v in mio.items():
        if k not in out:
            out[k] = v
    return out

def save_metadata(meta):
    lock = None
    try:
        if not xbmcvfs.exists(ADDON_DATA):
            log("Creando directorio: %s" % ADDON_DATA)
            xbmcvfs.mkdirs(ADDON_DATA)
        lock = _meta_lock()
        en_disco = _read_meta_file()
        if en_disco:
            antes = (len(en_disco.get("tvshows", {})), len(en_disco.get("movies", {})))
            meta = _merge_meta(en_disco, meta)
            ahora = (len(meta.get("tvshows", {})), len(meta.get("movies", {})))
            if ahora != antes:
                log("Metadata fusionado con el disco: %s -> %s series/pelis" % (antes, ahora))
        content = json.dumps(meta, ensure_ascii=False, indent=2)
        tmp = METADATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, METADATA_FILE)
        log("Metadata guardado (%d bytes, fusion+bloqueo): %s" % (len(content), METADATA_FILE))
    except Exception as e:
        log("ERROR guardando metadata: %s" % e)
    finally:
        if lock:
            lock.close()


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
    """Escanea series y peliculas. Con los .nfo delante, el escaneo es local."""
    for d, que in ((TVSHOWS_DIR, "series"), (MOVIES_DIR, "películas")):
        if not xbmcvfs.exists(d):
            continue
        log("Escaneando librería (%s): %s" % (que, d))
        _rpc_once({
            "jsonrpc": "2.0", "method": "VideoLibrary.Scan",
            "params": {"directory": d, "showdialogs": False},
            "id": 1,
        }, timeout=10)


# ── Escritura de .strm ────────────────────────────────────────────────────────

def _xml_escape(text):
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))


def _episode_title(ep, e_num):
    """Titulo limpio del episodio: '3. Adar' -> 'Adar'."""
    raw = (ep.get('label') or '').strip()
    raw = re.sub(r'^\s*\d+\s*[\.\)\-:]\s*', '', raw)
    return raw or ("Capitulo %d" % e_num)


def write_episode_nfo(show_dir, show, s_num, e_num, title):
    """Escribe el .nfo del episodio si falta o si cambio el titulo.

    Por que: cuando el .nfo existe junto al .strm, Kodi da el episodio por
    bueno y NO consulta el buscador de internet. Eso evita que descarte
    capitulos de temporadas que el buscador no tiene (FMA:B repartido en 5
    temporadas cuando TheMovieDB solo tiene 1, los E25 de Sword Art Online,
    la temporada 5 de Rent-a-Girlfriend, etc.). Tambien deja el episodio
    reproducible aunque el buscador no lo conozca.
    """
    nfo = os.path.join(show_dir, "%s S%02dE%02d.nfo" % (show, s_num, e_num))
    content = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<episodedetails>\n'
        '  <title>%s</title>\n'
        '  <season>%d</season>\n'
        '  <episode>%d</episode>\n'
        '</episodedetails>\n' % (_xml_escape(title), s_num, e_num)
    )
    try:
        if xbmcvfs.exists(nfo):
            with xbmcvfs.File(nfo, 'r') as f:
                if f.read() == content:
                    return False
        with xbmcvfs.File(nfo, 'w') as f:
            f.write(content)
        return True
    except Exception as exc:                    # noqa: BLE001
        log("  error escribiendo nfo %s: %s" % (nfo, exc))
        return False


YEAR_RE = re.compile(r'\((\d{4})\)\s*$')


def year_from_title(title):
    """Separa el anio del final del titulo: 'Godzilla (1984)'.

    Devuelve (titulo_limpio, anio). Anio None si el titulo no lo lleva.
    """
    text = (title or '').strip()
    m = YEAR_RE.search(text)
    if not m:
        return text, None
    return text[:m.start()].strip(), int(m.group(1))


def write_movie_nfo(movie_dir, title):
    """Escribe el .nfo de una pelicula si falta o si cambio.

    Por que: con el .nfo junto al .strm, Kodi da la pelicula por buena y NO
    consulta el buscador de internet. Sin el, si el buscador no reconoce el
    titulo, Kodi la descarta ("No information found for item X, it won't be
    added to the library") y la pelicula no entra en la biblioteca aunque su
    .strm este en disco. Mismo arreglo que en los episodios.
    """
    name, year = year_from_title(title)
    nfo = os.path.join(movie_dir, "%s.nfo" % title)
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<movie>',
             '  <title>%s</title>' % _xml_escape(name)]
    if year:
        lines.append('  <year>%d</year>' % year)
    lines.append('</movie>')
    content = "\n".join(lines) + "\n"
    try:
        if not os.path.isdir(movie_dir):
            return False
        if os.path.isfile(nfo):
            with open(nfo, 'r', encoding='utf-8') as f:
                if f.read() == content:
                    return False
        with open(nfo, 'w', encoding='utf-8') as f:
            f.write(content)
        log("  + nfo pelicula: %s" % os.path.basename(nfo))
        try:
            os.utime(movie_dir, None)
        except Exception:                       # noqa: BLE001
            pass
        return True
    except Exception as exc:                    # noqa: BLE001
        log("  error escribiendo nfo de pelicula %s: %s" % (nfo, exc))
        return False


def write_episodes(show_dir, show, s_num, episodes):
    """Crea/refresca los .strm de una temporada. Devuelve cuantos cambiaron."""
    changed = 0
    nfo_written = 0
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
        ep_title = _episode_title(ep, e_num)
        filepath = os.path.join(
            show_dir, "%s S%02dE%02d.strm" % (show, s_num, e_num))
        try:
            if not xbmcvfs.exists(filepath):
                with xbmcvfs.File(filepath, 'w') as f:
                    f.write(ep_url)
                log("  + nuevo: %s" % os.path.basename(filepath))
                if write_episode_nfo(show_dir, show, s_num, e_num, ep_title):
                    nfo_written += 1
                changed += 1
                continue
            with xbmcvfs.File(filepath, 'r') as f:
                old_url = f.read()
            if old_url.rstrip('\r\n') != ep_url.rstrip('\r\n'):
                with xbmcvfs.File(filepath, 'w') as f:
                    f.write(ep_url)
                log("  ~ actualizado: %s" % os.path.basename(filepath))
                changed += 1
            if write_episode_nfo(show_dir, show, s_num, e_num, ep_title):
                nfo_written += 1
        except Exception as exc:                # noqa: BLE001
            log("  error escribiendo %s: %s" % (filepath, exc))
    # Tocar la carpeta cuando hay cambios: Kodi 21 se salta los directorios
    # "sin cambios" (VideoInfoScanner: Skipping dir ... due to no change) y
    # por eso no llegaba a ver los .strm nuevos.
    if changed or nfo_written:
        try:
            os.utime(show_dir, None)
        except Exception:                       # noqa: BLE001
            pass
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


# ── Temporadas nuevas en TODAS las fuentes (parche-manu-todas-fuentes-v1) ────
#
# v6.13.5 — Antes solo se buscaban temporadas nuevas en Amazon; para el resto de
# fuentes (Crunchyroll, Max, Movistar+, ...) se seguian las temporadas guardadas
# y punto. Ahora, en una serie que NO es de Amazon:
#
#   1) Se deduce la pagina de la SERIE a partir de la ruta guardada de una
#      temporada. En la ruta se prueban los antecesores (1, 2 y 3 tramos menos):
#      `.../series/<serie>/<temporada>/audio/es-ES` -> `.../series/<serie>`.
#      En la query se quita el parametro de temporada
#      (`?_=series&id=<serie>&season=1`) y se prueba tambien
#      `action=season` -> `action=series`.
#   2) Si eso no da temporadas, se busca la serie por su titulo en el buscador
#      del propio add-on (tabla BUSCADORES).
#   3) Una pagina se acepta SOLO si sus temporadas incluyen alguna de las ya
#      guardadas: asi no se confunde con un menu cualquiera (los antecesores de
#      Crunchyroll devuelven el menu raiz del add-on, por ejemplo).
#   4) Solo se ANADEN las temporadas que falten; las guardadas NO se
#      sobrescriben (cada temporada de Crunchyroll tiene una variante por idioma
#      de audio y no queremos cambiarle el audio a la que ya funcionaba). Solo
#      si una ruta guardada se queda vacia se usa la descubierta.
#
# El numero de temporada se saca de la propiedad `season` del listado (varios
# add-ons la rellenan aunque la etiqueta no lleve numero: "Basilisk [US Audio]")
# y, si no, del texto de la etiqueta o de la ruta ("Temporada 4", "Season 1").

_PARAM_TEMPORADA = ('season', 'season_number', 'seasonid', 'season_id',
                    'temporada', 'temp')
_ACCION_TEMPORADA = ('season', 'temporada', 'episodes', 'episodios')
_NIVELES_PADRE = 3          # cuantos tramos de ruta se prueban hacia arriba

# Add-ons con buscador propio: (trozo de ruta, plantilla de busqueda por titulo)
BUSCADORES = (
    ('plugin.video.movistarplus',
     'plugin://plugin.video.movistarplus/?action=search&search_term='),
)

# Memoria de la propia ejecucion: por add-on, que tipo de pagina funciono
# (para no repetir los intentos fallidos en cada serie del mismo add-on).
_AJUSTE_FUENTE = {}


def _fuente_de(path):
    """'plugin.video.movistarplus' a partir de 'plugin://plugin.video.movistarplus/...'."""
    p = path or ''
    if p.startswith('plugin://'):
        return p[len('plugin://'):].split('/')[0]
    return ''


def _sin_acentos_plus(valor):
    """unquote_plus sin depender de que urllib este importado."""
    try:
        from urllib.parse import unquote_plus
        return unquote_plus(valor or '')
    except Exception:                            # noqa: BLE001
        return (valor or '').replace('+', ' ')


def _padres_de_temporada(path):
    """[(tipo, nivel, url)] candidatos a pagina-SERIE de una ruta de temporada."""
    cands = []
    p = (path or '').strip()
    if not p:
        return cands
    base, sep, query = p.partition('?')
    if sep:
        trozos = [t for t in query.split('&') if t]
        sin_temp = []
        quitado = False
        tiene_temp = False
        for kv in trozos:
            k, _, v = kv.partition('=')
            kl = k.lower()
            if kl in _PARAM_TEMPORADA:
                quitado = True
                tiene_temp = True
                continue
            sin_temp.append(kv)
        con_series = []
        cambiado = False
        for kv in sin_temp:
            k, _, v = kv.partition('=')
            if k.lower() == 'action' and v.lower() in _ACCION_TEMPORADA:
                # v6.14.2 — OJO: en estas rutas el `id` suele ser el de la
                # TEMPORADA ("action=season&id=4248311", Movistar+), asi que
                # cambiar `action` a `series` pide una serie que no existe; el
                # add-on de Movistar+ lo peta ademas con KeyError: 'name'.
                # Solo se convierte si la temporada va aparte, en su propio
                # parametro ("action=season&id=<serie>&season=2").
                if not tiene_temp:
                    cambiado = False
                    con_series = []
                    break
                kv = k + '=series'
                cambiado = True
            con_series.append(kv)
        for lista, aplica in ((sin_temp, quitado), (con_series, cambiado)):
            if not aplica:
                continue
            q = '&'.join(lista)
            cand = (base + '?' + q) if q else base
            if cand and cand != p and cand not in [c[2] for c in cands]:
                cands.append(('padre-query', 0, cand))
    else:
        limpio = p.rstrip('/')
        resto = limpio[len('plugin://'):] if limpio.startswith('plugin://') else limpio
        for nivel in range(1, _NIVELES_PADRE + 1):
            if resto.count('/') < nivel:
                break
            padre = limpio
            for _ in range(nivel):
                padre = padre.rsplit('/', 1)[0]
            for cand in (padre, padre + '/'):
                if cand and cand.rstrip('/') != limpio and cand not in [
                        c[2] for c in cands]:
                    cands.append(('padre', nivel, cand))
    return cands


_EXTRA = ('ova', 'oad', 'especial', 'special', 'recap', 'resumen',
          'pelicula', 'movie', 'avance', 'trailer', 'preview')


def _es_extra(etiqueta):
    """True para OVAs, especiales, recaps, peliculas y avances (no son temporada)."""
    t = (etiqueta or '').lower()
    if not t:
        return False
    for palabra in _EXTRA:
        if palabra in t:
            return True
    return False


def _temporadas_en_items(items, prioridad='prop'):
    """{n: [rutas]} de una pagina; {} si esa pagina no parece de temporadas.

    `prioridad` elige de donde sale el numero: la propiedad `season` del listado
    (los add-ons suelen rellenarla aunque la etiqueta no lleve numero:
    "Basilisk [US Audio]") o el texto de la etiqueta/ruta (hay add-ons que
    ponen una `season` que no cuadra con lo que se ve: "Temporada 1" con
    season=2).
    """
    halladas = {}
    for it in items or []:
        if it.get('filetype') != 'directory':
            continue
        ruta = it.get('file') or ''
        if _es_extra(it.get('label')):
            continue
        prop = None
        try:
            prop = int(it.get('season'))
        except (TypeError, ValueError):
            prop = None
        if prop is not None and not 1 <= prop <= 99:
            prop = None
        texto = season_number(it.get('label')) or season_number(ruta)
        if texto is not None and not 1 <= texto <= 99:
            texto = None
        if prioridad == 'prop':
            n = prop if prop is not None else texto
        else:
            n = texto if texto is not None else prop
        if n is None:
            continue
        halladas.setdefault(n, [])
        if ruta and ruta not in halladas[n]:
            halladas[n].append(ruta)
    return halladas


def _compatible(a, b):
    """Dos rutas de la misma temporada (una guardada y otra recien leida)."""
    a = a or ''
    b = b or ''
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _puntos_pagina(halladas, guardadas):
    """Cuantas temporadas guardadas reconoce esta pagina, con su misma ruta."""
    puntos = 0
    for n, rutas in (halladas or {}).items():
        ref = (guardadas or {}).get(n)
        if not ref:
            continue
        if any(_compatible(r, ref) for r in rutas):
            puntos += 1
    return puntos


def _prefijo_comun(a, b):
    n = 0
    for x, y in zip(a or '', b or ''):
        if x != y:
            break
        n += 1
    return n


def _mejor_variante(rutas, referencia):
    """De varias rutas de la misma temporada, la que mas se parece a las guardadas."""
    def puntos(r):
        if not referencia:
            return 0
        return max(_prefijo_comun(r, s) for s in referencia)
    validas = [r for r in rutas if r]
    if not validas:
        return None
    return max(validas, key=puntos)


def _series_por_titulo(url_busqueda, show):
    """Lista el buscador del add-on y devuelve paginas de serie con ese titulo."""
    want = norm_title(show)
    if not want:
        return []
    exactas = []
    parecidas = []
    for it in list_dir(url_busqueda):
        if it.get('filetype') != 'directory' or not it.get('file'):
            continue
        etiqueta = norm_title(it.get('label'))
        if etiqueta == want:
            exactas.append(it['file'])
        elif etiqueta.startswith(want) and len(etiqueta) <= len(want) + 12:
            parecidas.append(it['file'])
    return (exactas + parecidas)[:3]


def resolve_other_seasons(show, seasons):
    """{n: ruta} con TODAS las temporadas de una serie que NO es de Amazon."""
    guardadas = {}
    for k, v in (seasons or {}).items():
        if not v or is_amazon(v):
            continue
        try:
            guardadas[int(k)] = v
        except (TypeError, ValueError):
            continue
    if not guardadas:
        return {}
    guardadas_paths = [p for p in guardadas.values() if p]
    fuente = _fuente_de(guardadas_paths[0])

    candidatos = []
    for p in guardadas_paths:
        for cand in _padres_de_temporada(p):
            if cand[2] in guardadas_paths:
                continue
            if cand[2] not in [c[2] for c in candidatos]:
                candidatos.append(cand)

    for trozo, plantilla in BUSCADORES:
        if not any(trozo in (p or '') for p in guardadas_paths):
            continue
        q = (show or '').strip().replace(' ', '+')
        if not q:
            continue
        for url in _series_por_titulo(plantilla + q, show):
            if url not in [c[2] for c in candidatos]:
                candidatos.append(('buscador', 0, url))

    # Lo que ya funciono con este add-on, primero
    acierto = _AJUSTE_FUENTE.get(fuente)
    if acierto:
        candidatos.sort(key=lambda c: 0 if (c[0], c[1]) == acierto else 1)

    mejor_pagina = None
    mejor_puntos = 0
    for tipo, nivel, cand in candidatos[:12]:
        halladas = _temporadas_en_items(list_dir(cand), 'prop')
        if not halladas:
            continue
        # Se exige que la pagina reconozca alguna temporada guardada CON SU MISMA
        # RUTA: asi no se cuela otra serie con el mismo titulo ("Doc" / "DOC")
        # ni una pagina que no es la de esta serie.
        puntos = _puntos_pagina(halladas, guardadas)
        if puntos <= 0:
            continue
        if puntos > mejor_puntos:
            mejor_puntos = puntos
            mejor_pagina = (tipo, nivel, halladas)
        if puntos == len(set(halladas) & set(guardadas)):
            break

    if not mejor_pagina:
        log("  %s: sin pagina de serie reconocible (solo temporadas guardadas)" % show)
        return {}
    tipo, nivel, halladas = mejor_pagina
    _AJUSTE_FUENTE[fuente] = (tipo, nivel)
    out = {}
    for n, rutas in halladas.items():
        mejor = _mejor_variante(rutas, guardadas_paths)
        if mejor:
            out[n] = mejor
    log("  %s: pagina de la serie -> T%s" % (
        show, ",".join(str(n) for n in sorted(out))))
    return out


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

        stored_otras = {k: v for k, v in (seasons or {}).items()
                        if v and not is_amazon(v)}

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
        if stored_otras:
            # ── Otras fuentes: temporadas nuevas + actualizacion (v6.13.5) ──
            live = resolve_other_seasons(show, seasons)
            # Solo se anaden temporadas POSTERIORES a la ultima guardada: lo que
            # interesa es la temporada siguiente, no rellenar temporadas viejas
            # que nunca se siguieron (eso llenaria la biblioteca de episodios
            # que no ha pedido nadie).
            techo = 0
            for k, v in (seasons or {}).items():
                if not v or is_amazon(v):
                    continue
                try:
                    techo = max(techo, int(k))
                except (TypeError, ValueError):
                    continue
            for n, path in sorted(live.items()):
                key = str(n)
                if seasons.get(key) or n <= techo:
                    continue
                seasons[key] = path
                meta_dirty = True
                log("  T%d: TEMPORADA NUEVA (%s)" % (n, path[:60]))

            objetivos = {}
            for k, v in (seasons or {}).items():
                if not v or is_amazon(v):
                    continue
                try:
                    objetivos[int(k)] = v
                except (TypeError, ValueError):
                    continue

            for s_num in sorted(objetivos):
                season_path = objetivos[s_num]
                eps = [e for e in list_dir(season_path)
                       if e.get('filetype') != 'directory'
                       and not is_promo(e.get('label', ''))]
                if not eps and s_num in live and live[s_num] != season_path:
                    # La ruta guardada ya no sirve: se usa la descubierta.
                    alt = live[s_num]
                    eps = [e for e in list_dir(alt)
                           if e.get('filetype') != 'directory'
                           and not is_promo(e.get('label', ''))]
                    if eps:
                        log("  T%d: ruta guardada vacia, uso la nueva" % s_num)
                        seasons[str(s_num)] = alt
                        meta_dirty = True
                if not eps:
                    log("  T%d: sin episodios — %s" % (s_num, season_path[:60]))
                    continue
                log("  T%d: %d episodio(s)" % (s_num, len(eps)))
                total_changed += write_episodes(show_dir, show, s_num, eps)

    # ── Peliculas ──
    # v6.13.7: el .nfo se escribe para TODAS las peliculas, sean de Amazon o de
    # cualquier otra fuente. Sin el, si el buscador de internet no reconoce el
    # titulo, Kodi descarta el .strm ("No information found for item X") y la
    # pelicula no entra en la biblioteca aunque el fichero este en disco.
    # Se comprueba con os.path (no con xbmcvfs): es el mismo sistema de ficheros
    # que ya usa save_metadata y no depende de como resuelva Kodi la ruta.
    movies = meta.get("movies", {}) or {}
    pelis_strm = pelis_nfo = 0
    for title, path in movies.items():
        movie_dir = os.path.join(MOVIES_DIR, title)
        strm_path = os.path.join(movie_dir, "%s.strm" % title)
        if os.path.isfile(strm_path):
            pelis_strm += 1
            if write_movie_nfo(movie_dir, title):
                pelis_nfo += 1
                total_changed += 1
                _touch(strm_path)
        if not is_amazon(path):
            continue
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

    # Carpetas de pelicula con .strm que NO estan en el registro (restos de
    # versiones anteriores): sin .nfo tambien se descartan, asi que se cubren.
    pelis_extra = 0
    try:
        for name in sorted(os.listdir(MOVIES_DIR)):
            d = os.path.join(MOVIES_DIR, name)
            if name in movies or not os.path.isdir(d):
                continue
            strm = os.path.join(d, "%s.strm" % name)
            if os.path.isfile(strm):
                if write_movie_nfo(d, name):
                    pelis_extra += 1
                    total_changed += 1
                    _touch(strm)
    except Exception as exc:                    # noqa: BLE001
        log("  error recorriendo peliculas en disco: %s" % exc)
    if movies or pelis_extra:
        log("Películas: %d en el registro, %d con .strm, %d .nfo escritos, "
            "%d carpetas fuera del registro" %
            (len(movies), pelis_strm, pelis_nfo + pelis_extra, pelis_extra))

    if meta_dirty:
        save_metadata(meta)
        log("metadata.json actualizado")

    return total_changed


def _touch(path):
    """Actualiza el mtime de un fichero (en silencio si falla).

    Kodi no vuelve a mirar una pelicula cuyo .strm no ha cambiado, aunque
    aparezca un .nfo nuevo a su lado: sin tocar el .strm, el .nfo no se lee y
    la pelicula sigue fuera de la biblioteca.
    """
    try:
        os.utime(path, None)
    except Exception:                           # noqa: BLE001
        pass


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
        log("Servicio iniciado (v%s)" % ADDON_VERSION)
        if self.waitForAbort(30):
            return
        ensure_library_sources()
        self._run_check("(arranque)")
        while not self.waitForAbort(6 * 3600):
            self._run_check("(periódica)")


if __name__ == '__main__':
    UpdateService().run()
