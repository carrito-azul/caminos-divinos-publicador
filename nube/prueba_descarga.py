"""Prueba sin publicar: Instagram descarga un video desde GitHub y lo procesa. No llama a media_publish."""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import ciclo as c

nombre = "2026-11-09-ceguera-eleccion.mp4"
a = c.asset_de(nombre)
if not a:
    c.log(f"PRUEBA: no esta {nombre} en GitHub"); sys.exit(1)
ig, url = c.ENV["IG_USER_ID"], a["browser_download_url"]
resultado = "FALLO"
for via in ("file_url", "video_url"):
    try:
        if via == "file_url":
            cont = c.meta("POST", f"{ig}/media", {"media_type": "REELS", "upload_type": "resumable", "caption": "prueba"})["id"]
            res = c.http("POST", f"https://rupload.facebook.com/ig-api-upload/{c.ENV['GRAPH_VERSION'] or 'v25.0'}/{cont}",
                         b"", {"Authorization": f"OAuth {c.ENV['FB_PAGE_TOKEN']}", "file_url": url})
            if not res.get("success"):
                raise c.ErrorAPI(str(res))
        else:
            cont = c.meta("POST", f"{ig}/media", {"media_type": "REELS", "video_url": url, "caption": "prueba"})["id"]
        for _ in range(20):
            time.sleep(30)
            st = c.meta("GET", cont, {"fields": "status_code,status"})
            if st.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
                break
        c.log(f"PRUEBA via {via}: {st.get('status_code')} {st.get('status', '')}")
        if st.get("status_code") == "FINISHED":
            resultado = f"OK por {via}"
            break
    except Exception as e:
        c.log(f"PRUEBA via {via}: error {e}")
c.log(f"PRUEBA RESULTADO: {resultado} (no se publico nada; el contenedor vence solo)")
