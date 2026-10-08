# kodi-smart-library

Addon de Kodi **Smart Library** (`script.context.smartlibrary`), propio de Manu.

Mantiene una biblioteca propia de **series y películas** a partir de varios addons
(Crunchyroll, SlyGuy.max, Amazon, Movistar...) y las inyecta en Kodi como ficheros
`.strm`, con un servicio que los refresca cuando cambian las URLs.

## Contenido

- `script.context.smartlibrary/` → el addon (**v6.13.4**)
  - `addon.xml` → definición del addon
  - `context_menu.py` → menú contextual: añadir / actualizar / **eliminar de la librería**
  - `service.py` → servicio en segundo plano que comprueba y refresca los `.strm`

## Instalación

Copiar la carpeta `script.context.smartlibrary/` a la carpeta `addons/` del perfil de Kodi
y reiniciar Kodi.

## Cambios recientes (v6.13.4)

- **Eliminar de la librería**: además de borrar los `.strm`, ahora **quita la entrada de la
  biblioteca de Kodi**, **borra la carpeta completa** y añade el nombre a `removed_series`
  para que **no vuelva a aparecer**.
- **Listado para borrar ordenado alfabéticamente** (sin distinguir acentos ni mayúsculas).
- El servicio **ya no crea carpetas vacías** cuando una fuente no devuelve episodios.
- **Borrado de carpeta robusto**: se usa `os`/`shutil` en vez de `xbmcvfs.exists()`
  (que devuelve `False` para carpetas sin barra final y hacía que el borrado no se ejecutara).
