"""Ciclo en la nube (GitHub Actions, cada 15 min). Hora de referencia: Colombia.

1. Pasa a calendario.csv los videos nuevos que la PC dejo en cola/ y decide su hora.
2. Publica en Instagram lo que ya llego a su hora: Instagram descarga el video desde GitHub.
3. Borra de GitHub el video ya publicado.
4. Mide cada Reel a las 24 h y, con cada dato nuevo, vuelve a decidir la hora de lo pendiente.
5. Una vez al dia guarda las metricas de todas las publicaciones en historico.csv.
"""
import csv, json, os, random, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Bogota")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL, HIST, LOG, COLA = (os.path.join(RAIZ, n) for n in ("calendario.csv", "historico.csv", "registro.log", "cola"))

# ---------- Configuracion ----------
FRANJAS = [(7, 0), (12, 30), (18, 0), (20, 0), (22, 30)]   # franjas que se prueban (hora Colombia)
EXPLORACION = 0.25          # 1 de cada 4 veces prueba otra franja distinta a la mejor
SEPARACION_MIN_HORAS = 20   # minimo entre publicaciones
MAX_POR_DIA = 1
ANTICIPACION_MIN_HORAS = 1  # un video nuevo no se publica antes de 1 h
# -----------------------------------

COLS = ["id", "fecha_hora", "carpeta", "asset", "tema", "gancho", "aprobado", "estado", "ig_media_id", "permalink",
        "detectado", "franja", "motivo_hora", "publicado_real", "views_24h", "reach_24h", "skip_24h", "watch_24h_s",
        "yt_video_id", "yt_estado"]
