import json
import os
import queue
import re
import sys
import threading
import tkinter as tk
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from PIL import Image, ImageDraw, ImageFont, ImageTk

# ============================================================
# CONFIGURACIÓN
#
# Los valores se pueden cambiar sin tocar el código editando el
# archivo "steamgrid_config.json", que se crea automáticamente
# junto al script / .exe la primera vez que se ejecuta.
#
# Las carpetas por defecto usan la carpeta del usuario actual,
# así que en tu PC equivalen a C:\Users\johns\Desktop\ljuan.fyi\...
# y en otros equipos se adaptan solas.
# ============================================================


def carpeta_app():
    """Carpeta donde vive el .exe (o el .py si se ejecuta como script)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = Path.home() / "Desktop" / "ljuan.fyi"

CONFIG_DEFECTO = {
    "api_key": "7aac2563018b0c1c792e2f1c2072089e",
    "quick_cover_dir": str(BASE_DIR / "covers"),
    "quick_icon_dir": str(BASE_DIR / "icons"),
    "quick_side_dir": str(BASE_DIR / "sides"),
}

CONFIG_PATH = carpeta_app() / "steamgrid_config.json"


def cargar_config():
    config = dict(CONFIG_DEFECTO)

    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                guardado = json.load(f)

            for clave in CONFIG_DEFECTO:
                if guardado.get(clave):
                    config[clave] = guardado[clave]
        else:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(CONFIG_DEFECTO, f, indent=4, ensure_ascii=False)

    except Exception as e:
        print("No se pudo leer/crear la configuración:", e)

    return config


CONFIG = cargar_config()

API_KEY = CONFIG["api_key"]

QUICK_COVER_DIR = CONFIG["quick_cover_dir"]
QUICK_ICON_DIR = CONFIG["quick_icon_dir"]
QUICK_SIDE_DIR = CONFIG["quick_side_dir"]

SGDB_URL = "https://www.steamgriddb.com/api/v2"
STEAM_STORE_URL = "https://store.steampowered.com"

SGDB_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

STEAM_ASSETS_BASE = "https://shared.fastly.steamstatic.com/store_item_assets/"

STEAM_CDN_HOSTS = [
    "shared.fastly.steamstatic.com",
    "cdn.cloudflare.steamstatic.com",
    "cdn.akamai.steamstatic.com",
    "steamcdn-a.akamaihd.net",
]

# Hosts donde Steam guarda los iconos de comunidad / cliente
ICON_HOSTS = [
    "https://shared.fastly.steamstatic.com/community_assets/images/apps",
    "https://shared.akamai.steamstatic.com/community_assets/images/apps",
    "https://cdn.fastly.steamstatic.com/steamcommunity/public/images/apps",
    "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps",
    "https://media.steampowered.com/steamcommunity/public/images/apps",
]

# Cookies para saltar el age-gate de la tienda
COOKIES_EDAD = {
    "birthtime": "0",
    "lastagecheckage": "1-0-1990",
    "mature_content": "1",
    "wants_mature_content": "1",
}

# Sesión reutilizable con más conexiones simultáneas (se usan hilos)
http = requests.Session()
http.headers.update({"User-Agent": "Mozilla/5.0 SteamGridTool"})

_adaptador = HTTPAdapter(pool_connections=32, pool_maxsize=32)
http.mount("https://", _adaptador)
http.mount("http://", _adaptador)

# ============================================================
# ESTADO GLOBAL
# ============================================================

juegos_encontrados = []
imagenes_preview = []          # referencias a PhotoImage (evita que se borren)

assets_actuales = {}
previews_actuales = {}         # clave -> PIL.Image (miniatura ya preparada)
nombre_juego_actual = ""
app_id_actual = None
sgdb_id_actual = None
side_imagen_actual = None      # PIL.Image 71x877

# Cachés
cache_bytes = {}               # url -> (bytes, content_type)
cache_validas = {}             # url -> bool
cache_juegos = {}              # sgdb_id -> datos completos del juego

# Tokens para ignorar resultados de cargas antiguas
carga_id = 0
busqueda_id = 0

# Cola para comunicar los hilos con la interfaz (Tk no es thread-safe)
cola_ui = queue.Queue()


def ui(funcion):
    """Programa una función para que se ejecute en el hilo de la interfaz."""
    cola_ui.put(funcion)


def procesar_cola():
    try:
        while True:
            funcion = cola_ui.get_nowait()
            try:
                funcion()
            except Exception:
                traceback.print_exc()
    except queue.Empty:
        pass

    app.after(40, procesar_cola)


# ============================================================
# UTILIDADES
# ============================================================

def nombre_abreviado(nombre):
    """Nombre de archivo seguro que conserva caracteres unicode (CJK)."""
    n = unicodedata.normalize("NFKD", nombre.lower().strip())
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[^\w\s]", "", n)
    n = re.sub(r"\s+", "_", n).strip("_")
    n = n[:45]

    if not n:
        n = f"steam_{app_id_actual or sgdb_id_actual or 'juego'}"

    return n


def descargar_bytes(url, timeout=30):
    """Descarga una URL (con caché). Devuelve (bytes, content_type)."""
    if url in cache_bytes:
        return cache_bytes[url]

    r = http.get(url, timeout=timeout)
    r.raise_for_status()

    resultado = (r.content, r.headers.get("Content-Type", "").lower())
    cache_bytes[url] = resultado
    return resultado


def url_valida(url):
    """Comprueba que la URL existe y es una imagen (con caché)."""
    if url in cache_validas:
        return cache_validas[url]

    resultado = False

    try:
        r = http.head(url, timeout=8, allow_redirects=True)

        if r.status_code in (403, 405, 501):
            r = http.get(url, timeout=10, stream=True)
            r.close()

        if r.ok:
            tipo = r.headers.get("Content-Type", "").lower()
            resultado = tipo.startswith("image") or tipo == ""

    except Exception:
        resultado = False

    cache_validas[url] = resultado
    return resultado


def primera_valida(urls):
    for url in urls:
        if url_valida(url):
            return url
    return None


# ============================================================
# STEAMGRIDDB
# ============================================================

def buscar_juegos(nombre):
    """Busca en SteamGridDB. Lanza excepción si falla."""
    url = f"{SGDB_URL}/search/autocomplete/{quote(nombre)}"

    r = http.get(url, headers=SGDB_HEADERS, timeout=10)
    r.raise_for_status()

    return r.json().get("data", []) or []


def obtener_steam_app_id_sgdb(sgdb_id):
    """Pide a SteamGridDB el Steam App ID real del juego."""
    try:
        r = http.get(
            f"{SGDB_URL}/games/id/{sgdb_id}",
            headers=SGDB_HEADERS,
            params={"platformdata": "steam"},
            timeout=10,
        )
        r.raise_for_status()

        data = r.json().get("data", {}) or {}
        steam = (data.get("external_platform_data") or {}).get("steam") or []

        if steam:
            return steam[0].get("id")

    except Exception as e:
        print("Error obteniendo Steam ID desde SGDB:", e)

    return None


def sgdb_por_juego(tipo, sgdb_id, params=None):
    """tipo: grids, heroes, logos, icons (usa el ID de SteamGridDB)."""
    try:
        r = http.get(
            f"{SGDB_URL}/{tipo}/game/{sgdb_id}",
            headers=SGDB_HEADERS,
            params=params or {},
            timeout=10,
        )
        if r.ok:
            return r.json().get("data", []) or []

    except Exception as e:
        print(f"Error SGDB {tipo}:", e)

    return []


def primer_url_sgdb(items):
    if not items:
        return None
    return items[0].get("url") or items[0].get("thumb")


# ============================================================
# STEAM: BÚSQUEDA POR NOMBRE (respaldo)
# ============================================================

def buscar_en_steam(nombre):
    """Busca por nombre en Steam probando varios idiomas y regiones."""
    intentos = [
        ("english", "us"),
        ("schinese", "cn"),
        ("tchinese", "tw"),
        ("japanese", "jp"),
        ("koreana", "kr"),
    ]

    nombre_lower = nombre.lower().strip()

    def consultar(par):
        lang, cc = par
        try:
            r = http.get(
                f"{STEAM_STORE_URL}/api/storesearch/",
                params={"term": nombre, "l": lang, "cc": cc},
                timeout=10,
            )
            r.raise_for_status()
            return r.json().get("items", [])
        except Exception as e:
            print("Error buscando en Steam:", e)
            return []

    # Los 5 idiomas se consultan a la vez; se respeta el orden de prioridad
    with ThreadPoolExecutor(max_workers=len(intentos)) as pool:
        respuestas = list(pool.map(consultar, intentos))

    primero = None

    for items in respuestas:
        for juego in items:
            if juego.get("name", "").lower().strip() == nombre_lower:
                return juego.get("id")

        if items and primero is None:
            primero = items[0].get("id")

    return primero


# ============================================================
# STEAM: ASSETS ORIGINALES
#
# Muchos juegos nuevos guardan sus imágenes en rutas con hash:
#   .../apps/<appid>/<hash>/header.jpg
# y cada imagen puede tener un hash distinto. Esas rutas no se
# pueden adivinar, así que se leen de 3 fuentes:
#   1) API de la tienda (IStoreBrowseService)
#   2) HTML de la página de la tienda
#   3) appinfo (api.steamcmd.net) -> iconos y library assets
# Después se prueban las URLs de cada asset EN ORDEN DE PRIORIDAD
# y se usa la primera que exista (los assets se comprueban en
# paralelo entre sí).
# Todas las funciones devuelven una lista de pares (clave, url).
# ============================================================

# Orden = prioridad
MAPA_API = [
    ("header", "portada_amplia"),
    ("library_capsule_2x", "portada"),
    ("library_capsule", "portada"),
    ("library_hero_2x", "fondo"),
    ("library_hero", "fondo"),
    ("library_logo_2x", "logo_steam"),
    ("library_logo", "logo_steam"),
    ("main_capsule", "capsule_main"),
    ("small_capsule", "capsule_small"),
    ("hero_capsule_2x", "hero_capsule"),
    ("hero_capsule", "hero_capsule"),
    ("raw_page_background", "page_bg"),
    ("page_background", "page_bg"),
    ("page_background_path", "page_bg"),
]

# Nombre de archivo -> clave (para leer el HTML de la tienda)
ARCHIVO_A_CLAVE = {
    "header.jpg": "portada_amplia",
    "capsule_616x353.jpg": "capsule_main",
    "capsule_231x87.jpg": "capsule_small",
    "library_600x900_2x.jpg": "portada",
    "library_600x900.jpg": "portada",
    "library_hero_2x.jpg": "fondo",
    "library_hero.jpg": "fondo",
    "logo_2x.png": "logo_steam",
    "logo.png": "logo_steam",
    "hero_capsule_2x.jpg": "hero_capsule",
    "hero_capsule.jpg": "hero_capsule",
    "page_bg_raw.jpg": "page_bg",
}

EXTENSIONES_IMAGEN = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def candidatos_api(app_id):
    pares = []

    try:
        payload = {
            "ids": [{"appid": int(app_id)}],
            "context": {"language": "english", "country_code": "US"},
            "data_request": {"include_assets": True},
        }

        r = http.get(
            "https://api.steampowered.com/IStoreBrowseService/GetItems/v1",
            params={"input_json": json.dumps(payload)},
            timeout=10,
        )
        r.raise_for_status()

        item = r.json()["response"]["store_items"][0]
        assets = item.get("assets", {}) or {}
        fmt = assets.get("asset_url_format")

        if not fmt:
            return pares

        def construir(valor):
            return STEAM_ASSETS_BASE + fmt.replace("${FILENAME}", valor)

        usados = {"asset_url_format", "community_icon"}

        for campo, clave in MAPA_API:
            usados.add(campo)
            valor = assets.get(campo)
            if isinstance(valor, str) and valor:
                pares.append((clave, construir(valor)))

        # Icono de comunidad (solo trae el hash)
        hash_icono = assets.get("community_icon")
        if isinstance(hash_icono, str) and hash_icono:
            for host in ICON_HOSTS:
                pares.append(("community_icon", f"{host}/{app_id}/{hash_icono}.jpg"))

        # Cualquier otro asset de imagen que Steam devuelva
        for campo, valor in assets.items():
            if campo in usados or not isinstance(valor, str):
                continue
            if valor.lower().split("?")[0].endswith(EXTENSIONES_IMAGEN):
                pares.append((f"extra_{campo}", construir(valor)))

    except Exception as e:
        print("Error en API de tienda:", e)

    return pares


def candidatos_html(app_id):
    pares = []

    try:
        r = http.get(
            f"{STEAM_STORE_URL}/app/{app_id}/",
            params={"l": "english"},
            cookies=COOKIES_EDAD,
            timeout=10,
        )
        r.raise_for_status()
        html = r.text

        patron = re.compile(
            rf"https?://[\w.\-]+/store_item_assets/steam/apps/{app_id}/"
            rf"[^\s\"'<>)\\]+"
        )

        vistas = set()
        for m in patron.finditer(html):
            url = m.group(0).split("?")[0]
            if url in vistas:
                continue
            vistas.add(url)

            archivo = url.rsplit("/", 1)[-1].lower()
            clave = ARCHIVO_A_CLAVE.get(archivo)
            if clave:
                pares.append((clave, url))

        # Icono de comunidad (hash de 40 caracteres)
        patron_icono = re.compile(
            rf"community_assets/images/apps/{app_id}/([0-9a-f]{{40}})\.(jpg|png|ico)"
        )
        for m in patron_icono.finditer(html):
            for host in ICON_HOSTS:
                pares.append(
                    ("community_icon", f"{host}/{app_id}/{m.group(1)}.{m.group(2)}")
                )

    except Exception as e:
        print("Error leyendo HTML de la tienda:", e)

    return pares


def candidatos_steamcmd(app_id):
    """appinfo público: iconos (comunidad y cliente) y library assets."""
    pares = []

    try:
        r = http.get(f"https://api.steamcmd.net/v1/info/{app_id}", timeout=8)
        r.raise_for_status()

        common = r.json()["data"][str(app_id)].get("common", {}) or {}

        icon = common.get("icon")
        if isinstance(icon, str) and icon:
            for host in ICON_HOSTS:
                pares.append(("community_icon", f"{host}/{app_id}/{icon}.jpg"))

        clienticon = common.get("clienticon")
        if isinstance(clienticon, str) and clienticon:
            for host in ICON_HOSTS:
                pares.append(("client_icon", f"{host}/{app_id}/{clienticon}.ico"))

        biblioteca = common.get("library_assets_full", {}) or {}

        mapa = [
            ("library_capsule", "portada"),
            ("library_hero", "fondo"),
            ("library_logo", "logo_steam"),
            ("library_header", "portada_amplia"),
        ]

        for nombre, clave in mapa:
            bloque = biblioteca.get(nombre) or {}

            for sub in ("image2x", "image"):
                valores = bloque.get(sub) or {}
                if not isinstance(valores, dict):
                    continue

                valor = valores.get("english") or next(
                    (v for v in valores.values() if isinstance(v, str)), None
                )

                if isinstance(valor, str) and valor:
                    pares.append(
                        (clave, f"{STEAM_ASSETS_BASE}steam/apps/{app_id}/{valor}")
                    )

    except Exception as e:
        print("Error consultando appinfo:", e)

    return pares


def candidatos_fijos(app_id):
    """Rutas clásicas (sin hash) en varios hosts. Último recurso."""
    pares = []

    archivos = [
        ("portada", "library_600x900_2x.jpg"),
        ("portada", "library_600x900.jpg"),
        ("portada_amplia", "header.jpg"),
        ("fondo", "library_hero_2x.jpg"),
        ("fondo", "library_hero.jpg"),
        ("logo_steam", "logo.png"),
        ("capsule_main", "capsule_616x353.jpg"),
        ("capsule_small", "capsule_231x87.jpg"),
        ("hero_capsule", "hero_capsule.jpg"),
    ]

    for clave, archivo in archivos:
        pares.append(
            (clave, f"{STEAM_ASSETS_BASE}steam/apps/{app_id}/{archivo}")
        )
        for host in STEAM_CDN_HOSTS[1:]:
            pares.append((clave, f"https://{host}/steam/apps/{app_id}/{archivo}"))

    return pares


def resolver_assets_steam(app_id):
    """Devuelve {clave: url_valida} con los assets originales de Steam."""

    fuentes = [candidatos_api, candidatos_html, candidatos_steamcmd]

    # Las 3 fuentes se consultan en paralelo, pero se fusionan en orden
    with ThreadPoolExecutor(max_workers=3) as pool:
        futuros = [pool.submit(f, app_id) for f in fuentes]
        pares = []
        for futuro in futuros:
            pares += futuro.result()

    pares += candidatos_fijos(app_id)

    candidatos = {}
    for clave, url in pares:
        lista = candidatos.setdefault(clave, [])
        if url not in lista:
            lista.append(url)

    claves = list(candidatos)

    # Cada asset prueba sus candidatas en orden (se detiene en la primera
    # válida); los assets entre sí se comprueban en paralelo.
    with ThreadPoolExecutor(max_workers=16) as pool:
        encontradas = list(
            pool.map(lambda c: primera_valida(candidatos[c]), claves)
        )

    resultado = {c: u for c, u in zip(claves, encontradas) if u}

    print("Assets de Steam encontrados:", sorted(resultado))
    return resultado


# ============================================================
# OBTENER TODOS LOS ASSETS
# ============================================================

def obtener_urls_assets(app_id, sgdb):
    """
    Portada, portada amplia y fondo: primero Steam, luego SteamGridDB.
    Logo: SteamGridDB. Icono: SteamGridDB, luego iconos de Steam.
    Además se añaden todos los demás assets originales de Steam.

    `sgdb` es un dict de futuros con las consultas ya lanzadas a
    SteamGridDB (grids_portada, grids_amplia, heroes, logos, icons).
    """

    steam = resolver_assets_steam(app_id) if app_id else {}
    assets = {}

    def poner(clave, nombre, url, origen, tipo="imagen"):
        if url:
            assets[clave] = {
                "nombre": nombre,
                "url": url,
                "tipo": tipo,
                "origen": origen,
            }

    def elegir(clave, nombre, futuro):
        if steam.get(clave):
            poner(clave, nombre, steam[clave], "Steam")
        else:
            poner(clave, nombre, primer_url_sgdb(sgdb[futuro].result()),
                  "SteamGridDB")

    elegir("portada", "Portada", "grids_portada")
    elegir("portada_amplia", "Portada amplia", "grids_amplia")
    elegir("fondo", "Fondo", "heroes")

    # ---------------- LOGO (SteamGridDB) ----------------
    poner("logo", "Logo", primer_url_sgdb(sgdb["logos"].result()), "SteamGridDB")

    # ---------------- ICONO ----------------
    iconos = sgdb["icons"].result()

    if iconos:
        oficiales = [i for i in iconos if i.get("style") == "official"]
        icono = (oficiales or iconos)[0]
        poner("icono", "Icono", icono.get("url") or icono.get("thumb"),
              "SteamGridDB", "icono")
    elif steam.get("community_icon") or steam.get("client_icon"):
        poner("icono", "Icono",
              steam.get("community_icon") or steam.get("client_icon"),
              "Steam", "icono")

    # ---------------- EXTRAS ORIGINALES DE STEAM ----------------
    for clave, url in steam.items():
        if clave in ("portada", "portada_amplia", "fondo"):
            continue
        poner(clave, clave, url, "Steam")

    return assets


# ============================================================
# DESCARGA NORMAL
# ============================================================

def descargar_archivo(clave, titulo, url):

    if not url:
        messagebox.showerror("Error", "No hay una URL disponible.")
        return

    try:
        contenido, content_type = descargar_bytes(url)

        if "png" in content_type:
            extension = ".png"
        elif "webp" in content_type:
            extension = ".webp"
        elif "jpeg" in content_type or "jpg" in content_type:
            extension = ".jpg"
        elif "ico" in content_type:
            extension = ".ico"
        else:
            extension = os.path.splitext(url.split("?")[0])[1] or ".png"

        archivo = filedialog.asksaveasfilename(
            title=f"Guardar {titulo}",
            defaultextension=extension,
            initialfile=f"{nombre_abreviado(nombre_juego_actual)}_{clave}{extension}",
            filetypes=[("Imagen", f"*{extension}"), ("Todos los archivos", "*.*")],
        )

        if not archivo:
            return

        with open(archivo, "wb") as f:
            f.write(contenido)

        messagebox.showinfo("Descarga completada", f"{titulo} guardado correctamente.")

    except Exception as e:
        messagebox.showerror("Error", f"No se pudo descargar {titulo}:\n\n{e}")


# ============================================================
# DESCARGA RÁPIDA
# ============================================================

def descarga_rapida(tipo):

    if tipo == "cover":
        asset = assets_actuales.get("portada")
        carpeta = QUICK_COVER_DIR
        sufijo = "_cover.jpg"

    elif tipo == "icon":
        asset = assets_actuales.get("icono")
        carpeta = QUICK_ICON_DIR
        sufijo = "_icon.jpg"

    else:
        return

    if not asset or not asset.get("url"):
        return

    try:
        os.makedirs(carpeta, exist_ok=True)

        archivo = os.path.join(carpeta, nombre_abreviado(nombre_juego_actual) + sufijo)

        contenido, _ = descargar_bytes(asset["url"])
        imagen = Image.open(BytesIO(contenido))

        if imagen.mode in ("RGBA", "LA", "P"):
            imagen = imagen.convert("RGBA")
            fondo = Image.new("RGB", imagen.size, "white")
            fondo.paste(imagen, mask=imagen.getchannel("A"))
            imagen = fondo
        else:
            imagen = imagen.convert("RGB")

        imagen.save(archivo, "JPEG", quality=95)

        estado_var.set(f"Guardado: {archivo}")
        print(f"Descarga rápida: {archivo}")

    except Exception as e:
        estado_var.set(f"Error en descarga rápida: {e}")
        print(f"Error en descarga rápida: {e}")


# ============================================================
# ICONO 64x64 PNG
# ============================================================

def descargar_icono_64():

    asset = assets_actuales.get("icono")

    if not asset:
        messagebox.showerror("Error", "No hay un icono disponible.")
        return

    try:
        contenido, _ = descargar_bytes(asset["url"])

        imagen = Image.open(BytesIO(contenido)).convert("RGBA")
        imagen = imagen.resize((64, 64), Image.Resampling.LANCZOS)

        archivo = filedialog.asksaveasfilename(
            title="Guardar icono 64x64 PNG",
            defaultextension=".png",
            initialfile=f"{nombre_abreviado(nombre_juego_actual)}_icon_64x64.png",
            filetypes=[("PNG", "*.png")],
        )

        if not archivo:
            return

        imagen.save(archivo, "PNG")

        messagebox.showinfo("Icono generado", "Icono 64x64 PNG guardado correctamente.")

    except Exception as e:
        messagebox.showerror("Error", f"No se pudo generar el icono:\n\n{e}")


# ============================================================
# PREVIEWS (se preparan en hilos; PhotoImage se crea en la interfaz)
# ============================================================

def preparar_preview(url, ancho, alto):
    """Descarga la imagen y devuelve una miniatura PIL (o None)."""

    if not url:
        return None

    urls = [url]

    # Si es una URL del CDN de Steam, probar hosts alternativos
    for host in STEAM_CDN_HOSTS:
        if host in url:
            urls += [url.replace(host, otro) for otro in STEAM_CDN_HOSTS if otro != host]
            break

    for intento in urls:
        try:
            contenido, _ = descargar_bytes(intento, timeout=15)

            imagen = Image.open(BytesIO(contenido))

            # Los JPG grandes se decodifican a menor resolución (mucho más rápido)
            if imagen.format == "JPEG":
                imagen.draft("RGB", (ancho * 2, alto * 2))

            imagen = imagen.convert("RGBA")
            imagen.thumbnail((ancho, alto), Image.Resampling.LANCZOS)

            return imagen

        except Exception as e:
            print(f"No funcionó {intento}: {e}")

    return None


# ============================================================
# TARJETAS
# ============================================================

def crear_card(parent, titulo, asset_key, ancho, alto):

    asset = assets_actuales.get(asset_key)

    if not asset or not asset.get("url"):
        return

    url = asset["url"]

    card = tk.Frame(parent, bd=1, relief="solid", padx=10, pady=10)
    card.pack(side="left", padx=10, pady=10, anchor="n")

    tk.Label(card, text=titulo, font=("Segoe UI", 11, "bold")).pack(pady=(0, 2))

    tk.Label(
        card,
        text=f"Origen: {asset.get('origen', '?')}",
        font=("Segoe UI", 8),
        fg="#666666",
    ).pack(pady=(0, 6))

    preview_frame = tk.Frame(card, width=ancho, height=alto)
    preview_frame.pack()
    preview_frame.pack_propagate(False)

    miniatura = previews_actuales.get(asset_key)

    if miniatura is not None:
        foto = ImageTk.PhotoImage(miniatura)
        imagenes_preview.append(foto)
        tk.Label(preview_frame, image=foto).pack(expand=True)
    else:
        tk.Label(preview_frame, text="No disponible").pack(expand=True)

    tk.Button(
        card,
        text="Descargar",
        command=lambda: descargar_archivo(asset_key, titulo, url),
    ).pack(pady=(8, 0), fill="x")

    if asset_key == "portada":
        tk.Button(
            card,
            text="Descarga rápida",
            command=lambda: descarga_rapida("cover"),
        ).pack(pady=(5, 0), fill="x")

    if asset_key == "icono":
        tk.Button(
            card,
            text="Descarga rápida",
            command=lambda: descarga_rapida("icon"),
        ).pack(pady=(5, 0), fill="x")

        tk.Button(
            card,
            text="Guardar 64×64 PNG",
            command=descargar_icono_64,
        ).pack(pady=(5, 0), fill="x")


# ============================================================
# IMAGEN LATERAL 71x877
#
# Fondo #171a21, título en blanco, centrado, girado a la
# izquierda (90° antihorario) y con la fuente Microsoft Yi Baiti.
#
# - El texto se escala para ocupar el máximo posible dejando
#   30 px arriba y abajo (los bordes horizontales de la imagen).
# - También se limita el grosor del texto para que no toque los
#   bordes laterales (71 px de ancho).
# - Yi Baiti solo contiene caracteres latinos y yi. Para chino,
#   japonés, coreano, etc. se usa una fuente de respaldo carácter
#   por carácter, para que no salgan cuadros vacíos.
# ============================================================

SIDE_WIDTH = 71
SIDE_HEIGHT = 877
SIDE_BG = "#171a21"
SIDE_MARGEN_LARGO = 8   # margen arriba y abajo
SIDE_MARGEN_ANCHO = 15    # margen a izquierda y derecha

FONTS_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

FUENTE_PRINCIPAL = "msyi.ttf"  # Microsoft Yi Baiti

FUENTES_RESPALDO = [
    "msyh.ttc",       # Microsoft YaHei (chino simplificado)
    "msjh.ttc",       # Microsoft JhengHei (chino tradicional)
    "YuGothM.ttc",    # Yu Gothic (japonés)
    "meiryo.ttc",     # Meiryo (japonés)
    "malgun.ttf",     # Malgun Gothic (coreano)
    "simsun.ttc",
    "segoeui.ttf",
    "seguisym.ttf",
    "arial.ttf",
    "DejaVuSans.ttf",  # por si se ejecuta fuera de Windows
]

_cache_fuentes = {}
_cache_glyphs = {}
_aviso_fuente = False
_lock_fuentes = threading.Lock()


def cargar_fuente(nombre, tamano):
    clave = (nombre, tamano)

    if clave in _cache_fuentes:
        return _cache_fuentes[clave]

    fuente = None

    for ruta in (os.path.join(FONTS_DIR, nombre), nombre):
        try:
            fuente = ImageFont.truetype(ruta, tamano)
            break
        except Exception:
            continue

    _cache_fuentes[clave] = fuente
    return fuente


def _dibujar_caracter(fuente, ch):
    img = Image.new("L", (100, 100), 0)
    ImageDraw.Draw(img).text((20, 20), ch, font=fuente, fill=255)
    return img


def tiene_glyph(nombre_fuente, ch):
    """True si la fuente tiene un glyph real para el carácter."""
    if ch.isspace():
        return True

    clave = (nombre_fuente, ch)
    if clave in _cache_glyphs:
        return _cache_glyphs[clave]

    fuente = cargar_fuente(nombre_fuente, 32)
    resultado = False

    if fuente is not None:
        try:
            img = _dibujar_caracter(fuente, ch)
            notdef = _dibujar_caracter(fuente, "\uffff")

            resultado = (
                img.getbbox() is not None
                and img.tobytes() != notdef.tobytes()
            )
        except Exception:
            resultado = False

    _cache_glyphs[clave] = resultado
    return resultado


def fuente_para_caracter(ch):
    for nombre in [FUENTE_PRINCIPAL] + FUENTES_RESPALDO:
        if cargar_fuente(nombre, 32) is not None and tiene_glyph(nombre, ch):
            return nombre

    return FUENTE_PRINCIPAL


def dividir_en_tramos(texto):
    """Agrupa caracteres consecutivos que usan la misma fuente."""
    tramos = []

    for ch in texto:
        nombre = fuente_para_caracter(ch)

        if tramos and tramos[-1][0] == nombre:
            tramos[-1][1] += ch
        else:
            tramos.append([nombre, ch])

    return tramos


def render_texto_mask(tramos, tamano):
    """Dibuja el texto horizontal y devuelve una máscara recortada a la tinta."""
    partes = []

    for nombre, texto in tramos:
        fuente = cargar_fuente(nombre, tamano)
        if fuente is not None:
            partes.append((fuente, texto))

    if not partes:
        return None

    ascent = max(f.getmetrics()[0] for f, _ in partes)
    descent = max(f.getmetrics()[1] for f, _ in partes)

    ancho = int(sum(f.getlength(t) for f, t in partes)) + tamano * 2
    alto = ascent + descent + tamano

    img = Image.new("L", (ancho, alto), 0)
    dibujo = ImageDraw.Draw(img)

    x = tamano
    y = tamano // 2 + ascent

    for fuente, texto in partes:
        dibujo.text((x, y), texto, font=fuente, fill=255, anchor="ls")
        x += fuente.getlength(texto)

    bbox = img.getbbox()
    return img.crop(bbox) if bbox else None


def generar_imagen_side(titulo):
    """Genera la imagen lateral 71x877 con el título del juego."""

    global _aviso_fuente

    with _lock_fuentes:

        if not _aviso_fuente and cargar_fuente(FUENTE_PRINCIPAL, 32) is None:
            _aviso_fuente = True
            print(
                f"Aviso: no se encontró {FUENTE_PRINCIPAL} (Microsoft Yi Baiti). "
                "Se usará una fuente de respaldo."
            )

        titulo = " ".join((titulo or "").split()) or "?"
        tramos = dividir_en_tramos(titulo)

        max_largo = SIDE_HEIGHT - 2 * SIDE_MARGEN_LARGO   # 817 px
        max_grosor = SIDE_WIDTH - 2 * SIDE_MARGEN_ANCHO   # 55 px

        # Búsqueda binaria del mayor tamaño de fuente que cabe
        bajo, alto = 6, 300
        mejor = None

        while bajo <= alto:
            medio = (bajo + alto) // 2
            mascara = render_texto_mask(tramos, medio)

            if (
                mascara is not None
                and mascara.width <= max_largo
                and mascara.height <= max_grosor
            ):
                mejor = mascara
                bajo = medio + 1
            else:
                alto = medio - 1

        # Título larguísimo: ni con 6 px cabe, se reduce la imagen
        if mejor is None:
            mascara = render_texto_mask(tramos, 6)

            if mascara is None:
                mascara = Image.new("L", (10, 10), 0)

            factor = min(max_largo / mascara.width, max_grosor / mascara.height, 1)
            mejor = mascara.resize(
                (max(1, int(mascara.width * factor)),
                 max(1, int(mascara.height * factor))),
                Image.Resampling.LANCZOS,
            )

    # Girar a la izquierda (90° antihorario): el texto se lee de abajo hacia arriba
    rotado = mejor.transpose(Image.Transpose.ROTATE_270)

    imagen = Image.new("RGB", (SIDE_WIDTH, SIDE_HEIGHT), SIDE_BG)

    x = (SIDE_WIDTH - rotado.width) // 2
    y = (SIDE_HEIGHT - rotado.height) // 2

    imagen.paste(Image.new("RGB", rotado.size, "white"), (x, y), rotado)

    return imagen


def descarga_rapida_side():

    if side_imagen_actual is None:
        return

    try:
        os.makedirs(QUICK_SIDE_DIR, exist_ok=True)

        archivo = os.path.join(
            QUICK_SIDE_DIR, nombre_abreviado(nombre_juego_actual) + "_side.png"
        )

        side_imagen_actual.save(archivo, "PNG")

        estado_var.set(f"Guardado: {archivo}")
        print(f"Descarga rápida: {archivo}")

    except Exception as e:
        estado_var.set(f"Error en descarga rápida: {e}")
        print(f"Error en descarga rápida: {e}")


def crear_card_side(parent):

    if side_imagen_actual is None:
        return

    card = tk.Frame(parent, bd=1, relief="solid", padx=10, pady=10)
    card.pack(side="left", padx=10, pady=10, anchor="n")

    tk.Label(
        card,
        text=f"Imagen lateral · {SIDE_WIDTH}×{SIDE_HEIGHT}",
        font=("Segoe UI", 11, "bold"),
    ).pack(pady=(0, 6))

    preview = ImageTk.PhotoImage(side_imagen_actual)
    imagenes_preview.append(preview)

    tk.Label(card, image=preview, bd=1, relief="solid").pack()

    tk.Button(
        card,
        text="Descarga rápida",
        command=descarga_rapida_side,
    ).pack(pady=(8, 0), fill="x")


# ============================================================
# MOSTRAR ASSETS
# ============================================================

# (título, clave, ancho preview, alto preview)
FILAS = [
    [
        ("Portada", "portada", 260, 390),
        ("Portada amplia · 920×430", "portada_amplia", 500, 235),
    ],
    [
        ("Fondo / Library Hero · 3840×1240", "fondo", 600, 195),
        ("Logo", "logo", 400, 180),
    ],
    [
        ("Icono", "icono", 220, 220),
        ("Icono de comunidad", "community_icon", 184, 184),
        ("Icono de cliente (.ico)", "client_icon", 128, 128),
    ],
    [
        ("Cápsula principal · 616×353", "capsule_main", 400, 229),
        ("Cápsula pequeña · 231×87", "capsule_small", 231, 87),
        ("Hero capsule · 748×896", "hero_capsule", 260, 312),
    ],
    [
        ("Fondo de página", "page_bg", 500, 281),
        ("Logo original de Steam", "logo_steam", 400, 180),
    ],
]

TAMANOS_PREVIEW = {
    clave: (ancho, alto)
    for fila in FILAS
    for _, clave, ancho, alto in fila
}


def tamano_preview(clave):
    return TAMANOS_PREVIEW.get(clave, (300, 200))


def limpiar_panel():
    for widget in assets_frame.winfo_children():
        widget.destroy()

    imagenes_preview.clear()


def dibujar_resultado():
    """Construye las tarjetas con los datos ya cargados (hilo de la interfaz)."""

    limpiar_panel()

    # ---------------- FILAS FIJAS ----------------
    for fila in FILAS:
        if not any(clave in assets_actuales for _, clave, _, _ in fila):
            continue

        frame = tk.Frame(assets_frame)
        frame.pack(fill="x", pady=6)

        for titulo, clave, ancho, alto in fila:
            crear_card(frame, titulo, clave, ancho, alto)

    # ---------------- EXTRAS (assets de Steam no previstos) ----------------
    extras = sorted(k for k in assets_actuales if k.startswith("extra_"))

    for i in range(0, len(extras), 3):
        frame = tk.Frame(assets_frame)
        frame.pack(fill="x", pady=6)

        for clave in extras[i:i + 3]:
            crear_card(frame, f"Steam · {clave[6:]}", clave, 300, 200)

    # ---------------- IMAGEN LATERAL 71x877 ----------------
    frame_side = tk.Frame(assets_frame)
    frame_side.pack(fill="x", pady=6)
    crear_card_side(frame_side)

    canvas.yview_moveto(0)


def aplicar_datos(datos):
    """Publica los datos de un juego como 'actuales' y los dibuja."""

    global assets_actuales, previews_actuales, nombre_juego_actual
    global app_id_actual, sgdb_id_actual, side_imagen_actual

    nombre_juego_actual = datos["nombre"]
    sgdb_id_actual = datos["sgdb_id"]
    app_id_actual = datos["app_id"]
    assets_actuales = datos["assets"]
    previews_actuales = datos["previews"]
    side_imagen_actual = datos["side"]

    app_id = datos["app_id"]
    sgdb_id = datos["sgdb_id"]

    base_estado = (
        f"Steam App ID: {app_id}  |  SGDB ID: {sgdb_id}"
        if app_id
        else f"Sin Steam App ID (solo SteamGridDB)  |  SGDB ID: {sgdb_id}"
    )

    if assets_actuales:
        estado_var.set(base_estado + f"  |  {len(assets_actuales)} assets")
    else:
        estado_var.set(base_estado + "  |  No se encontraron imágenes.")

    dibujar_resultado()


# ============================================================
# CARGA EN SEGUNDO PLANO
# ============================================================

def cargar_datos_juego(juego, mi_id):
    """
    Se ejecuta en un hilo. Hace TODA la red y el procesado de imágenes
    en paralelo y devuelve un dict con los resultados. No toca Tk.
    """

    nombre = juego.get("name", "Desconocido")
    sgdb_id = juego.get("id")

    def estado(texto):
        ui(lambda: estado_var.set(texto) if mi_id == carga_id else None)

    with ThreadPoolExecutor(max_workers=12) as pool:

        # Todo lo de SteamGridDB y la imagen lateral arrancan ya, a la vez
        sgdb = {
            "grids_portada": pool.submit(
                sgdb_por_juego, "grids", sgdb_id, {"dimensions": "600x900"}),
            "grids_amplia": pool.submit(
                sgdb_por_juego, "grids", sgdb_id,
                {"dimensions": "920x430,460x215"}),
            "heroes": pool.submit(sgdb_por_juego, "heroes", sgdb_id),
            "logos": pool.submit(sgdb_por_juego, "logos", sgdb_id),
            "icons": pool.submit(sgdb_por_juego, "icons", sgdb_id),
        }

        f_side = pool.submit(generar_imagen_side, nombre)

        # Steam App ID: desde SteamGridDB, o por nombre como respaldo
        estado("Buscando Steam App ID...")
        app_id = obtener_steam_app_id_sgdb(sgdb_id) or buscar_en_steam(nombre)

        estado(
            (f"Steam App ID: {app_id}  |  " if app_id else "Sin Steam App ID  |  ")
            + "Buscando assets..."
        )

        assets = obtener_urls_assets(app_id, sgdb)

        # Todas las miniaturas se descargan y preparan a la vez
        estado(f"Descargando {len(assets)} previews...")

        futuros = {
            clave: pool.submit(preparar_preview, asset["url"], *tamano_preview(clave))
            for clave, asset in assets.items()
        }

        previews = {clave: f.result() for clave, f in futuros.items()}
        side = f_side.result()

    return {
        "nombre": nombre,
        "sgdb_id": sgdb_id,
        "app_id": app_id,
        "assets": assets,
        "previews": previews,
        "side": side,
    }


def trabajo_carga(juego, mi_id):

    try:
        datos = cargar_datos_juego(juego, mi_id)

    except Exception as e:
        traceback.print_exc()
        mensaje = f"Error cargando el juego: {e}"
        ui(lambda: estado_var.set(mensaje) if mi_id == carga_id else None)
        return

    def terminar():
        # Si el usuario ya eligió otro juego, este resultado se descarta
        if mi_id != carga_id:
            return

        cache_juegos[datos["sgdb_id"]] = datos
        aplicar_datos(datos)

    ui(terminar)


def mostrar_assets(juego):

    global carga_id

    carga_id += 1
    mi_id = carga_id

    sgdb_id = juego.get("id")

    # Si ya se cargó antes, se muestra al instante
    if sgdb_id in cache_juegos:
        aplicar_datos(cache_juegos[sgdb_id])
        return

    limpiar_panel()
    estado_var.set("Cargando...")

    threading.Thread(
        target=trabajo_carga,
        args=(juego, mi_id),
        daemon=True,
    ).start()


# ============================================================
# EVENTOS
# ============================================================

def seleccionar_juego(event):

    seleccion = lista.curselection()

    if not seleccion:
        return

    juego = juegos_encontrados[seleccion[0]]

    nombre_seleccionado_var.set(juego.get("name", "Desconocido"))

    mostrar_assets(juego)


def trabajo_busqueda(texto, mi_id):

    try:
        resultados = buscar_juegos(texto)
        error = None
    except Exception as e:
        resultados = []
        error = e

    def terminar():
        global juegos_encontrados

        if mi_id != busqueda_id:
            return

        if error is not None:
            estado_var.set("Error al buscar.")
            messagebox.showerror(
                "Error", f"No se pudo buscar en SteamGridDB:\n\n{error}"
            )
            return

        juegos_encontrados = resultados

        if not juegos_encontrados:
            estado_var.set("No se encontraron juegos.")
            return

        for juego in juegos_encontrados:
            lista.insert(tk.END, juego.get("name", "Desconocido"))

        estado_var.set(f"{len(juegos_encontrados)} juegos encontrados.")

    ui(terminar)


def buscar():

    global busqueda_id, carga_id

    texto = entrada.get().strip()

    if not texto:
        return

    busqueda_id += 1
    carga_id += 1          # cancela cualquier carga de juego en curso

    lista.delete(0, tk.END)
    limpiar_panel()

    estado_var.set("Buscando...")

    threading.Thread(
        target=trabajo_busqueda,
        args=(texto, busqueda_id),
        daemon=True,
    ).start()


# ============================================================
# INTERFAZ
# ============================================================

app = tk.Tk()
app.title("SteamGrid Tool")
app.geometry("1450x950")
app.minsize(1100, 750)

# ---------------- CABECERA ----------------

top = tk.Frame(app, padx=10, pady=10)
top.pack(fill="x")

entrada = tk.Entry(top, font=("Segoe UI", 12))
entrada.pack(side="left", fill="x", expand=True)
entrada.bind("<Return>", lambda event: buscar())

tk.Button(top, text="Buscar", command=buscar, width=12).pack(side="left", padx=8)

# ---------------- CONTENEDOR PRINCIPAL ----------------

main = tk.Frame(app)
main.pack(fill="both", expand=True)

# ---------------- LISTA ----------------

left = tk.Frame(main, width=300, padx=10, pady=10)
left.pack(side="left", fill="y")
left.pack_propagate(False)

tk.Label(left, text="Juegos encontrados", font=("Segoe UI", 12, "bold")).pack(
    anchor="w", pady=(0, 5)
)

lista = tk.Listbox(left, font=("Segoe UI", 11), exportselection=False)
lista.pack(fill="both", expand=True)
lista.bind("<<ListboxSelect>>", seleccionar_juego)

# ---------------- DERECHA ----------------

right = tk.Frame(main, padx=10, pady=10)
right.pack(side="left", fill="both", expand=True)

nombre_seleccionado_var = tk.StringVar(value="Selecciona un juego")

tk.Label(
    right,
    textvariable=nombre_seleccionado_var,
    font=("Segoe UI", 16, "bold"),
).pack(anchor="w", pady=(0, 5))

estado_var = tk.StringVar(value="Busca un juego para comenzar.")

tk.Label(right, textvariable=estado_var, font=("Segoe UI", 9)).pack(
    anchor="w", pady=(0, 10)
)

# ---------------- SCROLL ----------------

canvas = tk.Canvas(right)

scrollbar = tk.Scrollbar(right, orient="vertical", command=canvas.yview)
canvas.configure(yscrollcommand=scrollbar.set)

scrollbar.pack(side="right", fill="y")
canvas.pack(side="left", fill="both", expand=True)

assets_frame = tk.Frame(canvas)

canvas_window = canvas.create_window((0, 0), window=assets_frame, anchor="nw")


def actualizar_scroll(event=None):
    canvas.configure(scrollregion=canvas.bbox("all"))


def ajustar_ancho(event):
    canvas.itemconfig(canvas_window, width=event.width)


assets_frame.bind("<Configure>", actualizar_scroll)
canvas.bind("<Configure>", ajustar_ancho)


def scroll_mouse(event):
    canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


canvas.bind_all("<MouseWheel>", scroll_mouse)


# ============================================================
# CIERRE
#
# Los hilos de red pueden tardar unos segundos en terminar; al
# cerrar la ventana se sale de inmediato para que no quede el
# proceso colgado en segundo plano.
# ============================================================

def cerrar():
    try:
        app.destroy()
    finally:
        os._exit(0)


app.protocol("WM_DELETE_WINDOW", cerrar)

# ============================================================
# INICIAR
# ============================================================

app.after(40, procesar_cola)
app.mainloop()