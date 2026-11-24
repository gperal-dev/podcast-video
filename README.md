# PodcastForge Studio

Sistema local automatizado y ultraeconómico que transforma cualquier archivo de audio de podcast (de 10 minutos a más de 8 horas) en:
- **Shorts / Reels / TikToks (9:16)** diferenciando entre **Cliffhanger (lazo abierto e intriga)** y **Conclusivos (remate cerrado)**.
- **Vídeos de duración media (5–6 min, 16:9 o 1:1)** con arco temático completo.
- **Vídeos largos para YouTube (12 min, 16:9)** con introducción, desarrollo y capítulos automáticos.

---

## Inicio rápido (1 clic, sin programar)

1. Copia `.env.example` a `.env` (si no existe ya) y añade tus claves de **OpenRouter**, **Pexels** y **Pixabay**.
2. En **Windows**, haz doble clic en **`INICIAR_APP.bat`**.  
   En **Linux/macOS**, ejecuta **`./start.sh`**.
3. Se abrirá automáticamente tu navegador en **`http://127.0.0.1:8000`**.

---

## ¿Qué hace automáticamente por ti?

1. **Transcripción 100% local y gratuita (`faster-whisper`):**
   - *Pasada 1* rápida sobre todo el episodio para indexar frases `[S0001]`.
   - *Pasada 2* precisa palabra por palabra únicamente en los clips ganadores.
2. **IA Editorial adaptativa vía OpenRouter:**
   - Mapeo en bloques de 5 min con modelo rápido + cirugía de arcos y momentos virales con modelo editorial.
   - Conteo exacto de tokens y coste estimado en USD/EUR por trabajo.
3. **Edición quirúrgica de audio (EDL + TimeMapper):**
   - Recorta silencios largos manteniendo pausas naturales.
   - Corta palabras/frases censuradas con micro-crossfade de 20 ms (sin pitidos) y las elimina de los subtítulos.
   - Normaliza a `-14 LUFS` con música de fondo opcional.
4. **Dirección visual, imágenes propias y subtítulos `.ass`:**
   - Prioriza tus imágenes propias según sus instrucciones de uso y prioridad; completa con stock gratuito de Pexels/Pixabay cacheado en `cache/stock/`.
   - Quema subtítulos karaoke palabra por palabra respetando zonas seguras de TikTok/Reels.
5. **Tolerancia a fallos y bajo impacto en el PC:**
   - Checkpoints guardados en `processing/<job_id>/` para reanudar sin repetir transcripciones ni llamadas a IA.
   - Autodetección de aceleración hardware FFmpeg y renderizado en prioridad baja del sistema.

---

## Uso por Línea de Comandos (CLI)

```bash
# Verificar FFmpeg, Whisper, aceleración hardware y claves .env
.venv\Scripts\python.exe cli.py doctor

# Listar perfiles de podcaster
.venv\Scripts\python.exe cli.py profiles list

# Ejecutar un podcast en modo automático
.venv\Scripts\python.exe cli.py run --profile ejemplo_podcast --input ./input/mi_episodio.mp3 \
  --shorts-cliffhanger 3 --shorts-conclusive 3 --medium 1 --long 1 --auto

# Modo Revisión (pausa antes del renderizado para editar en la web)
.venv\Scripts\python.exe cli.py run --profile ejemplo_podcast --input ./input/mi_episodio.mp3 --review

# Reanudar un trabajo interrumpido o aprobar uno en revisión
.venv\Scripts\python.exe cli.py resume --job <job_id>

# Vigilar la carpeta input/ automáticamente
.venv\Scripts\python.exe cli.py watch --profile ejemplo_podcast
```
