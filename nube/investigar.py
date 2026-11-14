"""Investigacion semanal: revisa las cuentas referentes del nicho con Business Discovery (API oficial)
y escribe investigacion/semanal.md con los reels excepcionales (3x o mas su mediana).
Solo lee datos publicos. No publica nada."""
import csv, json, os, statistics, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARP = os.path.join(RAIZ, "investigacion")
TOKEN, IG = os.environ["FB_PAGE_TOKEN"], os.environ["IG_USER_ID"]
VER = os.environ.get("GRAPH_VERSION") or "v25.0"
PAUSA = 20          # segundos entre cuentas (el limite de Meta corto la prueba en ~60 seguidas)
UMBRAL = 3.0        # veces su mediana
DIAS_RECIENTES = 45 # reels excepcionales a mostrar
AHORA = datetime.now(timezone.utc)
CAMPOS = ("business_discovery.username({u}){{followers_count,media.limit(50)"
          "{{timestamp,media_product_type,view_count,like_count,comments_count,permalink,caption}}}}")


def consultar(u):
    q = urllib.parse.urlencode({"fields": CAMPOS.format(u=u), "access_token": TOKEN})
    with urllib.request.urlopen(f"https://graph.facebook.com/{VER}/{IG}?{q}", timeout=60) as r:
        return json.loads(r.read().decode())["business_discovery"]


def dias(ts):
    return (AHORA - datetime.fromisoformat(ts.replace("+0000", "+00:00"))).days


def main():
    cuentas = [l.strip().lstrip("@") for l in open(os.path.join(CARP, "cuentas.txt"), encoding="utf-8")
               if l.strip() and not l.startswith("#")]
    filas, errores, casos = [], [], []
    for i, u in enumerate(cuentas):
        if i:
            time.sleep(PAUSA)
        try:
            b = consultar(u)
        except urllib.error.HTTPError as e:
            errores.append(f"{u}: HTTP {e.code} {e.read().decode(errors='ignore')[:120]}")
            if e.code in (400, 403) and "limit" in str(errores[-1]).lower():
                time.sleep(300)
            continue
        except Exception as e:
            errores.append(f"{u}: {e}")
            continue
        reels = [m for m in b.get("media", {}).get("data", [])
                 if m.get("media_product_type") == "REELS" and "view_count" in m]
        viejos = [m["view_count"] for m in reels if dias(m["timestamp"]) >= 7]
        med = statistics.median(viejos) if len(viejos) >= 5 else None
        activo = bool(reels) and min(dias(m["timestamp"]) for m in reels) <= 30
        for m in reels:
            veces = round(m["view_count"] / med, 1) if med else ""
            filas.append([AHORA.date().isoformat(), u, b.get("followers_count"), m["timestamp"][:10],
                          m["view_count"], m.get("like_count", ""), m.get("comments_count", ""), veces,
                          m["permalink"], (m.get("caption") or "").replace("\n", " ")[:140]])
            if (med and med >= 500 and activo and dias(m["timestamp"]) >= 7
                    and dias(m["timestamp"]) <= DIAS_RECIENTES and veces >= UMBRAL):
                casos.append((veces, u, m))

    with open(os.path.join(CARP, "reels.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["consulta", "cuenta", "seguidores", "fecha", "vistas", "me_gusta", "comentarios",
                    "veces_su_mediana", "enlace", "texto"])
        w.writerows(filas)

    casos.sort(key=lambda c: -c[0])
    lin = [f"# Investigación semanal ({AHORA:%Y-%m-%d})", "",
           "Lo escribe solo la nube cada semana. Reels excepcionales de las cuentas referentes "
           f"(`investigacion/cuentas.txt`): {UMBRAL:g}× o más la mediana de su creador, publicados hace 7 a "
           f"{DIAS_RECIENTES} días. Son **casos para estudiar**, no reglas: las hipótesis viven en "
           "`investigacion.md`. Ojo: un reel puede dispararse por una colaboración con alguien famoso; "
           "eso no se replica. Se toma el mecanismo, nunca la frase ni el guion.", "",
           f"Cuentas revisadas: {len(cuentas) - len(errores)} de {len(cuentas)}.", "",
           "| Veces su normal | Cuenta | Vistas | Fecha | Cómo empieza el texto del post | Enlace |",
           "|---|---|---|---|---|---|"]
    for veces, u, m in casos[:20]:
        txt = (m.get("caption") or "").replace("\n", " ").replace("|", "/")[:110]
        vistas = f"{m['view_count']:,}".replace(",", ".")
        lin.append(f"| {veces:g}× | {u} | {vistas} | {m['timestamp'][:10]} | {txt} | {m['permalink']} |")
    if not casos:
        lin.append("| — | Ningún reel excepcional esta semana | | | | |")
    if errores:
        lin += ["", "Cuentas que no respondieron (revisar si cambiaron de nombre o dejaron de ser profesionales):", ""]
        lin += [f"- {e}" for e in errores]
    with open(os.path.join(CARP, "semanal.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lin) + "\n")
    print("\n".join(lin))


if __name__ == "__main__":
    main()
