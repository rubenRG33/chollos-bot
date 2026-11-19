#!/usr/bin/env python3
"""Bot de chollos para compraventa.

Vigila anuncios nuevos de Wallapop cerca de tu casa, calcula el precio de mercado
con anuncios parecidos y te avisa por Telegram solo si el beneficio estimado
compensa. No compra ni contacta con nadie: decides tú.

Garantías de diseño:
  * Un anuncio solo se marca como "visto" DESPUÉS de que el aviso de Telegram se
    haya enviado. Si Telegram falla, se reintenta en la siguiente ronda.
  * Pausas aleatorias entre peticiones para no saturar Wallapop.
  * Si todas las búsquedas fallan, te avisa (máximo uno cada 12 h).
  * El estado se guarda de forma atómica.

Uso:
  python3 chollos.py --prueba            # busca y muestra, sin Telegram ni guardar nada
  python3 chollos.py --prueba --presupuesto 400   # ver cómo saldrían productos más caros
  python3 chollos.py                     # una ronda
  python3 chollos.py --bucle             # rondas continuas (para el servidor)
"""
import argparse
import html
import fcntl
import http.cookiejar
import json
import logging
import math
import os
import random
import re
import statistics
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
ESTADO = BASE / "estado.json"
MERCADO = BASE / "mercado.json"
WALLAPOP = "https://api.wallapop.com/api/v3/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DIAS_MEMORIA = 14
HORAS_ENTRE_AVISOS_FALLO = 12

log = logging.getLogger("chollos")
_CANDADO = None


# ---------------------------------------------------------------- utilidades
def configurar_log():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fichero = RotatingFileHandler(BASE / "chollos.log", maxBytes=500_000, backupCount=3,
                                  encoding="utf-8")
    fichero.setFormatter(fmt)
    consola = logging.StreamHandler()
    consola.setFormatter(fmt)
    log.addHandler(fichero)
    log.addHandler(consola)


def cargar_env():
    """Lee .env (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) sin pisar variables ya definidas."""
    ruta = BASE / ".env"
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def leer_json(ruta, defecto):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defecto


def guardar_json(ruta, datos):
    """Escritura atómica: nunca deja el archivo a medias."""
    fd, tmp = tempfile.mkstemp(dir=str(ruta.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=1)
        os.replace(tmp, ruta)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def http_json(url, datos=None, cabeceras=None, intentos=3, timeout=25):
    cab = {"User-Agent": UA, "Accept": "application/json",
           "Accept-Language": "es-ES,es;q=0.9", "X-DeviceOS": "0"}
    if cabeceras:
        cab.update(cabeceras)
    cuerpo = None
    if datos is not None:
        cuerpo = json.dumps(datos).encode("utf-8")
        cab["Content-Type"] = "application/json"
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, data=cuerpo, headers=cab)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            ultimo = e
        if i < intentos - 1:
            time.sleep(5 * (i + 1) ** 2)  # 5 s, 20 s
    destino = re.sub(r"/bot[^/]+/", "/bot***/", url.split("?")[0])  # nunca escribas el token
    raise RuntimeError("Fallo de red en %s: %s" % (destino, ultimo))


def pausa(cfg):
    a, b = cfg.get("pausa_segundos", [2, 4])
    time.sleep(random.uniform(a, b))


def distancia_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- Wallapop
def normalizar(raw):
    """Convierte un anuncio de Wallapop a un diccionario simple."""
    loc = raw.get("location") or {}
    imgs = raw.get("images") or []
    foto = None
    if imgs:
        urls = imgs[0].get("urls") or {}
        foto = urls.get("big") or urls.get("medium") or urls.get("small")
    precio = (raw.get("price") or {}).get("amount")
    return {
        "id": raw.get("id"),
        "user_id": raw.get("user_id"),
        "titulo": raw.get("title") or "",
        "desc": raw.get("description") or "",
        "precio": float(precio) if precio is not None else None,
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
        "ciudad": loc.get("city") or "",
        "reservado": bool((raw.get("reserved") or {}).get("flag")),
        "envio": bool((raw.get("shipping") or {}).get("item_is_shippable")),
        "url": "https://es.wallapop.com/item/%s" % raw.get("web_slug", raw.get("id")),
        "foto": foto,
        "creado": (raw.get("created_at") or 0) / 1000.0,
        "categoria": " > ".join(t.get("name", "") for t in raw.get("taxonomy") or []),
    }


def wallapop_buscar(texto, cfg, local=True, token=None):
    u = cfg["ubicacion"]
    p = {"keywords": texto, "source": "search_box", "language": "es_ES",
         "latitude": u["lat"], "longitude": u["lon"]}
    if local:
        p["distance_in_km"] = u["radio_km"]
        p["order_by"] = "newest"
    else:
        p["order_by"] = "most_relevance"
    if token:
        p["next_page"] = token
    d = http_json(WALLAPOP + "?" + urllib.parse.urlencode(p))
    try:
        items = [normalizar(x) for x in d["data"]["section"]["payload"]["items"]]
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError("Wallapop devolvió un formato inesperado (%s)" % e)
    return items, (d.get("meta") or {}).get("next_page")


