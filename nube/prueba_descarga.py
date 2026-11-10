"""Prueba sin publicar: Instagram descarga un video y su portada desde GitHub y los procesa.
No llama a media_publish (el contenedor vence solo)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import ciclo as c

carpeta = os.environ.get("CARPETA_PRUEBA", "2026-11-09-si-entonces")
v, p = c.asset_de(f"{carpeta}.mp4"), c.asset_de(f"{carpeta}.jpg")
if not v or not p:
    c.log(f"PRUEBA PORTADA: falta en GitHub {'el video' if not v else ''} {'la portada' if not p else ''} de {carpeta}")
    sys.exit(1)
cont, st = c.crear_contenedor(c.ENV["IG_USER_ID"], v["browser_download_url"], "prueba", p["browser_download_url"])
c.log(f"PRUEBA PORTADA {carpeta}: {st.get('status_code')} {st.get('status', '')} (no se publico nada)")
