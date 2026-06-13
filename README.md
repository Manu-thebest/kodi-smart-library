# Smart Library — Kodi Context Addon

**Versión:** 6.11.0
**Autor:** Manu
**Tipo:** Context menu addon + Service (Kodi Python 3)

## ¿Qué es?

Smart Library es un addon de contexto para Kodi que permite añadir series y películas a una librería local de forma inteligente. En lugar de depender de scrapers externos, genera archivos `.strm` que apuntan al contenido de tus plugins de streaming (Amazon, HBO, Movistar+, etc.), y un servicio en segundo plano comprueba periódicamente si hay episodios nuevos.

## Características

- **Menú contextual**: Click derecho sobre cualquier serie/película en un plugin → "Añadir a mi Librería Inteligente"
- **Series completas o temporadas sueltas**: Detecta automáticamente las temporadas dentro de una carpeta
- **Películas**: Registra películas individuales con título y año
- **Auto-actualización**: Servicio que revisa cada 6 horas si hay episodios nuevos y los añade
- **Actualización manual**: Opción en el menú para forzar la comprobación inmediata
- **Filtro de promos**: Ignora trailers, teasers, making-of, etc.
- **Eliminación**: Permite quitar series/películas de la librería y borrar sus `.strm`

## Estructura del addon

```
script.context.smartlibrary/
├── addon.xml              # Manifiesto del addon
├── context_menu.py        # Menú contextual (click derecho)
└── service.py             # Servicio de auto-actualización
```

## Instalación

1. Comprime el addon en un ZIP:
   ```
   script.context.smartlibrary_v6.11.0.zip
   ```
2. En Kodi: **Ajustes → Addons → Instalar desde archivo ZIP**
3. Selecciona el ZIP
4. Reinicia Kodi

## Uso

### Añadir una serie

1. Abre tu plugin de streaming (Amazon, HBO, etc.)
2. Navega hasta la serie que quieras añadir
3. **Click derecho** → "Añadir a mi Librería Inteligente"
4. Selecciona **"Añadir Serie o Temporada"**
5. Confirma el nombre de la serie
6. Si la carpeta tiene subcarpetas (temporadas), se añade la serie completa
7. Si es una temporada suelta, introduce el número de temporada

### Añadir una película

1. Navega hasta la película en tu plugin
2. **Click derecho** → "Añadir a mi Librería Inteligente"
3. Selecciona **"Añadir Película"**
4. Confirma el título y el año

### Actualizar manualmente

1. Click derecho sobre cualquier item de un plugin
2. Selecciona **"Actualizar series ahora"**
3. El servicio comprobará episodios nuevos inmediatamente

### Eliminar de la librería

1. Click derecho sobre cualquier item de un plugin
2. Selecciona **"Eliminar de la librería"**
3. Elige qué serie o película quieres quitar
4. Se borrarán los `.strm` y dejará de actualizarse

## Cómo funciona internamente

### Archivos .strm

Kodi usa archivos `.strm` (stream) para reproducir contenido remoto. Smart Library genera estos archivos con la URL del plugin de streaming, organizados así:

```
addon_data/script.context.smartlibrary/Library/
├── TVShows/
│   ├── Breaking Bad/
│   │   ├── Breaking Bad S01E01.strm
│   │   ├── Breaking Bad S01E02.strm
│   │   └── ...
│   └── The Office/
│       └── ...
└── Movies/
    ├── Inception (2010)/
    │   └── Inception (2010).strm
    └── ...
```

### Metadata (metadata.json)

El addon guarda un registro de qué series/películas tienes y de dónde vienen:

```json
{
  "tvshows": {
    "Breaking Bad": {
      "1": "plugin://plugin.video.amazon/?mode=season&show=...",
      "2": "plugin://plugin.video.amazon/?mode=season&show=..."
    }
  },
  "movies": {
    "Inception (2010)": "plugin://plugin.video.amazon/?mode=movie&id=..."
  }
}
```

### Servicio de actualización

El servicio (`service.py`) hace esto:

1. **Al iniciar Kodi**: Espera 30 segundos, fuentes de librería configuradas, primera comprobación
2. **Cada 6 horas**: Comprueba si hay episodios nuevos en las fuentes registradas
3. **Notificación manual**: Cuando el usuario fuerza una actualización desde el menú
4. Si encuentra episodios nuevos → los escribe como `.strm` → escanea la librería

### Fuentes de Kodi

El addon añade automáticamente sus carpetas al `sources.xml` de Kodi para que la librería las reconozca:

```xml
<video>
  <source>
    <name>Smart Library – Series</name>
    <path>special://profile/addon_data/script.context.smartlibrary/Library/TVShows/</path>
  </source>
  <source>
    <name>Smart Library – Películas</name>
    <path>special://profile/addon_data/script.context.smartlibrary/Library/Movies/</path>
  </source>
</video>
```

## Detección de temporadas y episodios

El addon usa expresiones regulares para extraer números de temporada y episodio de los nombres de archivo:

**Temporadas:**
- `1ª temporada`, `Temporada 2`, `Season 3`, `T1`, `S01`

**Episodios:**
- `S01E02`, `1x02`, `episodio 2`, `capítulo 2`

## Filtro de promos

Se ignoran automáticamente archivos que contengan en el nombre:
- trailer, teaser, adelanto, avance, promo, clip, preview
- featurette, making of, behind the scenes, extra, bonus

## Notas

- El contenido **no se descarga**, solo se crea un acceso directo al stream
- Si el plugin de streaming deja de funcionar, los `.strm` dejarán de reproducirse
- La librería de Kodi se actualiza automáticamente al añadir contenido nuevo
- Compatible con Kodi 19+ (Python 3)
