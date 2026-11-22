"""Ciclo en la nube (GitHub Actions, cada 15 min). Hora de referencia: Colombia.

1. Pasa a calendario.csv los videos nuevos que la PC dejo en cola/ y decide su hora.
2. Publica en Instagram lo que ya llego a su hora: Instagram descarga el video desde GitHub.
3. Borra de GitHub el video ya publicado.
4. Mide cada Reel a 1 h, 3 h, 24 h, 72 h y 7 dias (mediciones.csv) y, con el dato de 24 h, vuelve a decidir
   la hora de lo pendiente.
5. Une cada medicion con la ficha creativa del video (fichas/) y escribe rendimiento.md: lo que CODE ART lee
   antes de escribir el siguiente guion.
6. Una vez al dia guarda las metricas de todas las publicaciones en historico.csv.
"""
import csv, json, os, random, statistics, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Bogota")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL, HIST, LOG, COLA = (os.path.join(RAIZ, n) for n in ("calendario.csv", "historico.csv", "registro.log", "cola"))
MED, FICHAS, REND = (os.path.join(RAIZ, n) for n in ("mediciones.csv", "fichas", "rendimiento.md"))

# ---------- Configuracion ----------
FRANJAS = [(7, 0), (12, 30), (18, 0), (20, 0), (22, 30)]   # franjas que se prueban (hora Colombia)
EXPLORACION = 0.25          # 1 de cada 4 veces prueba otra franja distinta a la mejor
SEPARACION_MIN_HORAS = 14   # minimo entre publicaciones (14 h deja llegar a cualquier franja al dia siguiente)
CARRUSEL = "carrusel"        # franja especial: los carruseles van aparte de los Reels y no cuentan para horarios
CARRUSEL_DIAS, CARRUSEL_HORA = 2, (10, 0)   # el carrusel sale 2 dias despues de su Reel, a las 10:00
MIN_DATOS_FRANJA = 3        # videos con dato de 24 h que necesita una franja para poder llamarse 'la mejor'
MAX_POR_DIA = 1
ANTICIPACION_MIN_HORAS = 1  # un video nuevo no se publica antes de 1 h
MAX_INTENTOS = 3            # si Instagram falla, se reintenta en los ciclos siguientes (cada 15 min) hasta 3 veces
ALERTA = "/tmp/caminos_alerta.txt"  # si existe al terminar, el workflow falla y GitHub manda un correo
HITOS = [(1, "1h"), (3, "3h"), (24, "24h"), (72, "72h"), (168, "7d")]  # cuando se mide cada Reel
DESFASE_MAX_MIN = 45        # si salio mas tarde que esto respecto a su franja, no cuenta para aprender horarios
# -----------------------------------

COLS = ["id", "fecha_hora", "carpeta", "asset", "tema", "gancho", "aprobado", "estado", "ig_media_id", "permalink",
        "detectado", "franja", "motivo_hora", "publicado_real", "views_24h", "reach_24h", "skip_24h", "watch_24h_s",
        "yt_video_id", "yt_estado", "intentos"]