# ---------------------------------------------------------------- vendedores
def wallapop_vendedor(user_id, cfg):
    """Valoraciones y antigüedad del vendedor. None si no se puede leer."""
    if not user_id:
        return None
    try:
        st = http_json("https://api.wallapop.com/api/v3/users/%s/stats" % user_id)
        pausa(cfg)
        us = http_json("https://api.wallapop.com/api/v3/users/%s" % user_id)
    except (RuntimeError, ValueError, KeyError) as e:
        log.warning("No pude leer el vendedor %s: %s", user_id, e)
        return None
    cont = {c.get("type"): c.get("value", 0) for c in st.get("counters", [])}
    reg = us.get("register_date")
    return {
        "valoraciones": int(cont.get("reviews", 0)),
        "ventas": int(cont.get("sells", 0)),
        "nota": st.get("rating_average"),
        "dias": (time.time() - reg / 1000.0) / 86400 if reg else None,
        "profesional": us.get("type") not in (None, "normal"),
    }


def juzgar_vendedor(v, cfg):
    """'descartar' (sin valoraciones), 'aviso' (pocas o desconocidas) o 'ok'."""
    c = cfg.get("vendedor", {})
    if v is None:
        return "aviso"
    if v.get("profesional"):
        return "ok"
    if v["valoraciones"] < c.get("descartar_si_menos_de", 1):
        return "descartar"
    if v["valoraciones"] < c.get("avisar_si_menos_de", 5):
        return "aviso"
    return "ok"


