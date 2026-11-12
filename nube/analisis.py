"""Convierte las mediciones en instrucciones para el siguiente guion: escribe rendimiento.md.

Une tres cosas:
  - fichas/<carpeta>.json   lo que se decidio al crear el video (gancho, tema, duracion...), lo escribe CODE ART
  - mediciones.csv          como le fue en 1 h, 3 h, 24 h, 72 h y 7 dias
  - calendario.csv          cuando salio y en que franja
CODE ART lee rendimiento.md antes de escribir cada guion (regla en su AGENTS.md).
"""
import csv, json, os, statistics
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Bogota")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL, MED, HIST, FICHAS, REND = (os.path.join(RAIZ, n) for n in
                                ("calendario.csv", "mediciones.csv", "historico.csv", "fichas", "rendimiento.md"))

# Valores que puede tener cada variable de la ficha (CODE ART elige uno). Sirve para saber que falta probar.
CATALOGO = {
    "gancho_tipo": ["pregunta_directa", "reto_al_espectador", "dato_impactante", "imagina", "historia",
                    "afirmacion_contraria", "promesa"],
    "cierre_tipo": ["pregunta", "reflexion", "llamado_seguir", "llamado_comentar", "llamado_guardar", "llamado_enviar"],
}
VARIABLES = [("gancho_tipo", "Tipo de gancho"), ("emocion", "Emoción principal"), ("categoria", "Categoría"),
             ("cierre_tipo", "Tipo de cierre"), ("relato_biblico", "Relato bíblico"), ("estilo_visual", "Estilo visual"), ("duracion", "Duración")]
# Metas: lo que suelen tener los Reels que Instagram sigue empujando a no seguidores (referencia general,
# no sale de tus datos; se ajusta cuando haya suficientes videos propios).
META_SALTO = 40       # % que lo pasa en los primeros segundos: menos es mejor
META_RETENCION = 50   # % del video que se ve en promedio
META_COMPARTIDOS = 5  # compartidos por cada 1.000 cuentas alcanzadas
META_GUARDADOS = 10   # guardados por cada 1.000 cuentas alcanzadas
OBJETIVO = 50000


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _leer_csv(ruta):
    if not os.path.exists(ruta):
        return []
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _fichas():
    out = {}
    if os.path.isdir(FICHAS):
        for n in sorted(os.listdir(FICHAS)):
            if n.endswith(".json"):
                try:
                    out[n[:-5]] = json.load(open(os.path.join(FICHAS, n), encoding="utf-8"))
                except ValueError:
                    pass
    return out


def _confianza(n):
    return "pista (1 video)" if n < 2 else "pista" if n < 3 else "tendencia" if n < 10 else "regla"


def _bucket_duracion(s):
    s = _num(s)
    if s is None:
        return ""
    return "menos de 40 s" if s < 40 else "40-50 s" if s < 50 else "50-60 s" if s < 60 else "más de 60 s"


def _videos():
    """Una entrada por video publicado por el sistema (o con ficha), con su mejor dato disponible."""
    cal = {r["carpeta"]: r for r in _leer_csv(CAL)}
    fichas = _fichas()
    meds = {}
    for m in _leer_csv(MED):
        meds.setdefault(m["carpeta"], {})[m["hito"]] = m
    vids = []
    for carpeta in sorted(set(fichas) | {c for c, r in cal.items() if r.get("estado") == "publicado"}):
        r = cal.get(carpeta, {})
        if r.get("estado") != "publicado":
            continue
        f = fichas.get(carpeta, {})
        hitos = meds.get(carpeta, {})
        orden = ["7d", "72h", "48h", "24h", "4h", "3h", "1h"]
        ultimo = next((h for h in orden if h in hitos), None)
        m24 = hitos.get("24h") or hitos.get("48h")  # 48h: dato aproximado de videos anteriores al sistema
        dur = _num(f.get("duracion_s"))
        def deriv(m):
            if not m:
                return {}
            v, alc, watch = _num(m.get("views")), _num(m.get("reach")), _num(m.get("avg_watch_s"))
            return {"views": v, "reach": alc, "skip": _num(m.get("skip_rate")), "watch": watch,
                    "retencion": round(watch / dur * 100) if watch is not None and dur else None,
                    "shares": _num(m.get("shares")), "saved": _num(m.get("saved")),
                    "comp_1000": round(_num(m.get("shares")) / alc * 1000, 1) if alc and _num(m.get("shares")) is not None else None,
                    "guard_1000": round(_num(m.get("saved")) / alc * 1000, 1) if alc and _num(m.get("saved")) is not None else None}
        vids.append({"carpeta": carpeta, "ficha": f, "cal": r, "hitos": hitos, "ultimo": ultimo,
                     "aprox24": "24h" not in hitos and "48h" in hitos,
                     "d24": deriv(m24), "dult": deriv(hitos.get(ultimo)) if ultimo else {},
                     "duracion": _bucket_duracion(f.get("duracion_s"))})
    return vids