ENV = {k: os.environ.get(k, "") for k in ("FB_PAGE_TOKEN", "IG_USER_ID", "GRAPH_VERSION", "GH_TOKEN", "GH_REPO",
                                          "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "YT_REFRESH_TOKEN")}
HIST_YT = os.path.join(RAIZ, "historico_youtube.csv")
# Se pone en True cuando YouTube apruebe la auditoria (antes, todo lo subido por API queda privado para siempre)
YT_SUBIDA_ACTIVA = True   # 16 nov 2026: YouTube ya no deja privados los videos de la API (prueba no listada OK)


def ahora():
    return datetime.now(TZ).replace(tzinfo=None)


def log(msg):
    linea = f"{ahora():%Y-%m-%d %H:%M} {msg}"
    print(linea)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


# ---------- APIs ----------
class ErrorAPI(Exception):
    pass


def http(metodo, url, datos=None, headers=None):
    body = urllib.parse.urlencode(datos).encode() if isinstance(datos, dict) else datos
    req = urllib.request.Request(url, data=body, method=metodo, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode(errors="ignore")
        try:
            err = json.loads(cuerpo).get("error", {})
            raise ErrorAPI(f"{err.get('code')} - {err.get('message')}" if isinstance(err, dict) else cuerpo[:300])
        except ValueError:
            raise ErrorAPI(f"HTTP {e.code}: {cuerpo[:300]}")


def meta(metodo, ruta, params=None):
    params = dict(params or {}, access_token=ENV["FB_PAGE_TOKEN"])
    base = f"https://graph.facebook.com/{ENV['GRAPH_VERSION'] or 'v25.0'}/{ruta}"
    if metodo == "GET":
        return http("GET", base + "?" + urllib.parse.urlencode(params))
    return http("POST", base, params)


def github(metodo, ruta, datos=None):
    h = {"Authorization": f"Bearer {ENV['GH_TOKEN']}", "Accept": "application/vnd.github+json"}
    body = json.dumps(datos).encode() if datos is not None else None
    if body:
        h["Content-Type"] = "application/json"
    return http(metodo, f"https://api.github.com/repos/{ENV['GH_REPO']}{ruta}", body, h)


def asset_de(nombre):
    rel = github("GET", "/releases/tags/videos")
    for a in rel.get("assets", []):
        if a["name"] == nombre:
            return a
    return None


# ---------- calendario ----------
def leer_cal():
    if not os.path.exists(CAL):
        return []
    with open(CAL, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def guardar_cal(filas):
    with open(CAL, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in filas:
            w.writerow({k: r.get(k, "") for k in COLS})


def salio_a_tiempo(r):
    try:
        prog = datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M")
        real = datetime.strptime(r.get("publicado_real") or r["fecha_hora"], "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return False
    return abs((real - prog).total_seconds()) <= DESFASE_MAX_MIN * 60


def rendimiento(filas):
    datos = {}
    for r in filas:
        if r.get("views_24h") and r.get("franja") and r["franja"] != CARRUSEL and salio_a_tiempo(r) \
                and r["carpeta"] not in excluidos():
            datos.setdefault(r["franja"], []).append(float(r["views_24h"]))
    return {k: sum(v) / len(v) for k, v in datos.items()}, {k: len(v) for k, v in datos.items()}


def elegir_hora(filas, desde, excluir=None):
    excluir = excluir if isinstance(excluir, (list, tuple)) else [excluir]
    # 19 nov 2026: la separacion se mide contra cada video vecino (antes o despues), no "despues del ultimo";
    # asi un video fijado o una parte de serie en el futuro no empuja a los demas y no quedan dias vacios.
    ocupados, horas_ocupadas = {}, []
    for r in filas:
        if (any(r is x for x in excluir) or r["estado"] not in ("pendiente", "publicado", "publicando")
                or not r.get("fecha_hora") or r.get("franja") == CARRUSEL):
            continue
        ocupados[r["fecha_hora"][:10]] = ocupados.get(r["fecha_hora"][:10], 0) + 1
        horas_ocupadas.append(datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M"))
    sep = timedelta(hours=SEPARACION_MIN_HORAS)
    prom, cuenta = rendimiento(filas)
    etiquetas = [f"{h:02d}:{m:02d}" for h, m in FRANJAS]

    def proximo(e):
        """Primer hueco libre de la franja e desde 'desde' (respeta separacion y maximo por dia)."""
        t = desde.replace(second=0, microsecond=0, minute=(desde.minute // 15) * 15)
        for _ in range(14 * 96):
            if (f"{t.hour:02d}:{t.minute:02d}" == e and t >= desde
                    and ocupados.get(t.strftime("%Y-%m-%d"), 0) < MAX_POR_DIA
                    and all(abs(t - o) >= sep for o in horas_ocupadas)):
                return t
            t += timedelta(minutes=15)
        return None

    huecos = {e: proximo(e) for e in etiquetas}
    huecos = {e: t for e, t in huecos.items() if t}
    if not huecos:
        return desde, "", "sin hueco en 14 dias"
    # Solo se eligen franjas del primer dia con hueco: asi sale un video al dia y nunca se salta un dia por
    # esperar una franja concreta.
    primero = min(huecos.values())
    cerca = {e: t for e, t in huecos.items() if t.date() == primero.date()}
    # Para repartir se cuentan tambien los ya programados o publicados sin dato de 24 h todavia
    plan = dict(cuenta)
    for r in filas:
        if (not any(r is x for x in excluir) and r["estado"] in ("pendiente", "publicando", "publicado")
                and r.get("franja") and r["franja"] != CARRUSEL and not r.get("views_24h")):
            plan[r["franja"]] = plan.get(r["franja"], 0) + 1
    confiables = {e: v for e, v in prom.items() if cuenta.get(e, 0) >= MIN_DATOS_FRANJA}
    if not confiables:
        # Aun no hay franja con datos suficientes: se reparte, la que menos datos tiene primero (y la mas temprana).
        e = min(cerca, key=lambda x: (plan.get(x, 0), cerca[x]))
        return cerca[e], e, (f"exploracion: {e} tiene {cuenta.get(e, 0)} de {MIN_DATOS_FRANJA} videos para comparar")
    mejor = max(confiables, key=confiables.get)
    if mejor in cerca and not (random.random() < EXPLORACION and len(cerca) > 1):
        return cerca[mejor], mejor, (f"mejor franja: {mejor} ({confiables[mejor]:.0f} vistas a 24 h, "
                                     f"{cuenta[mejor]} Reels)")
    otras = [x for x in cerca if x != mejor] or list(cerca)
    e = min(otras, key=lambda x: (plan.get(x, 0), cerca[x]))
    return cerca[e], e, f"exploracion: se prueba {e} (la mejor con datos suficientes sigue siendo {mejor})"


# ---------- pasos ----------
def serie_de(carpeta):
    """(serie, parte) segun la ficha del video, o (None, None) si no es parte de una serie."""
    ruta = os.path.join(FICHAS, f"{carpeta}.json")
    if not os.path.exists(ruta):
        return None, None
    f = json.load(open(ruta, encoding="utf-8"))
    try:
        return (f.get("serie") or None), int(f.get("parte") or 0) or None
    except (TypeError, ValueError):
        return None, None


def anterior_de_serie(filas, serie, parte):
    for r in filas:
        if r.get("franja") != CARRUSEL and r.get("fecha_hora") and serie_de(r["carpeta"]) == (serie, parte - 1):
            return r
    return None


def hora_de_serie(filas, ficha):
    """Parte 2 (o siguiente) de una serie: 24 h despues de la parte anterior, a la misma hora."""
    serie, parte = ficha.get("serie"), ficha.get("parte")
    try:
        parte = int(parte)
    except (TypeError, ValueError):
        return None
    if not serie or parte < 2:
        return None
    prev = anterior_de_serie(filas, serie, parte)
    if not prev:
        return None
    t = datetime.strptime(prev["fecha_hora"], "%Y-%m-%d %H:%M") + timedelta(days=1)
    while t < ahora() + timedelta(hours=ANTICIPACION_MIN_HORAS):
        t += timedelta(days=1)
    return t, prev.get("franja", ""), f"serie {serie}: parte {parte}, 24 h despues de la parte {parte - 1}"


def tomar_cola(filas):
    if not os.path.isdir(COLA):
        return False
    cambio = False
    for nombre in sorted(os.listdir(COLA)):
        if not nombre.endswith(".json"):
            continue
        ruta = os.path.join(COLA, nombre)
        d = json.load(open(ruta, encoding="utf-8"))
        if d.get("tipo") == CARRUSEL and not any(r["carpeta"] == d["carpeta"] for r in filas):
            reel = next((r for r in filas if r["carpeta"] == d.get("reel")), None)
            base = datetime.strptime(reel["fecha_hora"], "%Y-%m-%d %H:%M") if reel else ahora()
            t = (base + timedelta(days=CARRUSEL_DIAS)).replace(hour=CARRUSEL_HORA[0], minute=CARRUSEL_HORA[1])
            minimo = ahora() + timedelta(hours=ANTICIPACION_MIN_HORAS)
            while t < minimo:
                t += timedelta(days=1)
            filas.append({"id": f"CD-{d['carpeta']}", "carpeta": d["carpeta"], "asset": d["asset"], "tema": d["tema"],
                          "gancho": d["ig"].splitlines()[0][:120], "aprobado": "Si", "estado": "pendiente",
                          "detectado": d.get("detectado", ""), "fecha_hora": t.strftime("%Y-%m-%d %H:%M"),
                          "franja": CARRUSEL, "motivo_hora": f"carrusel: {CARRUSEL_DIAS} dias despues de su Reel"})
            log(f"Agendado carrusel {d['carpeta']} -> {t:%Y-%m-%d %H:%M}")
        elif not any(r["carpeta"] == d["carpeta"] for r in filas):
            t, e, motivo = hora_de_serie(filas, d.get("ficha") or {}) or \
                elegir_hora(filas, ahora() + timedelta(hours=ANTICIPACION_MIN_HORAS))
            filas.append({"id": f"CD-{d['carpeta']}", "carpeta": d["carpeta"], "asset": d["asset"], "tema": d["tema"],
                          "gancho": d["ig"].splitlines()[0][:120], "aprobado": "Si", "estado": "pendiente",
                          "detectado": d.get("detectado", ""), "fecha_hora": t.strftime("%Y-%m-%d %H:%M"),
                          "franja": e, "motivo_hora": motivo})
            log(f"Agendado {d['carpeta']} -> {t:%Y-%m-%d %H:%M} ({motivo})")
        if d.get("ficha"):
            guardar_ficha(d["carpeta"], d["ficha"])
        os.makedirs(os.path.join(COLA, "textos"), exist_ok=True)
        os.replace(ruta, os.path.join(COLA, "textos", nombre))  # se conserva el texto para publicar
        cambio = True
    return cambio


def crear_contenedor(ig, url, caption, portada_url=None):
    """Crea el contenedor del Reel y espera a que Instagram procese el video. Devuelve (id, estado)."""
    p = {"media_type": "REELS", "video_url": url, "caption": caption, "share_to_feed": "true"}
    if portada_url:
        p["cover_url"] = portada_url  # la miniatura de CODE ART (flyer.jpg) como portada
    cont = meta("POST", f"{ig}/media", p)["id"]
    for _ in range(20):  # hasta 10 minutos
        time.sleep(30)
        st = meta("GET", cont, {"fields": "status_code,status"})
        if st.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
            return cont, st
    return cont, {"status_code": "TIEMPO", "status": "Instagram tardo mas de 10 minutos procesando"}


def publicar_carrusel(r, d):
    """Carrusel de imagenes (hasta 10): un contenedor por imagen y uno padre con todos (API oficial)."""
    ig = ENV["IG_USER_ID"]
    hijos, assets = [], []
    for nombre in d["imagenes"][:10]:
        a = asset_de(nombre)
        if not a:
            raise ErrorAPI(f"No esta la imagen {nombre} en GitHub")
        assets.append(a)
        hijos.append(meta("POST", f"{ig}/media", {"image_url": a["browser_download_url"],
                                                    "is_carousel_item": "true"})["id"])
    padre = meta("POST", f"{ig}/media", {"media_type": "CAROUSEL", "children": ",".join(hijos),
                                          "caption": d["ig"]})["id"]
    st = {}
    for _ in range(20):
        st = meta("GET", padre, {"fields": "status_code,status"})
        if st.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
            break
        time.sleep(15)
    if st.get("status_code") != "FINISHED":
        raise ErrorAPI(f"Instagram rechazo el carrusel: {st.get('status')}")
    mid = meta("POST", f"{ig}/media_publish", {"creation_id": padre})["id"]
    link = meta("GET", mid, {"fields": "permalink"}).get("permalink", "")
    r.update(estado="publicado", ig_media_id=mid, permalink=link, publicado_real=ahora().strftime("%Y-%m-%d %H:%M"))
    log(f"PUBLICADO carrusel {r['carpeta']} ({len(hijos)} imagenes): {link}")
    for a in assets:
        github("DELETE", f"/releases/assets/{a['id']}")


def publicar(r):
    d = json.load(open(os.path.join(COLA, "textos", f"{r['carpeta']}.json"), encoding="utf-8"))
    if r.get("franja") == CARRUSEL:
        return publicar_carrusel(r, d)
    a = asset_de(r["asset"])
    if not a:
        raise ErrorAPI(f"No esta el video {r['asset']} en GitHub")
    portada = asset_de(d.get("portada") or r["asset"].rsplit(".", 1)[0] + ".jpg")
    ig = ENV["IG_USER_ID"]
    # Instagram descarga el video (y la portada) desde GitHub (via probada el 9 nov 2026)
    cont, st = crear_contenedor(ig, a["browser_download_url"], d["ig"],
                                portada["browser_download_url"] if portada else None)
    log(f"{r['carpeta']}: contenedor {cont} ({'con portada' if portada else 'sin portada'}): {st.get('status_code')}")
    if st.get("status_code") != "FINISHED" and portada:
        log(f"{r['carpeta']}: fallo con portada ({st.get('status')}); se reintenta sin portada")
        cont, st = crear_contenedor(ig, a["browser_download_url"], d["ig"])
    if st.get("status_code") != "FINISHED":
        raise ErrorAPI(f"Instagram rechazo el video: {st.get('status')}")
    mid = meta("POST", f"{ig}/media_publish", {"creation_id": cont})["id"]
    link = meta("GET", mid, {"fields": "permalink"}).get("permalink", "")
    r.update(estado="publicado", ig_media_id=mid, permalink=link, publicado_real=ahora().strftime("%Y-%m-%d %H:%M"))
    log(f"PUBLICADO {r['carpeta']}: {link}")
    borrar_assets(r, a, portada)


def borrar_assets(r, a=None, portada=None):
    """Borra el video y la portada de GitHub cuando ya no hacen falta para ninguna plataforma."""
    if r.get("franja") == CARRUSEL:
        try:
            d = json.load(open(os.path.join(COLA, "textos", f"{r['carpeta']}.json"), encoding="utf-8"))
            for nombre in d.get("imagenes", []):
                x = asset_de(nombre)
                if x:
                    github("DELETE", f"/releases/assets/{x['id']}")
        except Exception as e:
            log(f"No se pudieron borrar las imagenes del carrusel {r['carpeta']}: {e}")
        return
    if YT_SUBIDA_ACTIVA and not r.get("yt_video_id"):
        return
    if a is None:
        a = asset_de(r["asset"])
        portada = asset_de(r["asset"].rsplit(".", 1)[0] + ".jpg")
    for x in (a, portada):
        if x:
            github("DELETE", f"/releases/assets/{x['id']}")
            log(f"{x['name']} borrado de GitHub")


def alerta(msg):
    with open(ALERTA, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    log(f"ALERTA: {msg}")


def ya_publicado(r):
    """Antes de (re)intentar, mira si el Reel ya esta en Instagram (p. ej. un ciclo se corto tras publicarlo)."""
    try:
        d = json.load(open(os.path.join(COLA, "textos", f"{r['carpeta']}.json"), encoding="utf-8"))
        inicio = " ".join(d["ig"].split())[:80]
        desde = datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ) - timedelta(minutes=10)
        for m in meta("GET", f"{ENV['IG_USER_ID']}/media",
                      {"fields": "id,caption,permalink,timestamp", "limit": 10}).get("data", []):
            t = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
            if t >= desde and " ".join((m.get("caption") or "").split())[:80] == inicio:
                return m, t.astimezone(TZ)
    except Exception as e:
        log(f"No se pudo comprobar si {r['carpeta']} ya estaba publicado: {e}")
    return None, None


def publicar_vencidos(filas):
    for r in filas:
        if r["estado"] == "publicando":  # una ejecucion anterior se corto: se comprueba en Instagram antes de reintentar
            r["estado"] = "pendiente"
    for r in sorted(filas, key=lambda r: r.get("fecha_hora", "")):
        if r["estado"] != "pendiente" or r.get("aprobado", "").strip().lower() not in ("si", "sí"):
            continue
        if datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M") > ahora():
            continue
        m, t = ya_publicado(r)
        if m:  # ya salio (no se publica dos veces)
            r.update(estado="publicado", ig_media_id=m["id"], permalink=m.get("permalink", ""),
                     publicado_real=t.strftime("%Y-%m-%d %H:%M"))
            log(f"{r['carpeta']} ya estaba publicado en Instagram: {r['permalink']}")
            borrar_assets(r)
            return True
        r["estado"] = "publicando"
        guardar_cal(filas)
        try:
            publicar(r)
        except Exception as e:
            n = int(r.get("intentos") or 0) + 1
            r["intentos"] = n
            if n < MAX_INTENTOS:
                r["estado"] = "pendiente"
                log(f"ERROR publicando {r['carpeta']} (intento {n} de {MAX_INTENTOS}); se reintenta en el proximo ciclo: {e}")
            else:
                r["estado"] = f"error: {str(e)[:150]}"
                alerta(f"No se pudo publicar {r['carpeta']} tras {n} intentos: {str(e)[:300]}")
        return True  # uno por ciclo
    return False


def guardar_ficha(carpeta, ficha):
    os.makedirs(FICHAS, exist_ok=True)
    with open(os.path.join(FICHAS, f"{carpeta}.json"), "w", encoding="utf-8") as f:
        json.dump(ficha, f, ensure_ascii=False, indent=1)


COLS_MED = ["carpeta", "ig_media_id", "hito", "fecha", "horas", "views", "reach", "likes", "comments", "saved",
            "shares", "total_interactions", "avg_watch_s", "skip_rate", "fuente"]


def excluidos():
    """Videos cuyas metricas no valen para aprender (excluidos.txt, una carpeta por linea; # comenta)."""
    ruta = os.path.join(RAIZ, "excluidos.txt")
    if not os.path.exists(ruta):
        return set()
    return {l.split("#")[0].strip() for l in open(ruta, encoding="utf-8") if l.split("#")[0].strip()}


def leer_med():
    if not os.path.exists(MED):
        return []
    with open(MED, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def insights_reel(mid):
    met = ["views", "reach", "likes", "comments", "saved", "shares", "total_interactions",
           "ig_reels_avg_watch_time", "reels_skip_rate"]
    try:
        datos = meta("GET", f"{mid}/insights", {"metric": ",".join(met)}).get("data", [])
    except ErrorAPI:  # por si alguna metrica no esta disponible todavia
        datos = meta("GET", f"{mid}/insights", {"metric": ",".join(met[:7])}).get("data", [])
    return {d["name"]: d["values"][0]["value"] for d in datos if d.get("values")}


def medir_hitos(filas):
    """Mide cada Reel en sus hitos. Un hito que se paso de largo (PC o nube caidas) no se inventa: se salta,
    salvo el de 24 h, que se acepta hasta las 48 h porque decide los horarios."""
    hechos = {(m["carpeta"], m["hito"]) for m in leer_med()}
    nuevas, cambio_24h = [], False
    franjas_antes = rendimiento(filas)
    for r in filas:
        if r["estado"] != "publicado" or not r.get("ig_media_id"):
            continue
        try:
            t0 = datetime.strptime(r.get("publicado_real") or r["fecha_hora"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        edad = (ahora() - t0).total_seconds() / 3600
        for horas, hito in HITOS:
            if (r["carpeta"], hito) in hechos or edad < horas:
                continue
            if edad > (horas * 2 if hito == "24h" else horas * 1.5):
                continue
            v = insights_reel(r["ig_media_id"])
            watch = round(v["ig_reels_avg_watch_time"] / 1000, 1) if "ig_reels_avg_watch_time" in v else ""
            nuevas.append({"carpeta": r["carpeta"], "ig_media_id": r["ig_media_id"], "hito": hito,
                           "fecha": ahora().strftime("%Y-%m-%d %H:%M"), "horas": round(edad, 1),
                           "views": v.get("views", ""), "reach": v.get("reach", ""), "likes": v.get("likes", ""),
                           "comments": v.get("comments", ""), "saved": v.get("saved", ""),
                           "shares": v.get("shares", ""), "total_interactions": v.get("total_interactions", ""),
                           "avg_watch_s": watch, "skip_rate": v.get("reels_skip_rate", ""), "fuente": "api"})
            log(f"Medicion {hito} de {r['carpeta']}: {v.get('views', 0)} vistas, salto {v.get('reels_skip_rate', '?')} %, "
                f"{watch} s de media, {v.get('shares', 0)} compartidos")
            if hito == "24h" and not r.get("views_24h"):
                r.update(views_24h=v.get("views", 0), reach_24h=v.get("reach", ""),
                         skip_24h=v.get("reels_skip_rate", ""), watch_24h_s=watch)
                cambio_24h = True
            break  # un hito por Reel y por ciclo
    if nuevas:
        nuevo = not os.path.exists(MED)
        with open(MED, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS_MED)
            if nuevo:
                w.writeheader()
            w.writerows(nuevas)
    if cambio_24h and rendimiento(filas) != franjas_antes:
        replanificar(filas)
    return cambio_24h


def es_fijo(r):
    """Fecha anunciada al publico (p. ej. en comentarios): la replanificacion no la mueve."""
    return (r.get("motivo_hora") or "").startswith("fijo")


def mover(r, t, e, motivo):
    antes = r["fecha_hora"]
    r.update(fecha_hora=t.strftime("%Y-%m-%d %H:%M"), franja=e, motivo_hora=motivo)
    if r["fecha_hora"] != antes:
        log(f"Replanificado {r['carpeta']}: {antes} -> {r['fecha_hora']} ({motivo})")
        if r.get("yt_video_id"):
            try:  # el Short sigue a su Reel: misma hora en las dos plataformas
                yt_reprogramar(r)
            except Exception as ex:
                log(f"No se pudo mover el Short de {r['carpeta']}: {ex}")


def replanificar(filas):
    """Vuelve a decidir la hora de lo pendiente, en orden. Cada uno respeta a los ya decididos y a los fijos;
    las partes 2+ de una serie van 24 h despues de su parte anterior, en cuanto esa se decide."""
    pend = sorted([r for r in filas if r["estado"] == "pendiente" and r.get("franja") != CARRUSEL
                   and not es_fijo(r)], key=lambda r: r["fecha_hora"])
    # 22 nov 2026: una parte 2+ cuya parte anterior ya salio (o no se replanifica) queda donde esta y ocupa su dia;
    # antes quedaba "por decidir", no contaba como ocupada y otro video tomo su mismo dia.
    def anterior_pendiente(r):
        serie, parte = serie_de(r["carpeta"])
        return any(x is not r and serie_de(x["carpeta"]) == (serie, (parte or 0) - 1) for x in pend)
    pend = [r for r in pend if not ((serie_de(r["carpeta"])[1] or 0) >= 2 and not anterior_pendiente(r))]
    pendientes = list(pend)
    for r in pend:
        if r not in pendientes:
            continue   # ya se coloco como parte de una serie
        serie, parte = serie_de(r["carpeta"])
        if serie and (parte or 0) >= 2:
            continue   # se coloca cuando se decide su parte anterior
        pendientes.remove(r)
        if datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M") - ahora() >= timedelta(hours=2):
            t, e, motivo = elegir_hora(filas, ahora() + timedelta(hours=2), excluir=[r] + pendientes)
            mover(r, t, e, motivo)
        if serie:
            sig = parte or 1
            while True:
                nxt = next((x for x in pendientes if serie_de(x["carpeta"]) == (serie, sig + 1)), None)
                if not nxt:
                    break
                pendientes.remove(nxt)
                nueva = hora_de_serie(filas, {"serie": serie, "parte": sig + 1})
                if nueva:
                    mover(nxt, *nueva)
                sig += 1



ARREGLO_19NOV = os.path.join(RAIZ, "arreglos", "2026-11-19-calendario.done")


def arreglo_19nov(filas):
    """Una sola vez: el 19 nov una replanificacion con un error empujo todo 4 dias. No es pereza (completo) vuelve
    a la fecha anunciada en comentarios (vie 20, 22:30) y lo demas se replanifica con la regla corregida."""
    if os.path.exists(ARREGLO_19NOV):
        return False
    for r in filas:
        if r["carpeta"] == "2026-11-17-no-es-pereza-completo" and r["estado"] == "pendiente":
            r.update(fecha_hora="2026-11-20 22:30", franja="22:30", motivo_hora="fijo: anunciado en comentarios")
            log("Arreglo: No es pereza (completo) vuelve al vie 20 22:30 (fecha anunciada)")
            if r.get("yt_video_id"):
                try:
                    yt_reprogramar(r)
                except Exception as ex:
                    log(f"No se pudo mover el Short de {r['carpeta']}: {ex}")
    replanificar(filas)
    os.makedirs(os.path.dirname(ARREGLO_19NOV), exist_ok=True)
    open(ARREGLO_19NOV, "w").write(ahora().strftime("%Y-%m-%d %H:%M") + "\n")
    return True


def metricas_diarias():
    hoy = ahora().strftime("%Y-%m-%d")
    if ahora().hour < 6:
        return
    if os.path.exists(HIST):
        with open(HIST, encoding="utf-8-sig") as f:
            if any(l.startswith(hoy) for l in f):
                return
    medios, despues = [], None
    while True:
        p = {"fields": "id,caption,media_product_type,timestamp,permalink", "limit": 50}
        if despues:
            p["after"] = despues
        res = meta("GET", f"{ENV['IG_USER_ID']}/media", p)
        medios += res.get("data", [])
        despues = res.get("paging", {}).get("cursors", {}).get("after")
        if not res.get("paging", {}).get("next"):
            break
    cols = ["fecha_consulta", "media_id", "fecha_publicacion", "tipo", "permalink", "texto_inicio", "views", "reach",
            "likes", "comments", "saved", "shares", "total_interactions", "avg_watch_time_s", "reels_skip_rate"]
    nuevo = not os.path.exists(HIST)
    with open(HIST, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if nuevo:
            w.writeheader()
        for m in medios:
            reels = m.get("media_product_type") == "REELS"
            met = ["views", "reach", "likes", "comments", "saved", "shares", "total_interactions"]
            if reels:
                met += ["ig_reels_avg_watch_time", "reels_skip_rate"]
            try:
                d = {x["name"]: x["values"][0]["value"] for x in meta("GET", f"{m['id']}/insights",
                                                                      {"metric": ",".join(met)}).get("data", [])}
            except ErrorAPI:
                d = {}
            w.writerow({"fecha_consulta": hoy, "media_id": m["id"], "fecha_publicacion": m.get("timestamp", "")[:16],
                        "tipo": m.get("media_product_type"), "permalink": m.get("permalink", ""),
                        "texto_inicio": (m.get("caption") or "").replace("\n", " ")[:80],
                        **{k: d.get(k, "") for k in ["views", "reach", "likes", "comments", "saved", "shares",
                                                     "total_interactions", "reels_skip_rate"]},
                        "avg_watch_time_s": round(d["ig_reels_avg_watch_time"] / 1000, 1)
                        if "ig_reels_avg_watch_time" in d else ""})
    log(f"Metricas del dia guardadas ({len(medios)} publicaciones)")


# ---------- YouTube ----------
_yt_token = None


def yt_token():
    global _yt_token
    if not _yt_token:
        _yt_token = http("POST", "https://oauth2.googleapis.com/token", {
            "client_id": ENV["GOOGLE_CLIENT_ID"], "client_secret": ENV["GOOGLE_CLIENT_SECRET"],
            "refresh_token": ENV["YT_REFRESH_TOKEN"], "grant_type": "refresh_token"})["access_token"]
    return _yt_token


def yt(url, params=None):
    return http("GET", url + "?" + urllib.parse.urlencode(params or {}), headers={"Authorization": f"Bearer {yt_token()}"})


def yt_ya_subido(titulo):
    """Busca entre las ultimas subidas del canal un video con el mismo titulo (evita duplicados si un ciclo
    subio el Short pero no alcanzo a guardar el calendario). Devuelve su id o None."""
    try:
        canal = yt("https://www.googleapis.com/youtube/v3/channels", {"part": "contentDetails", "mine": "true"})
        lista = canal["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        items = yt("https://www.googleapis.com/youtube/v3/playlistItems",
                   {"part": "snippet", "playlistId": lista, "maxResults": 25}).get("items", [])
        for it in items:
            if it["snippet"]["title"].strip() == titulo.strip():
                return it["snippet"]["resourceId"]["videoId"]
    except Exception as e:
        log(f"No se pudo revisar si el Short ya estaba subido: {e}")
    return None


def metricas_youtube():
    """Una vez al dia: metricas acumuladas de cada video del canal (YouTube Analytics tarda 2-3 dias en consolidar)."""
    if not ENV["YT_REFRESH_TOKEN"] or ahora().hour < 6:
        return
    hoy = ahora().strftime("%Y-%m-%d")
    if os.path.exists(HIST_YT):
        with open(HIST_YT, encoding="utf-8-sig") as f:
            if any(l.startswith(hoy) for l in f):
                return
    rep = yt("https://youtubeanalytics.googleapis.com/v2/reports", {
        "ids": "channel==MINE", "startDate": "2020-01-01", "endDate": hoy, "dimensions": "video",
        "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained",
        "sort": "-views", "maxResults": 200})
    cols_rep = [c["name"] for c in rep.get("columnHeaders", [])]
    filas = [dict(zip(cols_rep, f)) for f in rep.get("rows", [])]
    info = {}
    ids = [f["video"] for f in filas]
    for i in range(0, len(ids), 50):
        for v in yt("https://www.googleapis.com/youtube/v3/videos",
                    {"part": "snippet,contentDetails", "id": ",".join(ids[i:i + 50])}).get("items", []):
            info[v["id"]] = v
    cols = ["fecha_consulta", "video_id", "fecha_publicacion", "titulo", "duracion", "views", "minutos_vistos",
            "duracion_media_vista_s", "porcentaje_visto", "likes", "comments", "shares", "suscriptores_ganados"]
    nuevo = not os.path.exists(HIST_YT)
    with open(HIST_YT, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if nuevo:
            w.writeheader()
        for fl in filas:
            v = info.get(fl["video"], {})
            w.writerow({"fecha_consulta": hoy, "video_id": fl["video"],
                        "fecha_publicacion": v.get("snippet", {}).get("publishedAt", "")[:16],
                        "titulo": v.get("snippet", {}).get("title", "")[:90],
                        "duracion": v.get("contentDetails", {}).get("duration", ""),
                        "views": fl.get("views"), "minutos_vistos": fl.get("estimatedMinutesWatched"),
                        "duracion_media_vista_s": fl.get("averageViewDuration"),
                        "porcentaje_visto": fl.get("averageViewPercentage"), "likes": fl.get("likes"),
                        "comments": fl.get("comments"), "shares": fl.get("shares"),
                        "suscriptores_ganados": fl.get("subscribersGained")})
    # Politica de datos de YouTube: no conservar datos de la API mas de 30 dias sin actualizar
    limite = (ahora() - timedelta(days=30)).strftime("%Y-%m-%d")
    with open(HIST_YT, encoding="utf-8-sig", newline="") as f:
        todas = list(csv.DictReader(f))
    vigentes = [r for r in todas if r["fecha_consulta"] >= limite]
    if len(vigentes) < len(todas):
        with open(HIST_YT, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(vigentes)
    log(f"Metricas de YouTube guardadas ({len(filas)} videos)")


def subir_short(r):
    """Sube el Short programado (publishAt) a la misma hora que el Reel. Solo se usa con YT_SUBIDA_ACTIVA."""
    d = json.load(open(os.path.join(COLA, "textos", f"{r['carpeta']}.json"), encoding="utf-8"))
    a = asset_de(r["asset"])
    datos = urllib.request.urlopen(a["browser_download_url"], timeout=300).read()
    titulo = (d.get("yt_titulo") or d["tema"])[:100]
    previo = yt_ya_subido(titulo)
    if previo:
        r.update(yt_video_id=previo, yt_estado="programado")
        yt_reprogramar(r)
        log(f"Short de {r['carpeta']} ya estaba en YouTube ({previo}); se reutiliza y se ajusta su hora")
        return
    desc = d.get("yt_descripcion") or d["ig"]
    if "#shorts" not in desc.lower():
        desc += "\n\n#Shorts"
    publicar_en = datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ).isoformat()
    meta_json = json.dumps({"snippet": {"title": titulo, "description": desc, "categoryId": "22", "defaultLanguage": "es"},
                            "status": {"privacyStatus": "private", "publishAt": publicar_en,
                                       "selfDeclaredMadeForKids": False}}).encode()
    limite = "----caminosdivinos"
    cuerpo = (f"--{limite}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n").encode() + meta_json + \
             (f"\r\n--{limite}\r\nContent-Type: video/mp4\r\n\r\n").encode() + datos + f"\r\n--{limite}--".encode()
    res = http("POST", "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart&part=snippet,status",
               cuerpo, {"Authorization": f"Bearer {yt_token()}", "Content-Type": f"multipart/related; boundary={limite}"})
    r.update(yt_video_id=res["id"], yt_estado="programado")
    log(f"Short subido y programado para {r['fecha_hora']}: https://youtube.com/shorts/{res['id']}")


def yt_reprogramar(r):
    """Mueve la hora de salida de un Short ya subido (cuando su Reel se replanifica)."""
    publicar_en = datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ).isoformat()
    http("PUT", "https://www.googleapis.com/youtube/v3/videos?part=status",
         json.dumps({"id": r["yt_video_id"], "status": {"privacyStatus": "private", "publishAt": publicar_en,
                                                        "selfDeclaredMadeForKids": False}}).encode(),
         {"Authorization": f"Bearer {yt_token()}", "Content-Type": "application/json"})
    log(f"Short de {r['carpeta']} movido a {r['fecha_hora']}")


def subir_shorts_pendientes(filas):
    if not YT_SUBIDA_ACTIVA:
        return False
    for r in filas:
        if (r["estado"] == "pendiente" and not r.get("yt_video_id") and r.get("franja") != CARRUSEL
                and r.get("aprobado", "").lower() in ("si", "sí")):
            try:
                subir_short(r)
            except Exception as e:
                r["yt_estado"] = f"error: {str(e)[:120]}"
                log(f"ERROR subiendo Short {r['carpeta']}: {e}")
            return True
    return False


# ---------- Shorts de Reels anteriores (los publicados en Instagram antes de activar YouTube) ----------
COLA_YT = os.path.join(RAIZ, "cola_yt")             # <carpeta>.json que deja la PC, con el video en el release
YT_ANTERIORES = os.path.join(RAIZ, "yt_anteriores.csv")
COLS_YT_ANT = ["carpeta", "yt_video_id", "publicar_en", "subido"]
ANTERIORES_HORA = (15, 0)
ANTERIORES_MAX_DIA = 2   # cada subida gasta 1.600 de los 10.000 puntos diarios; quedan de sobra para los Shorts nuevos


def dia_cupo(fecha_col):
    """El cupo de la API de YouTube se reinicia a medianoche de California, no de Colombia."""
    return fecha_col.replace(tzinfo=TZ).astimezone(ZoneInfo("America/Los_Angeles")).date()


def subir_anteriores():
    if not YT_SUBIDA_ACTIVA or not os.path.isdir(COLA_YT):
        return
    trabajos = []
    for f in os.listdir(COLA_YT):
        if f.endswith(".json"):
            d = json.load(open(os.path.join(COLA_YT, f), encoding="utf-8"))
            trabajos.append((d.get("orden", 99), f, d))
    if not trabajos:
        return
    hechas = []
    if os.path.exists(YT_ANTERIORES):
        with open(YT_ANTERIORES, encoding="utf-8", newline="") as fh:
            hechas = list(csv.DictReader(fh))
    hoy = dia_cupo(ahora())
    if sum(1 for h in hechas if h.get("subido") and dia_cupo(datetime.strptime(h["subido"], "%Y-%m-%d %H:%M")) == hoy) \
            >= ANTERIORES_MAX_DIA:
        return
    _, archivo, d = sorted(trabajos, key=lambda t: (t[0], t[1]))[0]
    ruta_json = os.path.join(COLA_YT, archivo)
    if d["carpeta"] in {h["carpeta"] for h in hechas if h.get("yt_video_id")}:
        os.remove(ruta_json)
        return
    a = asset_de(d["asset"])
    if not a:
        log(f"Short anterior {d['carpeta']}: no esta el video {d['asset']} en GitHub; lo saco de la cola")
        os.remove(ruta_json)
        return
    # hueco: 15:00 del primer dia libre, despues del ultimo programado y con 3 h de margen
    cuando = ahora().replace(hour=ANTERIORES_HORA[0], minute=ANTERIORES_HORA[1], second=0, microsecond=0)
    if cuando < ahora() + timedelta(hours=3):
        cuando += timedelta(days=1)
    usados = [datetime.strptime(h["publicar_en"], "%Y-%m-%d %H:%M") for h in hechas if h.get("publicar_en")]
    if usados and max(usados) >= cuando:
        cuando = max(usados).replace(hour=ANTERIORES_HORA[0], minute=ANTERIORES_HORA[1]) + timedelta(days=1)
    previo = yt_ya_subido(d["titulo"][:100])
    if previo:
        res = {"id": previo}
        http("PUT", "https://www.googleapis.com/youtube/v3/videos?part=status",
             json.dumps({"id": previo, "status": {"privacyStatus": "private", "publishAt": cuando.replace(tzinfo=TZ).isoformat(),
                                                  "selfDeclaredMadeForKids": False}}).encode(),
             {"Authorization": f"Bearer {yt_token()}", "Content-Type": "application/json"})
        log(f"Short anterior {d['carpeta']} ya estaba en YouTube ({previo}); se reutiliza")
    else:
        res = None
    video = b"" if res else urllib.request.urlopen(a["browser_download_url"], timeout=600).read()
    meta_json = json.dumps({"snippet": {"title": d["titulo"][:100], "description": d["descripcion"], "categoryId": "22",
                                        "defaultLanguage": "es"},
                            "status": {"privacyStatus": "private", "publishAt": cuando.replace(tzinfo=TZ).isoformat(),
                                       "selfDeclaredMadeForKids": False}}).encode()
    limite = "----caminosdivinos"
    cuerpo = (f"--{limite}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n").encode() + meta_json + \
             (f"\r\n--{limite}\r\nContent-Type: video/mp4\r\n\r\n").encode() + video + f"\r\n--{limite}--".encode()
    try:
        res = res or http("POST", "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart&part=snippet,status",
                          cuerpo, {"Authorization": f"Bearer {yt_token()}", "Content-Type": f"multipart/related; boundary={limite}"})
    except ErrorAPI as e:
        if "quota" in str(e).lower():
            return  # cupo del dia agotado: se intenta en el proximo dia de cupo
        raise
    hechas.append({"carpeta": d["carpeta"], "yt_video_id": res["id"], "publicar_en": cuando.strftime("%Y-%m-%d %H:%M"),
                   "subido": ahora().strftime("%Y-%m-%d %H:%M")})
    with open(YT_ANTERIORES, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS_YT_ANT, lineterminator="\n")
        w.writeheader()
        w.writerows({k: h.get(k, "") for k in COLS_YT_ANT} for h in hechas)
    os.remove(ruta_json)
    github("DELETE", f"/releases/assets/{a['id']}")
    log(f"Short anterior {d['carpeta']} subido y programado para {cuando:%Y-%m-%d %H:%M}: https://youtube.com/shorts/{res['id']}")


# ---------- comentario con pregunta en cada Short (Cesar lo fija a mano desde la app) ----------
COMENTARIOS_YT = os.path.join(RAIZ, "comentarios_yt.json")   # {carpeta: texto} para Shorts sin yt_comentario
YT_COMENTADOS = os.path.join(RAIZ, "yt_comentados.csv")


def comentar_shorts(filas):
    if not YT_SUBIDA_ACTIVA:
        return
    hechos = set()
    if os.path.exists(YT_COMENTADOS):
        with open(YT_COMENTADOS, encoding="utf-8", newline="") as fh:
            hechos = {r["yt_video_id"] for r in csv.DictReader(fh)}
    extra = json.load(open(COMENTARIOS_YT, encoding="utf-8")) if os.path.exists(COMENTARIOS_YT) else {}
    candidatos = [(r["carpeta"], r["yt_video_id"], r["fecha_hora"]) for r in filas
                  if r.get("yt_video_id") and r.get("franja") != CARRUSEL]
    if os.path.exists(YT_ANTERIORES):
        with open(YT_ANTERIORES, encoding="utf-8", newline="") as fh:
            candidatos += [(h["carpeta"], h["yt_video_id"], h["publicar_en"]) for h in csv.DictReader(fh) if h.get("yt_video_id")]
    for carpeta, vid, cuando in candidatos:
        if vid in hechos:
            continue
        salida = datetime.strptime(cuando, "%Y-%m-%d %H:%M")
        if not (salida <= ahora() <= salida + timedelta(days=3)):   # solo cuando ya salio, y no Shorts viejos
            continue
        texto = extra.get(carpeta, "")
        ruta = os.path.join(COLA, "textos", f"{carpeta}.json")
        if not texto and os.path.exists(ruta):
            texto = json.load(open(ruta, encoding="utf-8")).get("yt_comentario", "")
        if not texto:
            continue
        try:
            res = http("POST", "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet",
                       json.dumps({"snippet": {"videoId": vid, "topLevelComment": {"snippet": {"textOriginal": texto}}}}).encode(),
                       {"Authorization": f"Bearer {yt_token()}", "Content-Type": "application/json"})
        except ErrorAPI as e:
            if ahora().minute < 15:   # sin permiso de comentar todavia: se avisa una vez por hora, no cada ciclo
                log(f"No se pudo comentar el Short {carpeta}: {str(e)[:150]}")
            return
        nuevo = not os.path.exists(YT_COMENTADOS)
        with open(YT_COMENTADOS, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            if nuevo:
                w.writerow(["carpeta", "yt_video_id", "comentario_id", "fecha"])
            w.writerow([carpeta, vid, res.get("id", ""), ahora().strftime("%Y-%m-%d %H:%M")])
        log(f"Comentario publicado en el Short {carpeta}: «{texto}» (fijalo desde la app de YouTube)")


# ---------- miniatura de cada Short (la misma portada de Instagram, la que usa el calendario web) ----------
YT_MINIATURAS = os.path.join(RAIZ, "yt_miniaturas.csv")
MINIATURA_MANUAL = {"2v5sb-Kz_N4"}   # Talento enterrado: Cesar la subio a mano el 16 nov


def poner_miniaturas(filas):
    if not YT_SUBIDA_ACTIVA:
        return
    hechos = set(MINIATURA_MANUAL)
    if os.path.exists(YT_MINIATURAS):
        with open(YT_MINIATURAS, encoding="utf-8", newline="") as fh:
            hechos |= {r["yt_video_id"] for r in csv.DictReader(fh)}
    candidatos = [(r["carpeta"], r["yt_video_id"]) for r in filas if r.get("yt_video_id") and r.get("franja") != CARRUSEL]
    if os.path.exists(YT_ANTERIORES):
        with open(YT_ANTERIORES, encoding="utf-8", newline="") as fh:
            candidatos += [(h["carpeta"], h["yt_video_id"]) for h in csv.DictReader(fh) if h.get("yt_video_id")]
    for carpeta, vid in candidatos[:]:
        if vid in hechos:
            continue
        img = os.path.join(RAIZ, "docs", "portadas", f"{carpeta}.jpg")
        if not os.path.exists(img):
            continue
        try:
            http("POST", f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={vid}&uploadType=media",
                 open(img, "rb").read(), {"Authorization": f"Bearer {yt_token()}", "Content-Type": "image/jpeg"})
        except ErrorAPI as e:
            if ahora().minute < 15:
                log(f"No se pudo poner la miniatura del Short {carpeta}: {str(e)[:150]}")
            return
        nuevo = not os.path.exists(YT_MINIATURAS)
        with open(YT_MINIATURAS, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            if nuevo:
                w.writerow(["carpeta", "yt_video_id", "fecha"])
            w.writerow([carpeta, vid, ahora().strftime("%Y-%m-%d %H:%M")])
        hechos.add(vid)
        log(f"Miniatura puesta en el Short {carpeta}")


from analisis import escribir_rendimiento  # noqa: E402  (lee calendario, mediciones y fichas)


SEGUIDORES = os.path.join(RAIZ, "seguidores.csv")


def registrar_seguidores():
    """Anota cada hora cuantos seguidores tiene la cuenta. La API no da seguidores por Reel; con este registro se
    estima cuantos llegaron en las 24 h siguientes a cada publicacion (con un Reel al dia, la atribucion es clara)."""
    filas = []
    if os.path.exists(SEGUIDORES):
        with open(SEGUIDORES, encoding="utf-8", newline="") as f:
            filas = list(csv.reader(f))[1:]
    if filas:
        ultima = datetime.strptime(filas[-1][0], "%Y-%m-%d %H:%M")
        if (ahora() - ultima).total_seconds() < 55 * 60:
            return
    n = meta("GET", ENV["IG_USER_ID"], {"fields": "followers_count"}).get("followers_count")
    if n is None:
        return
    nuevo = not os.path.exists(SEGUIDORES)
    with open(SEGUIDORES, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha", "seguidores"])
        w.writerow([ahora().strftime("%Y-%m-%d %H:%M"), n])


def main():
    if os.path.exists(ALERTA):
        os.remove(ALERTA)
    filas = leer_cal()
    for paso in (arreglo_19nov, tomar_cola, subir_shorts_pendientes, publicar_vencidos, medir_hitos, comentar_shorts, poner_miniaturas):
        try:
            if paso(filas):
                guardar_cal(filas)
        except Exception as e:
            log(f"ERROR en {paso.__name__}: {type(e).__name__}: {e}")
    for diario in (subir_anteriores, registrar_seguidores, metricas_diarias, metricas_youtube, escribir_rendimiento):
        try:
            diario()
        except Exception as e:
            log(f"ERROR en {diario.__name__}: {type(e).__name__}: {e}")
            if "OAuthException" in str(e) or "(190)" in str(e) or "code 190" in str(e):
                alerta(f"La llave de Instagram/Facebook dejo de funcionar: {str(e)[:300]}")


if __name__ == "__main__":
    main()
