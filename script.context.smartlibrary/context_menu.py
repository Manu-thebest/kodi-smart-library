import sys, xbmc, xbmcgui, xbmcvfs, os, json, re

ADDON_DATA   = xbmcvfs.translatePath("special://profile/addon_data/script.context.smartlibrary/")
LIB_BASE     = os.path.join(ADDON_DATA, "Library/")
TVSHOWS_DIR  = os.path.join(LIB_BASE, "TVShows/")
MOVIES_DIR   = os.path.join(LIB_BASE, "Movies/")
METADATA_FILE = os.path.join(ADDON_DATA, "metadata.json")
LOG_TAG      = "[SmartLibrary]"
ADDON_ID     = "script.context.smartlibrary"

SEASON_PATTERNS = [
    r'(\d+)\s*[aªoº°]?\s*(?:temporada|season|temp)\b',
    r'(?:temporada|season|temp)\s*(\d+)',
    r'(?<![A-Za-z])T(\d{1,2})(?!\d)',
    r'(?<![A-Za-z])S(\d{1,2})(?!\d)',
    r'\b(\d{1,2})[xX]\d{2}\b',
]
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


# ── Utilidades ────────────────────────────────────────────────────────────────

def log(msg):
    xbmc.log(f"{LOG_TAG} {msg}", xbmc.LOGINFO)

def scan_library(path=None):
    """Escanea la raíz de TVShows o Movies según el path indicado."""
    target = path or TVSHOWS_DIR
    req = json.dumps({
        "jsonrpc": "2.0", "method": "VideoLibrary.Scan",
        "params": {"directory": target, "showdialogs": False}, "id": 1
    })
    xbmc.executeJSONRPC(req)

def clean_library(content):
    """
    Limpia de la base de datos de Kodi las entradas cuyos ficheros ya no
    existen. Un VideoLibrary.Scan NO hace esto (solo añade contenido nuevo),
    asi que tras borrar una serie/pelicula hace falta un Clean para que no
    quede rastro en la biblioteca de Kodi. Sin ambito de directorio: ese
    parametro tiene bugs conocidos (no funciona bien con ciertos paths), asi
    que se limpia todo el tipo de contenido para no arriesgarse a que la
    entrada borrada se quede a medias.
    """
    req = json.dumps({
        "jsonrpc": "2.0", "method": "VideoLibrary.Clean",
        "params": {"content": content, "showdialogs": False}, "id": 1
    })
    xbmc.executeJSONRPC(req)

def notify_service():
    """Notifica al servicio que hay datos nuevos para que compruebe de inmediato."""
    xbmc.executebuiltin(f'NotifyAll({ADDON_ID},{ADDON_ID}.Updated,"")')

def sanitize(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    return re.sub(r'\s+', ' ', name).strip(' .')


def find_existing_folder(name, base_dir):
    """Busca una carpeta existente con nombre similar (ignorando mayusculas)."""
    name_norm = sanitize(name).lower()
    try:
        dirs = xbmcvfs.listdir(base_dir)[0] if xbmcvfs.exists(base_dir) else []
        for d in dirs:
            if sanitize(d).lower() == name_norm:
                return d
    except:
        pass
    return None


def clear_removed_status(show_name, meta):
    """Si la serie estaba en removed_series, la quita al re-añadirla."""
    removed = meta.get("removed_series", [])
    name_norm = sanitize(show_name).lower()
    still_removed = [s for s in removed if sanitize(s).lower() != name_norm]
    if len(still_removed) != len(removed):
        meta["removed_series"] = still_removed
        save_metadata(meta)
        log(f"Serie '{show_name}' retirada de removed_series (re-adañida por usuario)")


def strip_tags(text):
    return re.sub(r'(?i)\[/?(?:color|b|i|cr)[^\]]*\]', '', text).strip()

def find_season(text):
    for pat in SEASON_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1)), m.start()
            except (IndexError, ValueError):
                continue
    return None, None

def extract_show_and_season(label):
    clean = strip_tags(label)
    s_num, s_pos = find_season(clean)
    raw = clean[:s_pos] if s_pos is not None else clean
    raw = re.sub(r'[\[\(][^\]\)]*[\]\)]', '', raw)
    raw = re.sub(r'[\[\(][^\]\)]*$', '', raw)
    raw = re.sub(r'[\s\-_.,–—]+$', '', raw).strip()
    return sanitize(raw), s_num