def _linea_base():
    """Reels anteriores al sistema (2026), para tener contra que comparar."""
    hist = _leer_csv(HIST)
    if not hist:
        return None
    ult = max(r["fecha_consulta"] for r in hist)
    reels = [r for r in hist if r["fecha_consulta"] == ult and r.get("tipo") == "REELS"
             and r.get("fecha_publicacion", "") >= "2026-01-01" and _num(r.get("views"))]
    if not reels:
        return None
    vs = [_num(r["views"]) for r in reels]
    sk = [_num(r["reels_skip_rate"]) for r in reels if _num(r.get("reels_skip_rate")) is not None]
    wt = [_num(r["avg_watch_time_s"]) for r in reels if _num(r.get("avg_watch_time_s")) is not None]
    return {"n": len(reels), "mediana_views": statistics.median(vs), "max_views": max(vs),
            "mediana_salto": statistics.median(sk) if sk else None, "mediana_watch": statistics.median(wt) if wt else None}


def _a_tiempo(r, max_min=45):
    try:
        prog = datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M")
        real = datetime.strptime(r.get("publicado_real") or r["fecha_hora"], "%Y-%m-%d %H:%M")
        return abs((real - prog).total_seconds()) <= max_min * 60
    except (KeyError, ValueError):
        return True


def _f(x, suf="", dec=0):
    if x is None or x == "":
        return "—"
    return f"{x:,.{dec}f}".replace(",", ".") + suf