ENV = {k: os.environ.get(k, "") for k in ("FB_PAGE_TOKEN", "IG_USER_ID", "GRAPH_VERSION", "GH_TOKEN", "GH_REPO",
                                          "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "YT_REFRESH_TOKEN")}
HIST_YT = os.path.join(RAIZ, "historico_youtube.csv")
# Se pone en True cuando YouTube apruebe la auditoria (antes, todo lo subido por API queda privado para siempre)
YT_SUBIDA_ACTIVA = False


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


def rendimiento(filas):
    datos = {}
    for r in filas:
        if r.get("views_24h") and r.get("franja"):
            datos.setdefault(r["franja"], []).append(float(r["views_24h"]))
    return {k: sum(v) / len(v) for k, v in datos.items()}, {k: len(v) for k, v in datos.items()}


def elegir_hora(filas, desde, excluir=None):
    ocupados, ultima = {}, None
    for r in filas:
        if r is excluir or r["estado"] not in ("pendiente", "publicado", "publicando") or not r.get("fecha_hora"):
            continue
        ocupados[r["fecha_hora"][:10]] = ocupados.get(r["fecha_hora"][:10], 0) + 1
        t = datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M")
        ultima = max(ultima, t) if ultima else t
    if ultima:
        desde = max(desde, ultima + timedelta(hours=SEPARACION_MIN_HORAS))
    prom, cuenta = rendimiento(filas)
    etiquetas = [f"{h:02d}:{m:02d}" for h, m in FRANJAS]
    reservadas = {r.get("franja") for r in filas if r is not excluir and r["estado"] in ("pendiente", "publicado")}
    sin_datos = [e for e in etiquetas if cuenta.get(e, 0) == 0 and e not in reservadas]
    if sin_datos:
        objetivo, motivo = sin_datos, "exploracion: franja aun sin datos"
    elif not prom:
        objetivo, motivo = etiquetas, "esperando datos de 24 h"
    else:
        mejor = max(prom, key=prom.get)
        if random.random() < EXPLORACION and len(etiquetas) > 1:
            objetivo = [random.choice([e for e in etiquetas if e != mejor])]
            motivo = f"exploracion: se vuelve a probar {objetivo[0]} (la mejor sigue siendo {mejor})"
        else:
            objetivo = [mejor]
            motivo = f"mejor franja: {mejor} ({prom[mejor]:.0f} vistas a 24 h, {cuenta[mejor]} Reels)"
    t = desde.replace(second=0, microsecond=0, minute=(desde.minute // 15) * 15)
    for _ in range(14 * 96):
        e = f"{t.hour:02d}:{t.minute:02d}"
        if e in objetivo and t >= desde and ocupados.get(t.strftime("%Y-%m-%d"), 0) < MAX_POR_DIA:
            return t, e, motivo
        t += timedelta(minutes=15)
    return desde, "", "sin hueco en 14 dias"


# ---------- pasos ----------
def tomar_cola(filas):
    if not os.path.isdir(COLA):
        return False
    cambio = False
    for nombre in sorted(os.listdir(COLA)):
        if not nombre.endswith(".json"):
            continue
        ruta = os.path.join(COLA, nombre)
        d = json.load(open(ruta, encoding="utf-8"))
        if not any(r["carpeta"] == d["carpeta"] for r in filas):
            t, e, motivo = elegir_hora(filas, ahora() + timedelta(hours=ANTICIPACION_MIN_HORAS))
            filas.append({"id": f"CD-{d['carpeta']}", "carpeta": d["carpeta"], "asset": d["asset"], "tema": d["tema"],
                          "gancho": d["ig"].splitlines()[0][:120], "aprobado": "Si", "estado": "pendiente",
                          "detectado": d.get("detectado", ""), "fecha_hora": t.strftime("%Y-%m-%d %H:%M"),
                          "franja": e, "motivo_hora": motivo})
            log(f"Agendado {d['carpeta']} -> {t:%Y-%m-%d %H:%M} ({motivo})")
        os.makedirs(os.path.join(COLA, "textos"), exist_ok=True)
        os.replace(ruta, os.path.join(COLA, "textos", nombre))  # se conserva el texto para publicar
        cambio = True
    return cambio


def publicar(r):
    d = json.load(open(os.path.join(COLA, "textos", f"{r['carpeta']}.json"), encoding="utf-8"))
    a = asset_de(r["asset"])
    if not a:
        raise ErrorAPI(f"No esta el video {r['asset']} en GitHub")
    ig = ENV["IG_USER_ID"]
    url = a["browser_download_url"]
    # Instagram descarga el video desde GitHub (via probada el 9 nov 2026)
    cont = meta("POST", f"{ig}/media", {"media_type": "REELS", "video_url": url,
                                          "caption": d["ig"], "share_to_feed": "true"})["id"]
    log(f"{r['carpeta']}: contenedor {cont}, Instagram procesando el video")
    for _ in range(20):  # hasta 10 minutos
        time.sleep(30)
        st = meta("GET", cont, {"fields": "status_code,status"})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise ErrorAPI(f"Instagram rechazo el video: {st.get('status')}")
    else:
        raise ErrorAPI("Instagram tardo mas de 10 minutos procesando")
    mid = meta("POST", f"{ig}/media_publish", {"creation_id": cont})["id"]
    link = meta("GET", mid, {"fields": "permalink"}).get("permalink", "")
    r.update(estado="publicado", ig_media_id=mid, permalink=link, publicado_real=ahora().strftime("%Y-%m-%d %H:%M"))
    log(f"PUBLICADO {r['carpeta']}: {link}")
    # Borra el video de GitHub cuando ya no hace falta para ninguna plataforma
    if not YT_SUBIDA_ACTIVA or r.get("yt_video_id"):
        github("DELETE", f"/releases/assets/{a['id']}")
        log(f"{r['asset']} borrado de GitHub")


def publicar_vencidos(filas):
    for r in filas:
        if r["estado"] == "publicando":  # una ejecucion anterior se corto: no reintentar a ciegas
            r["estado"] = "error: interrumpido, revisar en Instagram"
    for r in sorted(filas, key=lambda r: r.get("fecha_hora", "")):
        if r["estado"] != "pendiente" or r.get("aprobado", "").strip().lower() not in ("si", "sí"):
            continue
        if datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M") > ahora():
            continue
        r["estado"] = "publicando"
        guardar_cal(filas)
        try:
            publicar(r)
        except Exception as e:
            r["estado"] = f"error: {str(e)[:150]}"
            log(f"ERROR publicando {r['carpeta']}: {e}")
        return True  # uno por ciclo
    return False


def medir_24h(filas):
    cambio = False
    for r in filas:
        if r["estado"] != "publicado" or r.get("views_24h") or not r.get("ig_media_id") or not r.get("publicado_real"):
            continue
        if ahora() - datetime.strptime(r["publicado_real"], "%Y-%m-%d %H:%M") < timedelta(hours=24):
            continue
        datos = meta("GET", f"{r['ig_media_id']}/insights",
                     {"metric": "views,reach,reels_skip_rate,ig_reels_avg_watch_time"}).get("data", [])
        v = {d["name"]: d["values"][0]["value"] for d in datos if d.get("values")}
        r.update(views_24h=v.get("views", 0), reach_24h=v.get("reach", ""), skip_24h=v.get("reels_skip_rate", ""),
                 watch_24h_s=round(v["ig_reels_avg_watch_time"] / 1000, 1) if "ig_reels_avg_watch_time" in v else "")
        log(f"24 h de {r['carpeta']} ({r.get('franja')}): {r['views_24h']} vistas, salto {r['skip_24h']} %")
        cambio = True
    if cambio:  # con un dato nuevo, se vuelve a decidir la hora de lo pendiente que aun no esta cerca
        for r in sorted([r for r in filas if r["estado"] == "pendiente"], key=lambda r: r["fecha_hora"]):
            if datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M") - ahora() < timedelta(hours=2):
                continue
            antes = r["fecha_hora"]
            t, e, motivo = elegir_hora(filas, ahora() + timedelta(hours=2), excluir=r)
            r.update(fecha_hora=t.strftime("%Y-%m-%d %H:%M"), franja=e, motivo_hora=motivo)
            if r["fecha_hora"] != antes:
                log(f"Replanificado {r['carpeta']}: {antes} -> {r['fecha_hora']} ({motivo})")
    return cambio


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
    log(f"Metricas de YouTube guardadas ({len(filas)} videos)")


def subir_short(r):
    """Sube el Short programado (publishAt) a la misma hora que el Reel. Solo se usa con YT_SUBIDA_ACTIVA."""
    d = json.load(open(os.path.join(COLA, "textos", f"{r['carpeta']}.json"), encoding="utf-8"))
    a = asset_de(r["asset"])
    datos = urllib.request.urlopen(a["browser_download_url"], timeout=300).read()
    titulo = (d.get("yt_titulo") or d["tema"])[:100]
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


def subir_shorts_pendientes(filas):
    if not YT_SUBIDA_ACTIVA:
        return False
    for r in filas:
        if r["estado"] == "pendiente" and not r.get("yt_video_id") and r.get("aprobado", "").lower() in ("si", "sí"):
            try:
                subir_short(r)
            except Exception as e:
                r["yt_estado"] = f"error: {str(e)[:120]}"
                log(f"ERROR subiendo Short {r['carpeta']}: {e}")
            return True
    return False


def main():
    filas = leer_cal()
    for paso in (tomar_cola, subir_shorts_pendientes, publicar_vencidos, medir_24h):
        try:
            if paso(filas):
                guardar_cal(filas)
        except Exception as e:
            log(f"ERROR en {paso.__name__}: {type(e).__name__}: {e}")
    for diario in (metricas_diarias, metricas_youtube):
        try:
            diario()
        except Exception as e:
            log(f"ERROR en {diario.__name__}: {e}")


if __name__ == "__main__":
    main()