def is_directory(item):
    return item.get('filetype') == 'directory'

def is_promo(item):
    return bool(PROMO_KEYWORDS.search(strip_tags(item.get('label', '') or '')))

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


# ── JSON-RPC ──────────────────────────────────────────────────────────────────

def get_items(path):
    req = json.dumps({
        "jsonrpc": "2.0", "method": "Files.GetDirectory",
        "params": {"directory": path, "media": "video",
                   "properties": ["season", "episode", "title", "file"]},
        "id": 1
    })
    try:
        return json.loads(xbmc.executeJSONRPC(req)).get('result', {}).get('files', [])
    except Exception as e:
        log(f"get_items error: {e}")
        return []


# ── Metadatos ─────────────────────────────────────────────────────────────────

def load_metadata():
    if xbmcvfs.exists(METADATA_FILE):
        try:
            with xbmcvfs.File(METADATA_FILE, 'r') as f:
                data = f.read()
            meta = json.loads(data)
            log(f"Metadata cargado: {len(meta.get('tvshows',{}))} series, {len(meta.get('movies',{}))} pelis")
            return meta
        except Exception as e:
            log(f"Error cargando metadata: {e}")
    else:
        log(f"Metadata no existe aun: {METADATA_FILE}")
    return {"tvshows": {}, "movies": {}}

def save_metadata(meta):
    try:
        if not xbmcvfs.exists(ADDON_DATA):
            log(f"Creando directorio: {ADDON_DATA}")
            xbmcvfs.mkdirs(ADDON_DATA)
        content = json.dumps(meta, ensure_ascii=False, indent=2)
        with xbmcvfs.File(METADATA_FILE, 'w') as f:
            ok = f.write(content)
        log(f"Metadata guardado ({len(content)} bytes, write={ok}): {METADATA_FILE}")
    except Exception as e:
        log(f"ERROR guardando metadata: {e}")

def register_tvshow(show, s_num, path):
    log(f"Registrando serie: {show} T{s_num} → {path}")
    meta = load_metadata()
    meta.setdefault("tvshows", {}).setdefault(show, {})[str(s_num)] = path
    save_metadata(meta)

def register_movie(title, path):
    log(f"Registrando pelicula: {title} → {path}")
    meta = load_metadata()
    meta.setdefault("movies", {})[title] = path
    save_metadata(meta)


# ── Escritura de .strm ────────────────────────────────────────────────────────

def write_episodes(show_dir, show, s_num, episodes, dp, offset, total):
    created = 0
    for i, ep in enumerate(episodes):
        if dp.iscanceled():
            break
        ep_url = ep.get('file', '') or ''
        if not ep_url:
            continue
        e_num    = get_episode_number(ep, i)
        filename = f"{show} S{s_num:02d}E{e_num:02d}.strm"
        filepath = os.path.join(show_dir, filename)
        if not xbmcvfs.exists(filepath):
            log(f"Nuevo: {filename}")
            with xbmcvfs.File(filepath, 'w') as f:
                f.write(ep_url)
            created += 1
        dp.update(int((offset + i + 1) / max(total, 1) * 100),
                  f"T{s_num} · Episodio {e_num}")
    return created


# ── Diálogos ──────────────────────────────────────────────────────────────────

def ask(prompt, default, numeric=False):
    result = xbmcgui.Dialog().input(
        prompt, defaultt=str(default or ''),
        type=xbmcgui.INPUT_NUMERIC if numeric else xbmcgui.INPUT_ALPHANUM)
    if result is None or result == '':
        return None
    if numeric:
        try:
            return int(result)
        except ValueError:
            return default
    return sanitize(result.strip()) or None


# ── Modo película ─────────────────────────────────────────────────────────────

def handle_movie(label, path):
    clean     = strip_tags(label)
    year_m    = re.search(r'\((\d{4})\)', clean)
    year      = year_m.group(1) if year_m else ''
    title_raw = re.sub(r'\(\d{4}\)', '', clean).strip()
    title_raw = sanitize(re.sub(r'[\s\-_.,–—]+$', '', title_raw))

    title = ask("Título de la película", title_raw)
    if not title:
        return

    if not year:
        year = ask("Año (dejar vacío si no se conoce)", '') or ''

    movie_name = f"{title} ({year})" if year else title
    movie_dir  = os.path.join(MOVIES_DIR, movie_name)
    if not xbmcvfs.exists(movie_dir):
        xbmcvfs.mkdirs(movie_dir)

    strm_path = os.path.join(movie_dir, f"{movie_name}.strm")
    if xbmcvfs.exists(strm_path):
        xbmcgui.Dialog().notification("Smart Library",
            f"'{movie_name}' ya estaba en la librería",
            xbmcgui.NOTIFICATION_INFO, 3000)
        return

    with xbmcvfs.File(strm_path, 'w') as f:
        f.write(path)

    register_movie(movie_name, path)
    log(f"Película añadida: {movie_name}")
    xbmcgui.Dialog().notification("Smart Library",
        f"Película añadida: {movie_name} ✓",
        xbmcgui.NOTIFICATION_INFO, 4000)
    notify_service()


