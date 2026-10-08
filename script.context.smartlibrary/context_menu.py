import sys, xbmc, xbmcgui, xbmcvfs, os, json, re, threading, shutil

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

def notify_service():
    """Notifica al servicio que hay datos nuevos para que compruebe de inmediato."""
    xbmc.executebuiltin(f'NotifyAll({ADDON_ID},{ADDON_ID}.Updated,"")')

def sanitize(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    return re.sub(r'\s+', ' ', name).strip(' .')

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
    # Eliminar corchetes anidados [...]
    while '[' in raw and ']' in raw:
        raw = re.sub(r'\[[^\[\]]*\]', '', raw)
    # Eliminar parentesis anidados (...)
    while '(' in raw and ')' in raw:
        raw = re.sub(r'\([^()]*\)', '', raw)
    # Eliminar corchetes/parentesis colgantes al final
    raw = re.sub(r'[\[\(][^\]\)]*$', '', raw)
    raw = re.sub(r'[\s\-_.,–—\)]+$', '', raw).strip()
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
    # Intentar extraer numero del path del archivo
    filepath = ep.get('file', '') or ''
    for pat in EPISODE_PATTERNS:
        m = re.search(pat, filepath, re.IGNORECASE)
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
        else:
            # Actualizar si la URL cambio (FIX: refrescar .strm)
            try:
                with xbmcvfs.File(filepath, 'r') as f_read:
                    old_url = f_read.read()
                if old_url.rstrip('\r\n') != ep_url.rstrip('\r\n'):
                    log(f"Actualizado: {filename}")
                    with xbmcvfs.File(filepath, 'w') as f:
                        f.write(ep_url)
                    created += 1
            except Exception:
                pass
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
    scan_library(MOVIES_DIR)
    notify_service()


# ── Actualización manual ──────────────────────────────────────────────────────


def _get_items_safe(path, timeout=15):
    """Obtiene items con timeout y filtra solo dicts."""
    result = []
    def worker():
        for media in ("video", "files"):
            try:
                req = json.dumps({
                    "jsonrpc": "2.0", "method": "Files.GetDirectory",
                    "params": {"directory": path, "media": media,
                               "properties": ["season", "episode", "title", "file"]},
                    "id": 1
                })
                res = json.loads(xbmc.executeJSONRPC(req))
                if res.get('error'):
                    continue
                items = res.get('result', {}).get('files', [])
                if isinstance(items, list) and items:
                    result.extend(it for it in items if isinstance(it, dict))
                    return
            except:
                pass
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    return result


def _fix_amazon_path(path):
    """Intenta reparar rutas Amazon quitando el GUID de temporada (ultimo segmento)."""
    if 'amazon' not in path.lower():
        return path
    parts = path.rstrip('/').rsplit('/', 1)
    if len(parts) < 2:
        return path
    # Solo reparar si el ultimo segmento contiene un GUID (amzn1.dv.gti.xxx)
    last = parts[1]
    if 'amzn1.dv.gti.' in last:
        fixed = parts[0] + '/'
        log("  Ruta reparada (quitado GUID temporada): %s" % fixed[:80])
        return fixed
    return path


def _do_check():
    """Ejecuta la comprobacion en el hilo actual (sin UI)."""
    meta = load_metadata()
    new_count = 0
    removed = meta.get("removed_series", [])
    active = {s: seas for s, seas in meta.get("tvshows", {}).items() if s not in removed}
    path_fixes = []
    
    for show, seasons in active.items():
        show_dir = os.path.join(TVSHOWS_DIR, show)
        if not xbmcvfs.exists(show_dir):
            try:
                xbmcvfs.mkdirs(show_dir)
            except:
                continue
        
        log("Revisando: %s" % show)
        
        for s_str, season_path in list(seasons.items()):
            if not season_path:
                continue
            try:
                s_num = int(s_str)
            except:
                continue
            
            log("  T%d: %s" % (s_num, season_path[:60]))
            
            episodes = _get_items_safe(season_path)
            if not episodes and 'amazon' in season_path.lower():
                fixed_path = _fix_amazon_path(season_path)
                if fixed_path != season_path:
                    log("  Reintentando ruta reparada para %s T%d" % (show, s_num))
                    eps2 = _get_items_safe(fixed_path)
                    if eps2:
                        log("  Ruta reparada FUNCIONA para %s T%d" % (show, s_num))
                        episodes = eps2
                        path_fixes.append((show, s_str, fixed_path))
            
            if not episodes:
                log("  T%d: SIN EPISODIOS" % s_num)
                continue
            
            for i, ep in enumerate(episodes):
                ep_url = ep.get('file', '') or ''
                if not ep_url:
                    continue
                e_num = get_episode_number(ep, i)
                fname = "%s S%02dE%02d.strm" % (show, s_num, e_num)
                fpath = os.path.join(show_dir, fname)
                if not xbmcvfs.exists(fpath):
                    log("  -> Nuevo: %s" % fname)
                    try:
                        with xbmcvfs.File(fpath, 'w') as f:
                            f.write(ep_url)
                        new_count += 1
                    except:
                        pass
    
    # Guardar reparaciones de rutas
    if path_fixes:
        for show, s_str, fixed_path in path_fixes:
            meta["tvshows"][show][s_str] = fixed_path
        save_metadata(meta)
        log("%d ruta(s) Amazon reparadas" % len(path_fixes))
    
    return new_count


def _force_check():
    """Lanza la comprobacion en segundo plano sin bloquear la UI."""
    xbmcgui.Dialog().notification(
        "Smart Library",
        "Comprobando series en segundo plano...",
        xbmcgui.NOTIFICATION_INFO, 2000)
    
    def worker():
        try:
            new_count = _do_check()
            if new_count:
                msg = "%d episodio(s) nuevo(s) encontrados" % new_count
                xbmcgui.Dialog().notification("Smart Library", msg,
                    xbmcgui.NOTIFICATION_INFO, 4000)
            else:
                xbmcgui.Dialog().notification("Smart Library",
                    "Sin episodios nuevos",
                    xbmcgui.NOTIFICATION_INFO, 3000)
            
            # Escanear para que Kodi los vea
            req = json.dumps({
                "jsonrpc": "2.0", "method": "VideoLibrary.Scan",
                "params": {"directory": TVSHOWS_DIR, "showdialogs": False},
                "id": 1
            })
            xbmc.executeJSONRPC(req)
        except Exception as e:
            log("Error en _force_check: %s" % e)
    
    t = threading.Thread(target=worker, daemon=True)
    t.start()

def manual_update():
    """Lanza una comprobación de episodios nuevos de forma inmediata."""
    _force_check()


# ── Eliminar de la librería ───────────────────────────────────────────────────

# Para ordenar alfabéticamente sin acentos ni mayúsculas
_ACCENTS = str.maketrans("áàäâãÁÀÄÂÃéèëêÉÈËÊíìïîÍÌÏÎóòöôõÓÒÖÔÕúùüûÚÙÜÛñÑçÇ",
                         "aaaaaAAAAAeeeeEEEEiiiiIIIIoooooOOOOOuuuuUUUUnNcC")


def _sort_key(text):
    """Clave de orden alfabético: sin acentos y sin distinguir mayúsculas."""
    return (text or "").translate(_ACCENTS).casefold()


def _delete_tree(folder):
    """Borra recursivamente una carpeta y todo su contenido.

    Usa os/shutil (fiable en rutas locales); si no aplica, cae a xbmcvfs.
    Devuelve True si la carpeta ya no existe.
    """
    if os.path.isdir(folder):
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except Exception as exc:                # noqa: BLE001
            log(f"borrar carpeta: error: {folder}: {exc}")
        if not os.path.exists(folder):
            return True
    try:
        dirs, files = xbmcvfs.listdir(folder)
    except Exception:                           # noqa: BLE001
        dirs, files = [], []
    for f in files:
        try:
            xbmcvfs.delete(os.path.join(folder, f))
        except Exception:                       # noqa: BLE001
            pass
    for d in dirs:
        _delete_tree(os.path.join(folder, d))
    try:
        xbmcvfs.rmdir(folder)
    except Exception:                           # noqa: BLE001
        pass
    return not os.path.exists(folder)


def _remove_from_kodi_library(kind, name, folder):
    """Quita de la BIBLIOTECA DE KODI las entradas cuyo fichero esté dentro de *folder*.

    Sin esto, aunque se borren los .strm, la serie/película sigue apareciendo en
    Kodi (la entrada de la BD no se limpia hasta un 'Limpiar biblioteca' manual).
    """
    if kind == "tvshow":
        method, idkey, remove = "VideoLibrary.GetTVShows", "tvshowid", "VideoLibrary.RemoveTVShow"
    else:
        method, idkey, remove = "VideoLibrary.GetMovies", "movieid", "VideoLibrary.RemoveMovie"
    req = json.dumps({"jsonrpc": "2.0", "method": method,
                      "params": {"properties": ["file"]}, "id": 1})
    try:
        resp = json.loads(xbmc.executeJSONRPC(req))
        items = (resp.get("result", {}) or {}).get(
            "tvshows" if kind == "tvshow" else "movies", []) or []
    except Exception as exc:                    # noqa: BLE001
        log(f"limpiar biblioteca: fallo listando {kind}: {exc}")
        return
    prefix = folder.rstrip("/") + "/"
    n = 0
    for it in items:
        f = it.get("file") or ""
        if f.startswith(prefix) or f.rstrip("/") == folder.rstrip("/"):
            try:
                xbmc.executeJSONRPC(json.dumps(
                    {"jsonrpc": "2.0", "method": remove,
                     "params": {idkey: it.get(idkey)}, "id": 1}))
                n += 1
            except Exception as exc:            # noqa: BLE001
                log(f"limpiar biblioteca: fallo quitando {f}: {exc}")
    log(f"limpiar biblioteca: {n} entrada(s) quitadas de {kind} '{name}'")


def remove_from_library(label):
    """
    Elimina una serie o película del registro, borra su carpeta y la saca de la
    BIBLIOTECA DE KODI. Esta es la única forma de que el servicio deje de rastrearla.
    """
    meta = load_metadata()

    # Construir lista ORDENADA alfabéticamente (series primero, luego películas)
    options = []
    keys    = []
    for show in sorted(meta.get("tvshows", {}), key=_sort_key):
        options.append(f"📺  {show}")
        keys.append(("tvshow", show))
    for movie in sorted(meta.get("movies", {}), key=_sort_key):
        options.append(f"🎬  {movie}")
        keys.append(("movie", movie))

    if not options:
        xbmcgui.Dialog().ok("Smart Library", "No hay nada registrado en la librería.")
        return

    idx = xbmcgui.Dialog().select("¿Qué quieres eliminar?", options)
    if idx < 0:
        return

    kind, name = keys[idx]

    if not xbmcgui.Dialog().yesno("Smart Library",
            f"¿Eliminar '{name}' de la librería?\n"
            "Se borrarán los ficheros y la entrada de la biblioteca de Kodi."):
        return

    if kind == "tvshow":
        folder = os.path.join(TVSHOWS_DIR, name)
        del meta["tvshows"][name]
    else:
        folder = os.path.join(MOVIES_DIR, name)
        del meta["movies"][name]

    # 1) Sacar de la biblioteca de Kodi (ANTES de borrar los ficheros)
    _remove_from_kodi_library(kind, name, folder)

    # 2) Borrar la carpeta y TODO su contenido (los .strm y lo que haya dentro).
    #    OJO: xbmcvfs.exists() devuelve False para carpetas sin barra final en este
    #    Kodi -> comprobamos tambien (y sobre todo) con os.path.isdir.
    log(f"Borrado '{name}': carpeta={folder} isdir={os.path.isdir(folder)}")
    if os.path.isdir(folder) or xbmcvfs.exists(folder):
        ok = _delete_tree(folder)
        log(f"Carpeta eliminada: {folder} (ok={ok})")
    else:
        log(f"Aviso: la carpeta ya no existe: {folder}")

    # 3) Marcarla como eliminada para que el servicio no la vuelva a crear
    removed = meta.get("removed_series", []) or []
    if name not in removed:
        removed.append(name)
    meta["removed_series"] = removed

    save_metadata(meta)
    xbmcgui.Dialog().notification("Smart Library",
        f"'{name}' eliminado de la librería", xbmcgui.NOTIFICATION_INFO, 3000)
    scan_library(TVSHOWS_DIR if kind == "tvshow" else MOVIES_DIR)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    item  = sys.listitem
    label = item.getLabel()
    path  = item.getPath()
    log(f"label='{label}'  path='{path}'")

    # Rechazar paths que no son de plugin SOLO si no es actualización
    is_local_path = path.startswith('videodb://') or path.startswith('/') or not path

    # Forzar mostrar el dialogo incluso desde paths locales
    if is_local_path:
        # Solo mostrar opciones de gestión (no añadir)
        choice = xbmcgui.Dialog().select(
            "Smart Library",
            ["🔄  Actualizar series ahora", "🗑️  Eliminar de la librería"])
        if choice < 0:
            return
        if choice == 0:
            manual_update()
            return
        if choice == 1:
            remove_from_library(label)
            return

    choice = xbmcgui.Dialog().select(
        "Smart Library",
        ["📺  Añadir Serie o Temporada", "🎬  Añadir Película",
         "🗑️  Eliminar de la librería",  "🔄  Actualizar series ahora"])
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

    # ── Serie / Temporada ─────────────────────────────────────────────────────
    show, s_num_hint = extract_show_and_season(label)
    show = ask("Nombre de la serie", show)
    if not show:
        return

    items = get_items(path)
    if not items:
        xbmcgui.Dialog().ok("Smart Library",
            f"No se encontró contenido.\nSerie: {show}")
        return

    has_dirs = any(is_directory(it) for it in items)
    show_dir = os.path.join(TVSHOWS_DIR, show)
    if not xbmcvfs.exists(show_dir):
        xbmcvfs.mkdirs(show_dir)

    dp = xbmcgui.DialogProgress()
    total_created = 0

    # ── Serie completa ────────────────────────────────────────────────────────
    if has_dirs:
        seasons = [it for it in items if is_directory(it)]
        dp.create("Smart Library", f"{show} — Serie completa")
        season_data = []
        for s in seasons:
            eps   = [e for e in get_items(s.get('file', '')) if not is_promo(e)]
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

    # ── Temporada individual ──────────────────────────────────────────────────
    else:
        s_num = ask("Número de temporada", s_num_hint, numeric=True)
        if s_num is None:
            return
        episodes = [it for it in items if not is_directory(it) and not is_promo(it)]
        dp.create("Smart Library", f"{show} — Temporada {s_num}")
        register_tvshow(show, s_num, path)
        total_created = write_episodes(show_dir, show, s_num, episodes, dp, 0, len(episodes))

    dp.close()

    if total_created > 0:
        xbmcgui.Dialog().notification("Smart Library",
            f"{show}: {total_created} episodio(s) añadidos ✓",
            xbmcgui.NOTIFICATION_INFO, 4000)
        scan_library(TVSHOWS_DIR)
        notify_service()
    else:
        xbmcgui.Dialog().notification("Smart Library",
            f"{show}: sin episodios nuevos",
            xbmcgui.NOTIFICATION_INFO, 3000)


if __name__ == '__main__':
    main()