def construir():
    vids = _videos()
    con24 = [v for v in vids if v["d24"].get("views") is not None]
    base = _linea_base()
    L = []
    L.append("# Rendimiento: lo que dicen los datos")
    L.append("")
    L.append("Lo escribe solo la nube (GitHub) cada vez que llega una medición nueva. **No lo edites a mano.** "
             "CODE ART lo lee antes de escribir cada guion (`AGENTS.md`). Lo que dice aquí son datos; lo que "
             "el creador pide sigue mandando.")
    ultima = max((m["fecha"] for v in vids for m in v["hitos"].values()), default="")
    L.append("")
    L.append(f"Última medición: {ultima or 'todavía ninguna'} · Videos medidos: {len(vids)} · "
             f"Con dato de 24 h: {len(con24)} · Meta: {OBJETIVO:,} reproducciones en un video".replace(",", "."))

    # --- Récord y metas
    L += ["", "## Dónde estamos"]
    if vids:
        rec = max(vids, key=lambda v: v["dult"].get("views") or 0)
        L.append(f"- **Récord del sistema:** `{rec['carpeta']}` con {_f(rec['dult'].get('views'))} reproducciones "
                 f"(medición {rec['ultimo']}). Gancho: «{rec['ficha'].get('gancho_texto', '¿?')}».")
    if base:
        L.append(f"- **Línea base (Reels de 2026 antes del sistema, {base['n']}):** mediana {_f(base['mediana_views'])} "
                 f"reproducciones, salto {_f(base['mediana_salto'], ' %')}, {_f(base['mediana_watch'], ' s', 1)} de "
                 f"reproducción media; el mejor llegó a {_f(base['max_views'])}.")
    L.append(f"- **Metas por video** (referencia general hasta tener datos propios): salto < {META_SALTO} %, "
             f"retención > {META_RETENCION} % del video, ≥ {META_COMPARTIDOS} compartidos y ≥ {META_GUARDADOS} "
             f"guardados por cada 1.000 cuentas alcanzadas. Los compartidos son lo que más empuja a no seguidores.")

    # --- Tabla por video
    L += ["", "## Cada video", "",
          "| Video | Salió | Gancho (tipo) | Dur. | Vistas 24 h | Salto 24 h | Retención | Compart./1.000 | Guard./1.000 | Último dato |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for v in sorted(vids, key=lambda v: v["cal"].get("publicado_real") or v["cal"].get("fecha_hora", "")):
        d, du, f = v["d24"], v["dult"], v["ficha"]
        fuera = "" if _a_tiempo(v["cal"]) else " ⚠"
        ap = " ≈" if v["aprox24"] else ""
        L.append(f"| `{v['carpeta']}` | {(v['cal'].get('publicado_real') or v['cal'].get('fecha_hora', ''))[5:]}{fuera} | "
                 f"{f.get('gancho_tipo', '—')} | {_f(_num(f.get('duracion_s')), ' s')} | {_f(d.get('views'))}{ap} | "
                 f"{_f(d.get('skip'), ' %')} | {_f(d.get('retencion'), ' %')} | {_f(d.get('comp_1000'), '', 1)} | "
                 f"{_f(d.get('guard_1000'), '', 1)} | {_f(du.get('views'))} vistas ({v['ultimo'] or '—'}) |")
    if not vids:
        L.append("| (todavía no hay videos medidos) | | | | | | | | | |")
    L.append("")
    L.append("⚠ = salió más de 45 min fuera de su hora programada: cuenta para aprender del contenido, no de los horarios. "
             "≈ = sin medición de 24 h; se usa la de ~48 h (video anterior al sistema).")
    sin_ficha = [v["carpeta"] for v in vids if not v["ficha"]]
    if sin_ficha:
        L.append(f"Sin ficha (no cuentan para comparar decisiones): {', '.join(sin_ficha)}.")

    # --- Comparaciones por variable
    L += ["", "## Qué decisión funciona mejor", "",
          "Solo con videos que ya tienen dato de 24 h. Con 1-2 videos es una **pista**; con 3-9, una **tendencia**; "
          "con 10 o más, una **regla**. Se ordena por salto (lo que más predice si Instagram lo sigue mostrando)."]
    for var, nombre in VARIABLES:
        grupos = {}
        for v in con24:
            if var == "duracion":
                val = v["duracion"]
            elif var == "relato_biblico":
                val = ("con relato bíblico" if v["ficha"].get("relato_biblico") else "sin relato") if v["ficha"] else None
            else:
                val = v["ficha"].get(var)
            if val:
                grupos.setdefault(val, []).append(v)
        if not grupos:
            continue
        L += ["", f"**{nombre}**", "", "| Opción | Videos | Vistas 24 h (prom.) | Salto (prom.) | Retención | Compart./1.000 | Confianza |",
              "|---|---|---|---|---|---|---|"]
        def prom(vs, k):
            xs = [x["d24"].get(k) for x in vs if x["d24"].get(k) is not None]
            return sum(xs) / len(xs) if xs else None
        for val, vs in sorted(grupos.items(), key=lambda kv: (prom(kv[1], "skip") if prom(kv[1], "skip") is not None else 999)):
            L.append(f"| {val} | {len(vs)} | {_f(prom(vs, 'views'))} | {_f(prom(vs, 'skip'), ' %')} | "
                     f"{_f(prom(vs, 'retencion'), ' %')} | {_f(prom(vs, 'comp_1000'), '', 1)} | {_confianza(len(vs))} |")
        if var in CATALOGO:
            faltan = [x for x in CATALOGO[var] if x not in grupos]
            if faltan:
                L.append(f"\nSin datos todavía: {', '.join(faltan)}.")

    # --- Lo que esto significa para el siguiente guion
    L += ["", "## Para el siguiente guion"]
    recs = []
    if con24:
        mejor = min(con24, key=lambda v: v["d24"].get("skip") if v["d24"].get("skip") is not None else 999)
        mf = mejor["ficha"]
        if mf:
            recs.append(f"**Punto de partida:** el gancho que menos gente salta hasta ahora es el de `{mejor['carpeta']}` "
                        f"({mf.get('gancho_tipo', '?')}: «{mf.get('gancho_texto', '')}», salto "
                        f"{_f(mejor['d24'].get('skip'), ' %')}). Mantén su forma salvo en la variable que se pruebe.")
        malos = [v for v in con24 if (v["d24"].get("skip") or 0) > 70 and v["ficha"]]
        for v in malos:
            recs.append(f"**Evitar repetir tal cual:** el gancho de `{v['carpeta']}` ({v['ficha'].get('gancho_tipo')}: "
                        f"«{v['ficha'].get('gancho_texto', '')}») lo saltó el {_f(v['d24'].get('skip'), ' %')}.")
        bajos_comp = [v for v in con24 if v["d24"].get("comp_1000") is not None and v["d24"]["comp_1000"] < META_COMPARTIDOS]
        if bajos_comp and len(bajos_comp) == len(con24):
            recs.append("**Compartidos:** ningún video llega todavía a la meta. Prueba un cierre que invite a enviarlo "
                        "(«mándaselo a quien…») o un tema que la gente quiera mandarle a alguien concreto.")
    probados = {f.get("gancho_tipo") for f in _fichas().values()}  # incluye los que ya esperan turno
    faltan = [x for x in CATALOGO["gancho_tipo"] if x not in probados]
    if faltan:
        recs.append(f"**Experimento sugerido:** un tipo de gancho aún sin probar ({faltan[0]}), manteniendo lo demás "
                    "como el mejor video. Una sola variable por video: si cambian varias, no se sabe qué funcionó.")
    en_cola = [c for c, f in _fichas().items() if c not in {v["carpeta"] for v in vids}]
    if en_cola:
        recs.append("**Ya en cola (aún no publicados):** " + "; ".join(
            f"`{c}` prueba {_fichas()[c].get('gancho_tipo', '?')}" for c in en_cola) + ". No repitas esas pruebas.")
    pendientes = [v for v in vids if v["d24"].get("views") is None]
    if pendientes:
        recs.append(f"**Esperando datos:** {', '.join(p['carpeta'] for p in pendientes)} (aún sin 24 h). "
                    "No saques conclusiones de ellos todavía.")
    if not recs:
        recs.append("Todavía no hay datos suficientes: sigue el método y llena bien la ficha.")
    L += [f"- {r}" for r in recs]

    # --- Principios generales
    L += ["", "## Principios generales (no salen de tus datos; los datos mandan cuando los contradigan)",
          "- El primer segundo decide: el fotograma 0 ya en movimiento y una frase que hable directo a quien mira, "
          "escrita grande en pantalla.",
          "- El gancho, corto: una idea en menos de 3 s. Una primera frase de más de 12 palabras suele perder gente.",
          "- Pagar la promesa del gancho antes de la mitad del video; si la respuesta llega al final, muchos no llegan.",
          "- Un video que se comparte se envía *a alguien*: piensa a quién se lo mandaría quien lo ve.",
          "- 50.000 no llega de golpe por un video perfecto: llega de muchos intentos medidos. Cada video, una prueba."]
    return "\n".join(L) + "\n"


def escribir_rendimiento():
    texto = construir()
    viejo = open(REND, encoding="utf-8").read() if os.path.exists(REND) else ""
    if texto != viejo:
        with open(REND, "w", encoding="utf-8") as f:
            f.write(texto)


if __name__ == "__main__":
    escribir_rendimiento()
    print(open(REND, encoding="utf-8").read())
