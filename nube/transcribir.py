"""Transcribe lo que se DICE en los reels virales de las cuentas referentes (y una muestra de reels normales para
comparar). Usa Business Discovery (API oficial) para obtener el archivo de cada reel publico, lo transcribe con
faster-whisper y escribe investigacion/transcripciones.csv. Solo para analizar: no se publica ni se guarda ningun video."""
import csv, json, os, random, statistics, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARP = os.path.join(RAIZ, "investigacion")
TOKEN, IG = os.environ["FB_PAGE_TOKEN"], os.environ["IG_USER_ID"]
VER = os.environ.get("GRAPH_VERSION") or "v25.0"
SALIDA = os.path.join(CARP, "transcripciones.csv")
PAUSA = 20
MAX_NORMALES_POR_CUENTA = 4
COLS = ["grupo", "cuenta", "seguidores", "fecha", "vistas", "veces_su_mediana", "duracion_s", "idioma", "enlace",
        "descripcion", "transcripcion", "nota"]


def bd(u, after=None):
    campos = "business_discovery.username(%s){followers_count,media.limit(50)%s{permalink,media_url,media_product_type}}" % (
        u, f".after({after})" if after else "")
    q = urllib.parse.urlencode({"fields": campos, "access_token": TOKEN})
    with urllib.request.urlopen(f"https://graph.facebook.com/{VER}/{IG}?{q}", timeout=60) as r:
        return json.loads(r.read().decode())["business_discovery"]


def urls_de(u, buscados):
    """permalink -> media_url para los reels buscados de la cuenta u (hasta 3 paginas de 50)."""
    encontrados, after = {}, None
    for _ in range(3):
        b = bd(u, after)
        media = b.get("media", {})
        for m in media.get("data", []):
            if m.get("permalink") in buscados:
                encontrados[m["permalink"]] = m.get("media_url")
        after = media.get("paging", {}).get("cursors", {}).get("after")
        if len(encontrados) == len(buscados) or not after:
            break
        time.sleep(5)
    return encontrados


def main():
    from faster_whisper import WhisperModel   # si falla, que falle antes de consultar la API
    filas = list(csv.DictReader(open(os.path.join(CARP, "reels.csv"), encoding="utf-8")))
    for f in filas:
        f["x"] = float(f["veces_su_mediana"] or 0)
        f["v"] = int(f["vistas"] or 0)
    virales = [f for f in filas if f["x"] >= 3 and f["v"] >= 10000]
    random.seed(7)
    normales = []
    for u in sorted({f["cuenta"] for f in filas}):
        pool = [f for f in filas if f["cuenta"] == u and 0 < f["x"] < 1.5]
        normales += random.sample(pool, min(MAX_NORMALES_POR_CUENTA, len(pool)))
    objetivo = [("viral", f) for f in sorted(virales, key=lambda f: -f["x"])] + [("normal", f) for f in normales]
    print(f"A transcribir: {len(virales)} virales y {len(normales)} normales", flush=True)

    hechos = set()
    if os.path.exists(SALIDA):
        hechos = {r["enlace"] for r in csv.DictReader(open(SALIDA, encoding="utf-8")) if r["transcripcion"] or r["nota"]}
    pendientes = [(g, f) for g, f in objetivo if f["enlace"] not in hechos]

    # 1) enlaces de los archivos, cuenta por cuenta
    urls = {}
    por_cuenta = {}
    for g, f in pendientes:
        por_cuenta.setdefault(f["cuenta"], set()).add(f["enlace"])
    for i, (u, buscados) in enumerate(sorted(por_cuenta.items())):
        if i:
            time.sleep(PAUSA)
        try:
            urls.update(urls_de(u, buscados))
            print(f"{u}: {sum(1 for p in buscados if urls.get(p))}/{len(buscados)} archivos", flush=True)
        except urllib.error.HTTPError as e:
            print(f"{u}: HTTP {e.code} {e.read().decode(errors='ignore')[:150]}", flush=True)
            if e.code in (400, 403):
                time.sleep(120)
        except Exception as e:
            print(f"{u}: {e}", flush=True)

    # 2) transcripcion
    modelo = WhisperModel("small", device="cpu", compute_type="int8")
    nuevo = not os.path.exists(SALIDA)
    out = open(SALIDA, "a", encoding="utf-8", newline="")
    w = csv.DictWriter(out, fieldnames=COLS)
    if nuevo:
        w.writeheader()
    tmp = tempfile.mkdtemp()
    for n, (g, f) in enumerate(pendientes, 1):
        fila = {"grupo": g, "cuenta": f["cuenta"], "seguidores": f["seguidores"], "fecha": f["fecha"],
                "vistas": f["vistas"], "veces_su_mediana": f["veces_su_mediana"], "enlace": f["enlace"],
                "descripcion": f["texto"], "duracion_s": "", "idioma": "", "transcripcion": "", "nota": ""}
        url = urls.get(f["enlace"])
        if not url:
            fila["nota"] = "la API no entrego el archivo"
            w.writerow(fila); out.flush()
            continue
        try:
            video, audio = os.path.join(tmp, "v.mp4"), os.path.join(tmp, "a.wav")
            with urllib.request.urlopen(url, timeout=120) as r, open(video, "wb") as fh:
                fh.write(r.read())
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-ac", "1", "-ar", "16000", audio], check=True)
            segs, info = modelo.transcribe(audio, beam_size=1, vad_filter=True)
            texto = " ".join(s.text.strip() for s in segs).strip()
            fila.update(duracion_s=round(info.duration, 1), idioma=info.language, transcripcion=texto,
                        nota="" if texto else "sin voz (solo musica o texto en pantalla)")
        except Exception as e:
            fila["nota"] = f"error: {str(e)[:120]}"
        w.writerow(fila); out.flush()
        print(f"{n}/{len(pendientes)} {g} {f['cuenta']} {fila['duracion_s']}s {fila['nota'] or fila['transcripcion'][:60]}",
              flush=True)
    out.close()


if __name__ == "__main__":
    main()