# ── Actualización manual ──────────────────────────────────────────────────────

def manual_update():
    """Lanza una comprobación de episodios nuevos de forma inmediata."""
    notify_service()
    xbmcgui.Dialog().notification(
        "Smart Library",
        "Comprobando episodios nuevos...",
        xbmcgui.NOTIFICATION_INFO, 3000)


# ── Eliminar de la librería ───────────────────────────────────────────────────

def find_show_in_metadata(name, meta):
    """Busca una serie en metadata ignorando mayúsculas/minúsculas y caracteres especiales."""
    name_norm = sanitize(name).lower()
    for show in meta.get("tvshows", {}):
        if sanitize(show).lower() == name_norm:
            return "tvshow", show
    for movie in meta.get("movies", {}):
        if sanitize(movie).lower() == name_norm:
            return "movie", movie
    return None, None


def remove_from_library(label):
    """
    Elimina una serie o película del registro y borra su carpeta de .strm.
    Detecta automaticamente el nombre desde el label contextual.
    Anade la serie a 'removed_series' para que el servicio no la re-annaida.
    """
    meta = load_metadata()

    # Intentar detectar el nombre desde el label contextual
    show_name = None
    kind = None
    if label:
        guess, _ = extract_show_and_season(label)
        if guess:
            found_kind, found_name = find_show_in_metadata(guess, meta)
            if found_name:
                show_name = found_name
                kind = found_kind

    # Si no se pudo detectar, mostrar dialogo de seleccion
    if not show_name:
        options = []
        keys    = []
        for show in meta.get("tvshows", {}):
            options.append(f"\U0001f4fa  {show}")
            keys.append(("tvshow", show))
        for movie in meta.get("movies", {}):
            options.append(f"\U0001f3ac  {movie}")
            keys.append(("movie", movie))

        if not options:
            xbmcgui.Dialog().ok("Smart Library", "No hay nada registrado en la libreria.")
            return

        idx = xbmcgui.Dialog().select("Que quieres eliminar?", options)
        if idx < 0:
            return

        kind, show_name = keys[idx]

    if not xbmcgui.Dialog().yesno("Smart Library",
            f"Eliminar '{show_name}' de la libreria?\n"
            "Se borraran los ficheros .strm y dejara de actualizarse."):
        return

    # Anotar como eliminada para que el servicio no la re-annada
    removed = meta.setdefault("removed_series", [])
    if show_name not in removed:
        removed.append(show_name)

    # Borrar carpeta y entrada de metadata
    if kind == "tvshow":
        folder = os.path.join(TVSHOWS_DIR, show_name)
        if show_name in meta.get("tvshows", {}):
            del meta["tvshows"][show_name]
    else:
        folder = os.path.join(MOVIES_DIR, show_name)
        if show_name in meta.get("movies", {}):
            del meta["movies"][show_name]

    if xbmcvfs.exists(folder):
        for f_name in xbmcvfs.listdir(folder)[1]:
            xbmcvfs.delete(os.path.join(folder, f_name))
        xbmcvfs.rmdir(folder, force=True)
        log(f"Carpeta eliminada: {folder}")

    save_metadata(meta)
    xbmcgui.Dialog().notification("Smart Library",
        f"'{show_name}' eliminado de la libreria", xbmcgui.NOTIFICATION_INFO, 3000)
    clean_library("tvshows" if kind == "tvshow" else "movies")



# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    item  = sys.listitem
    label = item.getLabel()
    path  = item.getPath()
    log(f"label='{label}'  path='{path}'")

    # Si es path de libreria de Kodi, ofrecer eliminar directamente
    if path.startswith('videodb://') or path.startswith('/') or not path:
        meta = load_metadata()
        guess, _ = extract_show_and_season(label)
        found_kind = None
        found_name = None
        if guess:
            found_kind, found_name = find_show_in_metadata(guess, meta)
        if not found_name:
            remove_from_library(label)
            return
        if not xbmcgui.Dialog().yesno("Smart Library",
                "Eliminar de Smart Library?\n\n" + found_name + "\n\n"
                "Se borraran los .strm y dejara de aparecer."):
            return
        removed = meta.setdefault("removed_series", [])
        if found_name not in removed:
            removed.append(found_name)
        if found_kind == "tvshow":
            folder = os.path.join(TVSHOWS_DIR, found_name)
            if found_name in meta.get("tvshows", {}):
                del meta["tvshows"][found_name]
        else:
            folder = os.path.join(MOVIES_DIR, found_name)
            if found_name in meta.get("movies", {}):
                del meta["movies"][found_name]
        if xbmcvfs.exists(folder):
            for f_name in xbmcvfs.listdir(folder)[1]:
                xbmcvfs.delete(os.path.join(folder, f_name))
            xbmcvfs.rmdir(folder, force=True)
            log(f"Carpeta eliminada: {folder}")
        save_metadata(meta)
        clean_library(found_kind or "tvshows")
        xbmcgui.Dialog().notification("Smart Library",
            "'" + found_name + "' eliminado de Smart Library",
            xbmcgui.NOTIFICATION_INFO, 3000)
        return

    choice = xbmcgui.Dialog().select(
        "Smart Library",
        ["\U0001f4fa  Anadir Serie o Temporada", "\U0001f3ac  Anadir Pelicula",
         "\U0001f5d1  Eliminar de la libreria",  "\U0001f504  Actualizar series ahora"])
    if choice < 0:
        return

    if choice == 1:
        handle_movie(label, path)
        return
    if choice == 2:
        remove_from_library(label)
        return
    if choice == 3:
        manual_update()
        return

    # Serie / Temporada
    show, s_num_hint = extract_show_and_season(label)
    show = ask("Nombre de la serie", show)
    if not show:
        return

    items = get_items(path)
    if not items:
        xbmcgui.Dialog().ok("Smart Library",
            f"No se encontro contenido.\nSerie: {show}")
        return

    has_dirs = any(is_directory(it) for it in items)
    existing = find_existing_folder(show, TVSHOWS_DIR)
    if existing and existing != show:
        log(f"Usando carpeta existente: '{existing}' en vez de '{show}'")
        show = existing
    show_dir = os.path.join(TVSHOWS_DIR, show)
    if not xbmcvfs.exists(show_dir):
        xbmcvfs.mkdirs(show_dir)
    clear_removed_status(show, load_metadata())

    dp = xbmcgui.DialogProgress()
    total_created = 0

    # Serie completa
    if has_dirs:
        seasons = [it for it in items if is_directory(it)]
        dp.create("Smart Library", f"{show} - Serie completa")
        season_data = []
        for s in seasons:
            eps = [e for e in get_items(s.get('file', '')) if not is_promo(e)]
            s_num, _ = find_season(strip_tags(s.get('label', '')))
            if s_num is None:
                s_num, _ = find_season(s.get('file', ''))
            s_num = s_num or (len(season_data) + 1)
            register_tvshow(show, s_num, s.get('file', ''))
            season_data.append((s_num, eps))

        total_eps = sum(len(e) for _, e in season_data)
        offset = 0
        for s_num, eps in season_data:
            total_created += write_episodes(show_dir, show, s_num, eps, dp, offset, total_eps)
            offset += len(eps)

    # Temporada individual
    else:
        s_num = ask("Numero de temporada", s_num_hint, numeric=True)
        if s_num is None:
            return
        episodes = [it for it in items if not is_directory(it) and not is_promo(it)]
        dp.create("Smart Library", f"{show} - Temporada {s_num}")
        register_tvshow(show, s_num, path)
        total_created = write_episodes(show_dir, show, s_num, episodes, dp, 0, len(episodes))

    dp.close()

    if total_created > 0:
        xbmcgui.Dialog().notification("Smart Library",
            f"{show}: {total_created} episodio(s) anadidos \u2713",
            xbmcgui.NOTIFICATION_INFO, 4000)
        notify_service()
    else:
        xbmcgui.Dialog().notification("Smart Library",
            f"{show}: sin episodios nuevos",
            xbmcgui.NOTIFICATION_INFO, 3000)


if __name__ == '__main__':
    main()