def linea_vendedor(v):
    if v is None:
        return "👤 No pude comprobar al vendedor: míralo con cuidado."
    partes = ["%d valoraciones" % v["valoraciones"]]
    if v.get("nota"):
        partes.append("%.1f★" % v["nota"])
    if v.get("dias") is not None:
        partes.append("cuenta de hace %d días" % v["dias"] if v["dias"] < 90
                      else "cuenta de hace %d meses" % (v["dias"] // 30))
    return "👤 Vendedor: " + " · ".join(partes)


# ---------------------------------------------------------------- Chollometro (AliExpress, Amazon...)
CHOLLOS_FEEDS = ["https://www.chollometro.com/rss/nuevos", "https://www.chollometro.com/rss/hot"]
NS_PEPPER = {"p": "http://www.pepper.com/rss", "m": "http://search.yahoo.com/mrss/"}


def numero_es(txt):
    """'1.299,50€' -> 1299.5 ; '326,45€' -> 326.45 ; None si no hay precio."""
    t = re.sub(r"[^\d.,]", "", txt or "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def parsear_chollometro(texto):
    raiz = ET.fromstring(texto)
    out = []
    for i in raiz.findall("./channel/item"):
        link = i.findtext("link") or ""
        titulo = re.sub(r"^\d+°\s*-\s*", "", i.findtext("title") or "")
        m = i.find("p:merchant", NS_PEPPER)
        tienda = m.get("name", "") if m is not None else ""
        precio = numero_es(m.get("price")) if m is not None else None
        miniatura = i.find("m:thumbnail", NS_PEPPER)
        try:
            creado = parsedate_to_datetime(i.findtext("pubDate")).timestamp()
        except (TypeError, ValueError):
            creado = 0
        if not link or precio is None:
            continue
        out.append({"id": "c" + link, "titulo": titulo, "desc": "", "precio": precio,
                    "tienda": tienda, "url": link, "foto": miniatura.get("url") if miniatura is not None else None,
                    "creado": creado, "origen": "chollo", "lat": None, "lon": None,
                    "ciudad": "", "reservado": False, "envio": True})
    return out


def leer_chollometro():
    items, ids = [], set()
    for url in CHOLLOS_FEEDS:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                for it in parsear_chollometro(r.read().decode("utf-8", "replace")):
                    if it["id"] not in ids:
                        ids.add(it["id"])
                        items.append(it)
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
            log.warning("Chollometro %s: %s", url, e)
    if not items:
        raise RuntimeError("Chollometro no responde")
    return items


# ---------------------------------------------------------------- Vinted
VINTED = "https://www.vinted.es"
RE_TITULO_VINTED = re.compile(
    r"^(?P<t>.*?)(?:, Marca: .*?)?(?:, Modelo: .*?)?(?:, Estado: (?P<e>[^,]+))?"
    r", (?P<p>\d+(?:\.\d+)?) €(?:, (?P<tot>\d+(?:\.\d+)?) €)?$", re.S)


def vinted_abrir():
    """Abre sesión en Vinted (la web pide cookies de visitante)."""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA), ("Accept-Language", "es-ES,es;q=0.9"),
                     ("Accept", "text/html,application/xhtml+xml")]
    vinted_get(op, VINTED + "/")
    return op


def vinted_get(op, url, intentos=3):
    ultimo = None
    for i in range(intentos):
        try:
            with op.open(url, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            ultimo = e
        if i < intentos - 1:
            time.sleep(5 * (i + 1) ** 2)
    raise RuntimeError("Fallo de red en Vinted %s: %s" % (url.split("?")[0], ultimo))


def parsear_vinted(pagina, cfg):
    """Saca los anuncios de la página de catálogo de Vinted."""
    envio = cfg.get("vinted", {}).get("envio_estimado", 4)
    fotos = {m.group(2): m.group(1) for m in re.finditer(
        r'<img[^>]*?src="([^"]+)"[^>]*?data-testid="product-item-id-(\d+)--image--img"', pagina)}
    items, vistos = [], set()
    for m in re.finditer(r'<a [^>]*data-testid="product-item-id-(\d+)--overlay-link"[^>]*>', pagina):
        iid, tag = m.group(1), m.group(0)
        if iid in vistos:
            continue
        vistos.add(iid)
        href = re.search(r'href="([^"?]+)', tag)
        tit = re.search(r'title="([^"]*)"', tag)
        if not href or not tit:
            continue
        mt = RE_TITULO_VINTED.match(html.unescape(tit.group(1)))
        if not mt:
            continue
        precio = float(mt.group("p"))
        total = float(mt.group("tot") or precio)
        items.append({
            "id": "v" + iid, "titulo": mt.group("t"), "desc": "", "precio": precio,
            "coste_total": total + envio, "estado": mt.group("e") or "",
            "lat": None, "lon": None, "ciudad": "", "reservado": False, "envio": True,
            "url": VINTED + href.group(1), "foto": html.unescape(fotos.get(iid, "")) or None,
            "creado": 0, "origen": "vinted",
        })
    return items


def vinted_buscar(op, texto, cfg):
    p = {"search_text": texto, "order": "newest_first", "currency": "EUR",
         "price_to": int(cfg["presupuesto_max"])}
    return parsear_vinted(vinted_get(op, VINTED + "/catalog?" + urllib.parse.urlencode(p)), cfg)


def vinted_ficha(op, url):
    """Descripción y valoraciones del vendedor, leídas de la ficha del anuncio."""
    pag = vinted_get(op, url)
    m = re.search(r'<meta property="og:description" content="([^"]*)"', pag)
    desc = html.unescape(m.group(1)) if m else ""
    plano = pag.replace('\\"', '"')
    n = re.search(r'"feedback_count":(\d+)', plano)
    r = re.search(r'"feedback_reputation":([\d.]+)', plano)
    vendedor = None
    if n:
        vendedor = {"valoraciones": int(n.group(1)), "ventas": None, "dias": None,
                    "nota": round(float(r.group(1)) * 5, 1) if r else None, "profesional": False}
    return desc, vendedor


# ---------------------------------------------------------------- Cash Converters
CASH = "https://www.cashconverters.es"


def cc_get(url, intentos=3):
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            ultimo = e
        if i < intentos - 1:
            time.sleep(5 * (i + 1) ** 2)
    raise RuntimeError("Fallo de red en Cash Converters: %s" % ultimo)


def parsear_cashconverters(pagina, cfg):
    """Saca los artículos disponibles de una página de búsqueda de Cash Converters."""
    c = cfg.get("cashconverters", {})
    envio, gratis = c.get("envio", 5), c.get("envio_gratis_desde", 50)
    items, vistos = [], set()
    for trozo in pagina.split('<div class="product-tile')[1:]:
        m = re.search(r'data-product-datalayer="([^"]+)"', trozo[:2000])
        if not m or "tile-body available" not in trozo:
            continue  # sin datos o sin stock
        try:
            d = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        pid, precio = d.get("id"), d.get("price")
        if not pid or precio is None or pid in vistos:
            continue
        vistos.add(pid)
        href = re.search(r'class="link" href="([^"]+)"', trozo)
        foto = re.search(r'class="tile-image" src="([^"]+)"', trozo)
        precio = float(precio)
        items.append({
            "id": "k" + pid, "titulo": d.get("name") or "", "desc": "", "precio": precio,
            "coste_total": precio + (0 if precio >= gratis else envio), "estado": d.get("variant") or "",
            "lat": None, "lon": None, "ciudad": "", "reservado": False, "envio": True,
            "url": CASH + html.unescape(href.group(1)) if href else CASH, "foto": html.unescape(foto.group(1)) if foto else None,
            "creado": 0, "origen": "cashconverters",
        })
    return items


def cc_buscar(texto, cfg):
    url = CASH + "/es/es/search/?" + urllib.parse.urlencode({"q": texto, "sz": 96})
    return parsear_cashconverters(cc_get(url), cfg)


# ---------------------------------------------------------------- clasificación
def normalizar_variante(v):
    v = v.lower().replace(" ", "").replace(",", ".")
    return re.sub(r"^series?", "s", v)


def clasificar(prod, it, cfg):
    """Devuelve la variante del producto (p. ej. '128') o None si el anuncio no vale."""
    titulo = it["titulo"].lower()
    if not re.search(prod["patron"], titulo):
        return None
    if it.get("precio") is not None and it["precio"] < prod.get("precio_min", 0):
        return None  # por debajo de un precio realista: juego, accesorio o chatarra
    if prod.get("excluye") and re.search(prod["excluye"], titulo):
        return None
    if re.search(cfg["excluir_titulo"], titulo):
        return None
    if cfg.get("excluir_categoria") and re.search(cfg["excluir_categoria"], it.get("categoria", "")):
        return None  # Wallapop lo clasifica como videojuego, accesorio, funda, etc.
    if re.search(cfg["excluir_defectos"], titulo + " " + it["desc"].lower()):
        return None
    if not prod.get("variante"):
        return "unico"
    m = re.search(prod["variante"], titulo)
    if m:
        for g in m.groups():
            if g:
                return normalizar_variante(g)
    if prod.get("variante_defecto"):
        return prod["variante_defecto"]
    return None


def calcular_mercado(items, prod, cfg):
    """Mediana de precios por variante. Quita valores raros (muy bajos o muy altos)."""
    grupos = {}
    for it in items:
        if it["precio"] is None or it["precio"] < 5 or it["reservado"]:
            continue
        var = clasificar(prod, it, cfg)
        if var is None:
            continue
        grupos.setdefault(var, []).append(it["precio"])
    res = {}
    for var, precios in grupos.items():
        m0 = statistics.median(precios)
        limpios = [p for p in precios if 0.5 * m0 <= p <= 1.8 * m0] or precios
        res[var] = {"mediana": round(statistics.median(limpios), 1), "n": len(limpios)}
    return res


def mercado_producto(prod, cfg, cache, forzar=False):
    """Precios de mercado de un producto, con caché para no consultar siempre."""
    ahora = time.time()
    c = cache.get(prod["nombre"])
    if c and not forzar and ahora - c["t"] < cfg["horas_cache_mercado"] * 3600:
        return c["variantes"]
    items, token = [], None
    for _ in range(3):  # hasta ~120 anuncios de toda España
        pagina, token = wallapop_buscar(prod["buscar"], cfg, local=False, token=token)
        items += pagina
        if not token:
            break
        pausa(cfg)
    variantes = calcular_mercado(items, prod, cfg)
    cache[prod["nombre"]] = {"t": ahora, "variantes": variantes}
    log.info("Mercado %s: %s", prod["nombre"],
             {v: "%s€ (%d)" % (d["mediana"], d["n"]) for v, d in variantes.items()})
    return variantes


def evaluar(it, mer, cfg):
    """Calcula venta estimada y beneficio. mer = {'mediana': x, 'n': n}."""
    venta = mer["mediana"] * cfg["descuento_venta"]
    coste = it.get("coste_total", it["precio"])  # en Vinted incluye protección y envío
    beneficio = venta - coste - cfg.get("costes_extra", 0)
    return {
        "venta": round(venta),
        "beneficio": round(beneficio),
        "n": mer["n"],
        "sospechoso": it["precio"] < 0.5 * mer["mediana"],
    }


# ---------------------------------------------------------------- Telegram
def telegram(metodo, datos):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Falta TELEGRAM_TOKEN (ejecuta configurar.py)")
    return http_json("https://api.telegram.org/bot%s/%s" % (token, metodo), datos=datos)


def texto_aviso(it, prod_nombre, var, ev, dist):
    esc = html.escape
    vinted = it.get("origen") == "vinted"
    nombre = prod_nombre if var in ("unico", "disco", "normal") else "%s (%s)" % (prod_nombre, var)
    chollo = it.get("origen") == "chollo"
    cash = it.get("origen") == "cashconverters"
    if cash:
        precio = "💰 Cuesta <b>%d €</b>%s · venta estimada ≈ <b>%d €</b> (%d anuncios parecidos en Wallapop)" % (
            round(it["precio"]),
            " (≈ %d € con envío)" % round(it["coste_total"]) if it["coste_total"] > it["precio"] else "",
            ev["venta"], ev["n"])
        sitio = ("🏬 Cash Converters · estado: %s · artículo revisado por la tienda%s\n"
                 "🚚 Envío gratis desde 50 € o recogida en la tienda que elijas") % (
            esc(it.get("estado") or "no indicado"),
            " · hay %d unidades en distintas tiendas" % it["unidades"] if it.get("unidades", 1) > 1 else "")
    elif chollo:
        precio = "💰 <b>%d €</b> en %s · venta estimada ≈ <b>%d €</b> (%d anuncios parecidos en Wallapop)" % (
            round(it["precio"]), esc(it.get("tienda") or "tienda"), ev["venta"], ev["n"])
        sitio = "🛒 Oferta online (Chollometro) · compra y espera el envío"
        if "aliexpress" in (it.get("tienda") or "").lower():
            sitio += "\n⏳ AliExpress: el envío puede tardar semanas y la devolución es más difícil."
    elif vinted:
        precio = "💰 Pide <b>%d €</b> (≈ %d € con protección y envío) · venta estimada ≈ <b>%d €</b> (%d anuncios parecidos en Wallapop)" % (
            round(it["precio"]), round(it["coste_total"]), ev["venta"], ev["n"])
        sitio = "📦 Vinted · solo envío · estado: %s" % esc(it.get("estado") or "no indicado")
    else:
        precio = "💰 Pide <b>%d €</b> · venta estimada ≈ <b>%d €</b> (%d anuncios parecidos)" % (
            round(it["precio"]), ev["venta"], ev["n"])
        sitio = "📍 %s · a %.0f km%s" % (esc(it["ciudad"]), dist, " · acepta envío" if it["envio"] else "")
    lineas = [
        "<b>%s</b>" % esc(it["titulo"][:120]),
        "🏷 Detectado como: %s" % esc(nombre),
        precio,
        "📈 Beneficio estimado: <b>~%d €</b>" % ev["beneficio"],
        sitio,
    ]
    if "vendedor" in it:
        lineas.append(linea_vendedor(it["vendedor"]))
    if vinted:
        lineas.append("ℹ️ Vinted no indica dónde está el vendedor: comprueba que sea de España.")
    if ev["sospechoso"]:
        lineas.append("⚠️ Precio muy por debajo del mercado: revisa bien que no sea estafa o defecto.")
    return "\n".join(lineas)


def enviar_aviso(it, texto):
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:
        raise RuntimeError("Falta TELEGRAM_CHAT_ID (ejecuta configurar.py)")
    etiqueta = {"vinted": "Abrir en Vinted", "chollo": "Ver oferta",
                                                       "cashconverters": "Abrir en Cash Converters"}.get(it.get("origen"), "Abrir en Wallapop")
    boton = {"inline_keyboard": [[{"text": etiqueta, "url": it["url"]}]]}
    if it["foto"]:
        try:
            telegram("sendPhoto", {"chat_id": chat, "photo": it["foto"], "caption": texto,
                                   "parse_mode": "HTML", "reply_markup": boton})
            return
        except RuntimeError as e:
            log.warning("sendPhoto falló, envío sin foto: %s", e)
    telegram("sendMessage", {"chat_id": chat, "text": texto, "parse_mode": "HTML",
                             "reply_markup": boton})


def enviar_texto(texto):
    telegram("sendMessage", {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": texto})


# ---------------------------------------------------------------- validación y comprobación
CLAVES_CFG = ["ubicacion", "presupuesto_max", "beneficio_min", "descuento_venta", "max_horas_anuncio",
              "min_comparables", "horas_cache_mercado", "pausa_segundos", "excluir_titulo",
              "excluir_defectos", "productos"]


def aplicar_entorno(cfg):
    """La ubicación puede venir de CHOLLOS_LAT / CHOLLOS_LON / CHOLLOS_RADIO.
    Así no hace falta escribir tu zona en el repositorio público."""
    u = cfg.setdefault("ubicacion", {})
    for var, clave in (("CHOLLOS_LAT", "lat"), ("CHOLLOS_LON", "lon"), ("CHOLLOS_RADIO", "radio_km")):
        v = os.environ.get(var)
        if v:
            try:
                u[clave] = float(v)
            except ValueError:
                sys.exit("%s no es un número: %r" % (var, v))
    return cfg


def validar_config(cfg):
    """Devuelve una lista de problemas de config.json (vacía si todo está bien)."""
    errores = []
    for k in CLAVES_CFG:
        if k not in cfg:
            errores.append("falta '%s'" % k)
    if errores:
        return errores
    for k in ("ubicacion",):
        for sub in ("lat", "lon", "radio_km"):
            if sub not in cfg[k]:
                errores.append("falta ubicacion.%s" % sub)
    if not errores and not cfg["ubicacion"]["lat"] and not cfg["ubicacion"]["lon"]:
        errores.append("falta tu ubicación: define CHOLLOS_LAT y CHOLLOS_LON (en .env o en los secretos)")
    for k in ("presupuesto_max", "beneficio_min", "min_comparables"):
        if not isinstance(cfg[k], (int, float)) or cfg[k] <= 0:
            errores.append("'%s' debe ser un número mayor que 0" % k)
    if not 0 < cfg["descuento_venta"] <= 1:
        errores.append("'descuento_venta' debe estar entre 0 y 1")
    for k in ("excluir_titulo", "excluir_defectos"):
        try:
            re.compile(cfg[k])
        except re.error as e:
            errores.append("regex mala en '%s': %s" % (k, e))
    nombres = set()
    for pr in cfg["productos"]:
        n = pr.get("nombre", "?")
        if n in nombres:
            errores.append("producto repetido: %s" % n)
        nombres.add(n)
        for k in ("nombre", "buscar", "patron"):
            if not pr.get(k):
                errores.append("%s: falta '%s'" % (n, k))
        for k in ("patron", "excluye", "variante"):
            if pr.get(k):
                try:
                    re.compile(pr[k])
                except re.error as e:
                    errores.append("%s: regex mala en '%s': %s" % (n, k, e))
    return errores


def comprobar():
    """Prueba de arranque: config, Telegram, Wallapop, Vinted y Chollometro. No envía avisos."""
    cfg = leer_json(CONFIG, None)
    if cfg is not None:
        aplicar_entorno(cfg)
    fallos = 0

    def paso(nombre, fn):
        nonlocal fallos
        try:
            print("  ✔ %-26s %s" % (nombre, fn() or ""))
        except Exception as e:  # noqa: BLE001 - queremos ver cualquier fallo
            fallos += 1
            print("  ✘ %-26s %s" % (nombre, str(e)[:160]))

    def p_python():
        if sys.version_info < (3, 8):
            raise RuntimeError("Hace falta Python 3.8 o superior")
        return "Python %d.%d" % sys.version_info[:2]

    def p_config():
        if cfg is None:
            raise RuntimeError("no se puede leer config.json")
        e = validar_config(cfg)
        if e:
            raise RuntimeError("; ".join(e))
        return "%d productos" % len(cfg["productos"])

    def p_env():
        if not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
            raise RuntimeError("faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (ejecuta configurar.py)")
        return ".env completo"

    def p_telegram():
        return "@" + telegram("getMe", None)["result"]["username"]

    def p_escritura():
        guardar_json(BASE / ".prueba_escritura.json", {"ok": 1})
        (BASE / ".prueba_escritura.json").unlink()
        return "puedo guardar estado"

    def p_wallapop():
        items, _ = wallapop_buscar("iphone", cfg, local=True)
        if not items:
            raise RuntimeError("0 anuncios")
        return "%d anuncios" % len(items)

    def p_vendedor():
        items, _ = wallapop_buscar("iphone", cfg, local=True)
        v = wallapop_vendedor(items[0]["user_id"], cfg)
        if v is None:
            raise RuntimeError("no pude leer el vendedor")
        return "%d valoraciones" % v["valoraciones"]

    def p_vinted():
        op = vinted_abrir()
        items = vinted_buscar(op, "iphone", cfg)
        if not items:
            raise RuntimeError("0 anuncios (¿bloqueo?)")
        _, v = vinted_ficha(op, items[0]["url"])
        if v is None:
            raise RuntimeError("anuncios ok pero no leo al vendedor")
        return "%d anuncios, vendedor legible" % len(items)

    def p_cashconverters():
        items = cc_buscar("iphone", cfg)
        if not items:
            raise RuntimeError("0 artículos (¿bloqueo?)")
        return "%d artículos" % len(items)

    def p_chollos():
        return "%d ofertas" % len(leer_chollometro())

    print("Comprobación del bot de chollos:")
    paso("Python", p_python)
    paso("config.json", p_config)
    paso(".env", p_env)
    paso("Escritura en la carpeta", p_escritura)
    if cfg is not None and not validar_config(cfg):
        paso("Telegram", p_telegram)
        paso("Wallapop (búsqueda)", p_wallapop)
        paso("Wallapop (vendedor)", p_vendedor)
        paso("Vinted", p_vinted)
        paso("Cash Converters", p_cashconverters)
        paso("Chollometro", p_chollos)
    print("\n%s" % ("TODO CORRECTO" if not fallos else "HAY %d PROBLEMA(S)" % fallos))
    return fallos


# ---------------------------------------------------------------- ronda principal
def ejecutar(args):
    cfg = leer_json(CONFIG, None)
    if cfg is None:
        sys.exit("No puedo leer config.json")
    aplicar_entorno(cfg)
    errores = validar_config(cfg)
    if errores:
        sys.exit("config.json tiene problemas: " + "; ".join(errores))
    if args.presupuesto is not None:
        cfg["presupuesto_max"] = args.presupuesto
    if args.margen is not None:
        cfg["beneficio_min"] = args.margen
    if args.horas is not None:
        cfg["max_horas_anuncio"] = args.horas

    estado = leer_json(ESTADO, {"vistos": {}})
    vistos = estado.setdefault("vistos", {})
    cache = leer_json(MERCADO, {})
    ahora = time.time()
    u = cfg["ubicacion"]

    # Limpia anuncios vistos muy antiguos
    for k in [k for k, v in vistos.items() if ahora - v["t"] > DIAS_MEMORIA * 86400]:
        del vistos[k]

    candidatos, ids_ronda = [], set()
    busquedas_ok = busquedas_mal = 0

    for prod in cfg["productos"]:
        try:
            items, _ = wallapop_buscar(prod["buscar"], cfg, local=True)
            busquedas_ok += 1
        except RuntimeError as e:
            busquedas_mal += 1
            log.error("%s: %s", prod["nombre"], e)
            continue
        pausa(cfg)

        for it in items:
            if it["id"] in ids_ronda or it["precio"] is None:
                continue
            ids_ronda.add(it["id"])
            previo = vistos.get(it["id"])
            if previo and previo["p"] == it["precio"]:
                continue  # ya lo evaluamos con este precio
            if it["reservado"] or it["precio"] <= 0 or it["precio"] > cfg["presupuesto_max"]:
                continue
            if ahora - it["creado"] > cfg["max_horas_anuncio"] * 3600:
                continue
            if it["lat"] is None:
                continue
            dist = distancia_km(u["lat"], u["lon"], it["lat"], it["lon"])
            if dist > u["radio_km"]:
                continue
            var = clasificar(prod, it, cfg)
            if var is None:
                continue
            try:
                mer = mercado_producto(prod, cfg, cache).get(var)
            except RuntimeError as e:
                log.error("Mercado %s: %s", prod["nombre"], e)
                continue
            if not mer or mer["n"] < cfg["min_comparables"]:
                log.info("Sin datos suficientes de %s %s: %s", prod["nombre"], var, it["titulo"][:60])
                continue  # no se marca como visto: se reintenta cuando haya datos
            ev = evaluar(it, mer, cfg)
            marca = "✔" if ev["beneficio"] >= cfg["beneficio_min"] else "·"
            log.info("%s %s | %d€ | mercado %d€ | beneficio %d€%s",
                     marca, it["titulo"][:50], it["precio"], ev["venta"], ev["beneficio"],
                     "" if os.environ.get("GITHUB_ACTIONS") else " | %.0f km" % dist)  # logs públicos: sin distancias
            if ev["beneficio"] >= cfg["beneficio_min"]:
                it["vendedor"] = wallapop_vendedor(it.get("user_id"), cfg)
                pausa(cfg)
                if juzgar_vendedor(it["vendedor"], cfg) == "descartar":
                    log.info("  descartado: vendedor sin valoraciones (%s)", it["titulo"][:50])
                    vistos[it["id"]] = {"p": it["precio"], "t": ahora}
                    continue
                candidatos.append((it, prod["nombre"], var, ev, dist))
            else:
                vistos[it["id"]] = {"p": it["precio"], "t": ahora}

    if cfg.get("vinted", {}).get("activo"):
        base = estado.setdefault("vinted_base", [])
        try:
            op = vinted_abrir()
        except RuntimeError as e:
            op = None
            log.error("Vinted no disponible esta ronda: %s", e)
        for prod in (cfg["productos"] if op else []):
            primera = prod["nombre"] not in base  # primera vez: solo apunta lo que ya hay
            try:
                items = vinted_buscar(op, prod["buscar"], cfg)
            except RuntimeError as e:
                log.error("Vinted %s: %s", prod["nombre"], e)
                continue
            pausa(cfg)
            for it in items:
                if it["id"] in ids_ronda:
                    continue
                ids_ronda.add(it["id"])
                previo = vistos.get(it["id"])
                if previo and previo["p"] == it["precio"]:
                    continue
                if primera and not args.prueba:
                    vistos[it["id"]] = {"p": it["precio"], "t": ahora}
                    continue
                var = clasificar(prod, it, cfg)
                if var is None:
                    continue
                try:
                    mer = mercado_producto(prod, cfg, cache).get(var)
                except RuntimeError as e:
                    log.error("Mercado %s: %s", prod["nombre"], e)
                    continue
                if not mer or mer["n"] < cfg["min_comparables"]:
                    continue
                ev = evaluar(it, mer, cfg)
                marca = "✔" if ev["beneficio"] >= cfg["beneficio_min"] else "·"
                log.info("%s [Vinted] %s | %d€ | mercado %d€ | beneficio %d€", marca,
                         it["titulo"][:50], it["precio"], ev["venta"], ev["beneficio"])
                if ev["beneficio"] < cfg["beneficio_min"]:
                    vistos[it["id"]] = {"p": it["precio"], "t": ahora}
                    continue
                try:  # descripción y vendedor solo se leen de las candidatas
                    it["desc"], it["vendedor"] = vinted_ficha(op, it["url"])
                    pausa(cfg)
                except RuntimeError as e:
                    log.warning("No pude leer la ficha de %s: %s", it["id"], e)
                    it["vendedor"] = None
                if re.search(cfg["excluir_defectos"], it["desc"].lower()) or \
                        juzgar_vendedor(it["vendedor"], cfg) == "descartar":
                    log.info("  descartado: defecto o vendedor sin valoraciones (%s)", it["titulo"][:50])
                    vistos[it["id"]] = {"p": it["precio"], "t": ahora}
                    continue
                candidatos.append((it, prod["nombre"], var, ev, None))
            if primera and not args.prueba:
                base.append(prod["nombre"])

    if cfg.get("cashconverters", {}).get("activo"):
        base_cc = estado.setdefault("cc_base", [])
        for prod in cfg["productos"]:
            primera = prod["nombre"] not in base_cc  # primera vez: solo apunta lo que ya hay
            try:
                items = cc_buscar(prod["buscar"], cfg)
            except RuntimeError as e:
                log.error("Cash Converters %s: %s", prod["nombre"], e)
                continue
            pausa(cfg)
            grupos = {}  # el mismo artículo en varias tiendas cuenta como uno
            for it in items:
                if it["id"] in ids_ronda:
                    continue
                ids_ronda.add(it["id"])
                grupo = "kg:%s|%s|%s" % (it["titulo"], it["estado"], it["precio"])
                if grupo in grupos:
                    grupos[grupo]["unidades"] = grupos[grupo].get("unidades", 1) + 1
                    continue
                previo = vistos.get(it["id"])
                if previo and previo["p"] == it["precio"]:
                    continue
                if grupo in vistos:
                    vistos[it["id"]] = {"p": it["precio"], "t": ahora}
                    continue  # ese mismo artículo ya se evaluó en otra tienda
                grupos[grupo] = it
                it["grupo"] = grupo
                if primera and not args.prueba:
                    vistos[it["id"]] = {"p": it["precio"], "t": ahora}
                    vistos[grupo] = {"p": it["precio"], "t": ahora}
                    continue
                if it["precio"] > cfg["presupuesto_max"]:
                    continue
                var = clasificar(prod, it, cfg)
                if var is None:
                    continue
                try:
                    mer = mercado_producto(prod, cfg, cache).get(var)
                except RuntimeError as e:
                    log.error("Mercado %s: %s", prod["nombre"], e)
                    continue
                if not mer or mer["n"] < cfg["min_comparables"]:
                    continue
                ev = evaluar(it, mer, cfg)
                log.info("%s [Cash Converters] %s | %d€ | mercado %d€ | beneficio %d€",
                         "✔" if ev["beneficio"] >= cfg["beneficio_min"] else "·",
                         it["titulo"][:50], it["precio"], ev["venta"], ev["beneficio"])
                if ev["beneficio"] >= cfg["beneficio_min"]:
                    candidatos.append((it, prod["nombre"], var, ev, None))
                else:
                    vistos[it["id"]] = {"p": it["precio"], "t": ahora}
                    vistos[grupo] = {"p": it["precio"], "t": ahora}
            if primera and not args.prueba:
                base_cc.append(prod["nombre"])

    if cfg.get("chollos", {}).get("activo"):
        try:
            ofertas = leer_chollometro()
        except RuntimeError as e:
            ofertas = []
            log.error("%s", e)
        for it in ofertas:
            if it["id"] in ids_ronda:
                continue
            ids_ronda.add(it["id"])
            previo = vistos.get(it["id"])
            if previo and previo["p"] == it["precio"]:
                continue
            if it["precio"] > cfg["presupuesto_max"] or \
                    ahora - it["creado"] > cfg["chollos"].get("max_horas", 24) * 3600:
                continue
            prod = var = None
            for p in cfg["productos"]:
                var = clasificar(p, it, cfg)
                if var is not None:
                    prod = p
                    break
            if prod is None:
                continue
            try:
                mer = mercado_producto(prod, cfg, cache).get(var)
            except RuntimeError as e:
                log.error("Mercado %s: %s", prod["nombre"], e)
                continue
            if not mer or mer["n"] < cfg["min_comparables"]:
                continue
            ev = evaluar(it, mer, cfg)
            log.info("%s [%s] %s | %d€ | mercado %d€ | beneficio %d€",
                     "✔" if ev["beneficio"] >= cfg["beneficio_min"] else "·", it["tienda"],
                     it["titulo"][:50], it["precio"], ev["venta"], ev["beneficio"])
            if ev["beneficio"] >= cfg["beneficio_min"]:
                candidatos.append((it, prod["nombre"], var, ev, None))
            else:
                vistos[it["id"]] = {"p": it["precio"], "t": ahora}

    candidatos.sort(key=lambda c: -c[3]["beneficio"])
    log.info("Ronda: %d búsquedas ok, %d fallidas, %d chollos", busquedas_ok, busquedas_mal,
             len(candidatos))

    if args.prueba:
        for it, nombre, var, ev, dist in candidatos:
            print("\n" + texto_aviso(it, nombre, var, ev, dist).replace("<b>", "").replace("</b>", "")
                  + "\n" + it["url"])
        print("\n(Modo prueba: no se envió nada ni se guardó estado)")
        return

    for it, nombre, var, ev, dist in candidatos:
        try:
            enviar_aviso(it, texto_aviso(it, nombre, var, ev, dist))
            vistos[it["id"]] = {"p": it["precio"], "t": ahora}
            if it.get("grupo"):
                vistos[it["grupo"]] = {"p": it["precio"], "t": ahora}
            estado["avisos"] = estado.get("avisos", 0) + 1
            guardar_json(ESTADO, estado)  # tras cada aviso entregado
            time.sleep(1)
        except RuntimeError as e:
            log.error("No se pudo enviar el aviso (se reintentará): %s", e)

    if busquedas_ok == 0 and busquedas_mal > 0:
        ult = estado.get("ultimo_fallo", 0)
        if ahora - ult > HORAS_ENTRE_AVISOS_FALLO * 3600:
            try:
                enviar_texto("⚠️ El bot de chollos no consigue consultar Wallapop. "
                             "Puede ser un bloqueo temporal; sigo reintentando.")
                estado["ultimo_fallo"] = ahora
            except RuntimeError as e:
                log.error("Tampoco pude avisar del fallo: %s", e)

    estado["rondas"] = estado.get("rondas", 0) + 1
    estado["ultima_ronda"] = ahora
    guardar_json(ESTADO, estado)
    guardar_json(MERCADO, cache)


def latido():
    """Aviso diario de control: confirma que el bot sigue haciendo rondas."""
    estado = leer_json(ESTADO, {})
    ult = estado.get("ultima_ronda")
    if not ult:
        enviar_texto("⚠️ Bot de chollos: no encuentro ninguna ronda hecha todavía.")
        return
    mins = (time.time() - ult) / 60
    if mins > 120:
        enviar_texto("⚠️ Bot de chollos: parece parado. La última ronda fue hace %.1f horas." % (mins / 60))
    else:
        enviar_texto("💓 Bot de chollos activo. Última ronda hace %d min · %d rondas · %d avisos en total."
                     % (mins, estado.get("rondas", 0), estado.get("avisos", 0)))


def main():
    ap = argparse.ArgumentParser(description="Bot de chollos")
    ap.add_argument("--prueba", action="store_true", help="no envía ni guarda nada")
    ap.add_argument("--bucle", action="store_true", help="rondas continuas")
    ap.add_argument("--latido", action="store_true", help="envía el aviso diario de control")
    ap.add_argument("--comprobar", action="store_true", help="prueba que todo está bien configurado")
    ap.add_argument("--presupuesto", type=float, help="precio máximo por compra (€)")
    ap.add_argument("--margen", type=float, help="beneficio mínimo (€)")
    ap.add_argument("--horas", type=float, help="antigüedad máxima del anuncio (horas)")
    args = ap.parse_args()
    configurar_log()
    cargar_env()
    if args.comprobar:
        sys.exit(1 if comprobar() else 0)
    if args.latido:
        latido()
        return
    if not args.prueba:  # una sola copia a la vez: evita avisos duplicados
        global _CANDADO
        _CANDADO = open(BASE / ".candado", "w")
        try:
            fcntl.flock(_CANDADO, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("Ya hay otra copia del bot funcionando.")
    if not args.bucle:
        ejecutar(args)
        return
    while True:
        try:
            ejecutar(args)
        except Exception:
            log.exception("Error en la ronda; sigo en la siguiente")
        cfg = leer_json(CONFIG, {})
        time.sleep(cfg.get("minutos_entre_rondas", 5) * 60 * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    main()
