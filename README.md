# Caminos Divinos - publicador automatico

- `calendario.csv`: que se publica y cuando. Para pausar un video, cambia `aprobado` a `No`.
- `historico.csv`: metricas diarias de todas las publicaciones de Instagram.
- `registro.log`: lo que hizo el sistema en cada ciclo.
- Los videos se guardan temporalmente en el release `videos` y se borran al publicarse.
- Los tokens estan en Settings > Secrets (cifrados, no visibles).
