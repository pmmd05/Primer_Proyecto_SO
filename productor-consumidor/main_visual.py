#!/usr/bin/env python3
"""
=============================================================
  PRODUCTOR-CONSUMIDOR — Visualización Web en Tiempo Real
  Sistemas Operativos — Universidad Rafael Landívar
=============================================================
  Buffer único compartido + Condition variable + imágenes de estado
  Flask + SocketIO transmite eventos al navegador en tiempo real.

  Estructura de carpetas esperada:
      main_visual.py
      imagenes/
          espera.png
          bloqueado.png
          seccion_critica.png
          consumiendo.png
      prueba1.txt
      prueba2.txt

  Uso:
      python main_visual.py prueba1.txt
      python main_visual.py          (pide la ruta interactivamente)
=============================================================
"""

import threading
import time
import sys
import os
import webbrowser
from flask import Flask, render_template_string, send_from_directory
from flask_socketio import SocketIO

# ──────────────────────────────────────────────────────────────
#  CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────
BUFFER_SIZE    = 10    # Capacidad máxima del buffer compartido
PRODUCER_DELAY = 0.50  # Pausa entre producciones  (visual más lento)
CONSUMER_DELAY = 0.70  # Pausa al procesar número consumido
CRITICAL_HOLD  = 0.35  # Pausa visible dentro de la sección crítica

# Directorio del script (para localizar imagenes/ aunque el cwd sea distinto)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ──────────────────────────────────────────────────────────────
#  FLASK + SOCKETIO
# ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = 'so-url-2025'
socketio = SocketIO(
    app,
    async_mode='threading',
    cors_allowed_origins='*',
    logger=False,
    engineio_logger=False,
)

# ──────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────
def is_prime(n: int) -> bool:
    if n < 2:      return False
    if n == 2:     return True
    if n % 2 == 0: return False
    for i in range(3, int(n ** 0.5) + 1, 2):
        if n % i == 0: return False
    return True

def classify(n: int) -> str:
    """Prioridad: primo > par > impar."""
    if is_prime(n):  return 'prime'
    if n % 2 == 0:   return 'even'
    return 'odd'

# ──────────────────────────────────────────────────────────────
#  ESTADO COMPARTIDO DE LA SIMULACIÓN
# ──────────────────────────────────────────────────────────────
_numbers  : list = []
_filepath : str  = ''

# ── Buffer único y primitiva de sincronización ────────────────
_buffer   : list = []
condition = threading.Condition()   # lock + variable de condición
_prod_done : bool = False

# ── Estado de actores (para UI) ───────────────────────────────
#   Estados posibles: 'idle' | 'waiting' | 'producing' | 'consuming'
#                     'blocked' | 'critical' | 'done'
_states : dict = {}
_states_lock = threading.Lock()

# ── Actor en sección crítica (None = ninguno) ─────────────────
_critical_actor : str = None
_critical_lock  = threading.Lock()

# ── Estadísticas de consumidores ─────────────────────────────
_sums   : dict = {'even': 0, 'odd': 0, 'prime': 0}
_counts : dict = {'even': 0, 'odd': 0, 'prime': 0}
_stats_lock = threading.Lock()

# ── Control de ejecución ──────────────────────────────────────
_sim_running : bool = False
_sim_lock = threading.Lock()

# ──────────────────────────────────────────────────────────────
#  GESTIÓN DE ESTADO
# ──────────────────────────────────────────────────────────────
def reset():
    """Reinicia todo el estado para permitir nueva ejecución."""
    global _buffer, _prod_done, _states, _critical_actor, _sums, _counts

    _buffer     = []
    _prod_done  = False
    _critical_actor = None

    _states = {
        'producer':       'idle',
        'consumer_even':  'idle',
        'consumer_odd':   'idle',
        'consumer_prime': 'idle',
    }
    _sums   = {'even': 0, 'odd': 0, 'prime': 0}
    _counts = {'even': 0, 'odd': 0, 'prime': 0}


def set_state(actor: str, state: str):
    with _states_lock:
        _states[actor] = state


def set_critical(actor):
    """actor = string con la clave del actor, o None para liberar."""
    with _critical_lock:
        global _critical_actor
        _critical_actor = actor


def snapshot() -> dict:
    """Captura atómica del estado completo para enviar al cliente."""
    with _critical_lock:
        ca = _critical_actor
    with _states_lock:
        st = dict(_states)
    with _stats_lock:
        su = dict(_sums)
        co = dict(_counts)
    return {
        'buffer':   list(_buffer),   # buffer puede leerse sin lock aquí (snapshot aproximado)
        'sums':     su,
        'counts':   co,
        'states':   st,
        'critical': ca,
    }


def emit_event(event: str, extra: dict = None):
    data = snapshot()
    if extra:
        data.update(extra)
    socketio.emit(event, data)


def log(msg: str, kind: str = 'system'):
    ts = time.strftime('%H:%M:%S')
    socketio.emit('log', {'msg': msg, 'kind': kind, 'ts': ts})


# ──────────────────────────────────────────────────────────────
#  PRODUCTOR
# ──────────────────────────────────────────────────────────────
def producer():
    """
    Inserta números al FINAL del buffer (buffer.append).
    Usa condition.wait() cuando el buffer está lleno.

    Sección crítica = bloque 'with condition'.
    Solo un thread puede estar dentro del bloque con el lock a la vez.
    """
    global _prod_done

    set_state('producer', 'producing')
    log(f"PRODUCTOR iniciando — {len(_numbers)} números en cola", 'producer')
    emit_event('state_update')

    for i, num in enumerate(_numbers):
        kind  = classify(num)
        label = {'even': 'PAR', 'odd': 'IMPAR', 'prime': 'PRIMO'}[kind]
        time.sleep(PRODUCER_DELAY)

        # ── Detectar bloqueo antes de adquirir el lock ────────────
        # (non-blocking peek para actualizar UI; el wait real ocurre dentro)
        with condition:                                  # ── ADQUIERE LOCK ──
            # Bucle de espera si buffer lleno
            if len(_buffer) >= BUFFER_SIZE:
                set_state('producer', 'blocked')
                log(f"PRODUCTOR bloqueado — buffer lleno "
                    f"({len(_buffer)}/{BUFFER_SIZE})", 'blocked')
                emit_event('state_update')
                while len(_buffer) >= BUFFER_SIZE:
                    condition.wait()                     # libera lock, espera notify

            # ── SECCIÓN CRÍTICA: insertar en buffer ───────────────
            set_state('producer', 'critical')
            set_critical('producer')
            emit_event('critical_enter',
                       {'actor': 'producer', 'num': num, 'kind': kind})

            time.sleep(CRITICAL_HOLD)                   # pausa visual en SC
            _buffer.append(num)                         # insertar al final
            buf_size = len(_buffer)
            buf_snap = list(_buffer)

            set_critical(None)
            condition.notify_all()                      # despertar consumidores
        # ── LIBERA LOCK ───────────────────────────────────────────

        set_state('producer', 'producing')
        log(f"PRODUCTOR insertó {num} ({label}) → buffer [{buf_size}/{BUFFER_SIZE}]",
            'producer')
        emit_event('insert', {
            'num':    num,
            'kind':   kind,
            'idx':    i + 1,
            'total':  len(_numbers),
            'buffer': buf_snap,
        })

    # Señal de fin
    with condition:
        _prod_done = True
        condition.notify_all()

    set_state('producer', 'done')
    log("PRODUCTOR terminado — señal de fin enviada", 'producer')
    emit_event('state_update')


# ──────────────────────────────────────────────────────────────
#  CONSUMIDOR (genérico, parametrizado por tipo)
# ──────────────────────────────────────────────────────────────
def consumer(kind: str):
    """
    FIFO estricto: solo puede tomar buffer[0] si classify(buffer[0]) == kind.
    Espera con condition.wait() si el buffer está vacío o el frente no es su tipo.

    Terminación:
      Cuando _prod_done=True y no quedan elementos de su tipo en el buffer.
      (Garantía: si hay elementos de su tipo, otro hilo los irá descubriendo
       en orden FIFO — no habrá deadlock porque los otros tipos se consumen.)
    """
    actor_key = f'consumer_{kind}'
    label_map = {'even': 'PARES', 'odd': 'IMPARES', 'prime': 'PRIMOS'}
    name      = f"CONSUMIDOR {label_map[kind]}"

    set_state(actor_key, 'waiting')
    log(f"{name} en espera de números...", kind)
    emit_event('state_update')

    while True:
        with condition:                                  # ── ADQUIERE LOCK ──
            # Bucle de espera
            while True:
                # ¿Frente del buffer es mi tipo? → puedo tomar
                if _buffer and classify(_buffer[0]) == kind:
                    break

                # ¿Terminó el productor y ya no hay elementos de mi tipo?
                if _prod_done and not any(classify(n) == kind for n in _buffer):
                    set_state(actor_key, 'done')
                    log(f"{name} recibió señal de fin", kind)
                    emit_event('state_update')
                    with _stats_lock:
                        s, c = _sums[kind], _counts[kind]
                    log(f"{name} FIN — {c} números procesados | suma total = {s}", kind)
                    return

                # Seguir esperando
                if _states.get(actor_key) != 'waiting':
                    set_state(actor_key, 'waiting')
                    emit_event('state_update')
                condition.wait()                         # libera lock, espera notify

            # ── SECCIÓN CRÍTICA: extraer del frente (FIFO) ────────
            set_state(actor_key, 'critical')
            set_critical(actor_key)
            emit_event('critical_enter', {'actor': actor_key, 'kind': kind})

            time.sleep(CRITICAL_HOLD)                   # pausa visual en SC
            num      = _buffer.pop(0)                   # extraer frente (FIFO)
            buf_size = len(_buffer)
            buf_snap = list(_buffer)

            set_critical(None)
            condition.notify_all()                      # despertar productor
        # ── LIBERA LOCK ───────────────────────────────────────────

        # Procesar número fuera de la sección crítica
        set_state(actor_key, 'consuming')
        time.sleep(CONSUMER_DELAY)

        with _stats_lock:
            _sums[kind]   += num
            _counts[kind] += 1
            s = _sums[kind]
            c = _counts[kind]

        log(f"{name} consumió {num} | suma acumulada = {s}", kind)
        emit_event('take', {
            'consumer': kind,
            'num':      num,
            'sum':      s,
            'count':    c,
            'buffer':   buf_snap,
        })

        set_state(actor_key, 'waiting')


# ──────────────────────────────────────────────────────────────
#  RUNNER DE SIMULACIÓN
# ──────────────────────────────────────────────────────────────
def run_simulation():
    global _sim_running

    reset()
    socketio.emit('simulation_start', snapshot())
    log("════ SIMULACIÓN INICIADA ════", 'system')

    threads = [
        threading.Thread(target=consumer, args=('even',),  name='C-Pares',   daemon=True),
        threading.Thread(target=consumer, args=('odd',),   name='C-Impares', daemon=True),
        threading.Thread(target=consumer, args=('prime',), name='C-Primos',  daemon=True),
        threading.Thread(target=producer,                  name='Productor',  daemon=True),
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    with _sim_lock:
        _sim_running = False

    with _stats_lock:
        final_sums   = dict(_sums)
        final_counts = dict(_counts)

    log("════ SIMULACIÓN COMPLETADA ════", 'system')
    socketio.emit('simulation_end', {'sums': final_sums, 'counts': final_counts})


# ──────────────────────────────────────────────────────────────
#  FLASK — RUTAS
# ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    evens  = [n for n in _numbers if classify(n) == 'even']
    odds   = [n for n in _numbers if classify(n) == 'odd']
    primes = [n for n in _numbers if classify(n) == 'prime']
    return render_template_string(
        HTML,
        filepath    = _filepath,
        total       = len(_numbers),
        n_even      = len(evens),
        n_odd       = len(odds),
        n_prime     = len(primes),
        buffer_size = BUFFER_SIZE,
    )

@app.route('/imagenes/<path:filename>')
def serve_image(filename):
    """Sirve las imágenes desde la carpeta imagenes/ junto al script."""
    img_dir = os.path.join(SCRIPT_DIR, 'imagenes')
    return send_from_directory(img_dir, filename)


# ──────────────────────────────────────────────────────────────
#  SOCKETIO — EVENTOS
# ──────────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    socketio.emit('ready', {'filepath': _filepath, 'total': len(_numbers), **snapshot()})

@socketio.on('start')
def on_start():
    global _sim_running
    with _sim_lock:
        if _sim_running:
            return
        _sim_running = True
    threading.Thread(target=run_simulation, daemon=True).start()


# ──────────────────────────────────────────────────────────────
#  HTML + CSS + JS
# ──────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Productor-Consumidor — SO / URL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=JetBrains+Mono:wght@300;400;600&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
/* ── Variables ─────────────────────────────────────────────── */
:root {
  --bg:     #080810;
  --bg1:    #0e0e1e;
  --bg2:    #13132a;
  --bg3:    #191935;
  --border: #1e1e42;
  --bord2:  #2a2a5a;
  --pro:    #00ff99;
  --even:   #00d4ff;
  --odd:    #ffb300;
  --prime:  #ff3d78;
  --txt:    #c0c0e8;
  --dim:    #40406a;
  --red:    #ff4455;
  --white:  #e8e8ff;
  --fh: 'Orbitron', monospace;
  --fm: 'JetBrains Mono', monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--fm);font-size:12px;min-height:100vh;overflow-x:hidden}

/* dot grid bg */
body::before{content:'';position:fixed;inset:0;
  background-image:radial-gradient(rgba(0,180,255,.07) 1px,transparent 1px);
  background-size:28px 28px;pointer-events:none;z-index:0}

/* ── Header ────────────────────────────────────────────────── */
header{
  position:sticky;top:0;z-index:200;
  background:rgba(8,8,16,.95);backdrop-filter:blur(16px);
  border-bottom:1px solid var(--bord2);
  padding:8px 18px;
  display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
}
.logo{font-family:var(--fh);font-size:9px;font-weight:900;letter-spacing:3px;white-space:nowrap}
.logo b{color:var(--pro)}
.hdr-r{display:flex;align-items:center;gap:8px;flex-wrap:wrap}

.pill{padding:3px 8px;border-radius:20px;font-size:8px;letter-spacing:1px;
  border:1px solid;font-family:var(--fh);font-weight:700}
.p-fp   {color:var(--dim);border-color:var(--border)}
.p-even {color:var(--even);border-color:var(--even);background:rgba(0,212,255,.06)}
.p-odd  {color:var(--odd);border-color:var(--odd);background:rgba(255,179,0,.06)}
.p-prime{color:var(--prime);border-color:var(--prime);background:rgba(255,61,120,.06)}

.btn-go{font-family:var(--fh);font-size:8px;letter-spacing:3px;font-weight:700;
  padding:8px 18px;border:1px solid var(--pro);background:rgba(0,255,153,.06);
  color:var(--pro);border-radius:6px;cursor:pointer;transition:all .2s}
.btn-go:hover:not(:disabled){background:rgba(0,255,153,.18);box-shadow:0 0 18px rgba(0,255,153,.2)}
.btn-go:disabled{opacity:.35;cursor:default}

/* ── Page ──────────────────────────────────────────────────── */
.page{position:relative;z-index:1}

/* ── Main layout: 3 columns ────────────────────────────────── */
.main-grid{
  display:grid;
  grid-template-columns:220px 1fr 220px;
  gap:12px;padding:12px 16px;
  max-width:1360px;margin:0 auto;
}
@media(max-width:860px){.main-grid{grid-template-columns:1fr}}

/* ── Card ──────────────────────────────────────────────────── */
.card{background:var(--bg1);border:1px solid var(--border);border-radius:10px;
  overflow:hidden;transition:border-color .25s,box-shadow .25s}
.card-hdr{padding:7px 12px;background:var(--bg2);border-bottom:1px solid var(--border);
  font-family:var(--fh);font-size:7px;letter-spacing:2px;font-weight:700;
  display:flex;align-items:center;justify-content:space-between}
.card-body{padding:12px}

/* card glow states */
.card.g-pro   {border-color:var(--pro)  !important;box-shadow:0 0 20px rgba(0,255,153,.12)!important}
.card.g-even  {border-color:var(--even) !important;box-shadow:0 0 20px rgba(0,212,255,.12)!important}
.card.g-odd   {border-color:var(--odd)  !important;box-shadow:0 0 20px rgba(255,179,0,.12)!important}
.card.g-prime {border-color:var(--prime)!important;box-shadow:0 0 20px rgba(255,61,120,.12)!important}
.card.g-crit  {border-color:var(--white)!important;box-shadow:0 0 24px rgba(255,255,255,.14)!important}
.card.g-block {border-color:var(--red)  !important;box-shadow:0 0 18px rgba(255,68,85,.16)!important}

/* ── STATE IMAGE ────────────────────────────────────────────── */
.actor-img-wrap{
  display:flex;flex-direction:column;align-items:center;gap:6px;
  padding:8px 0 6px;
}
.state-img{
  width:90px;height:90px;object-fit:contain;
  border-radius:8px;
  transition:filter .3s,opacity .3s,transform .3s;
  filter:drop-shadow(0 0 0 transparent);
}
/* glow tints por estado */
.state-img.s-producing{filter:drop-shadow(0 0 8px var(--pro))}
.state-img.s-consuming{filter:drop-shadow(0 0 8px var(--even))}  /* overridden per consumer */
.state-img.s-blocked  {filter:drop-shadow(0 0 10px var(--red)) brightness(.9)}
.state-img.s-critical {filter:drop-shadow(0 0 12px #fff) brightness(1.1);transform:scale(1.04)}
.state-img.s-done     {opacity:.35;filter:grayscale(1)}
.state-img.s-waiting  {opacity:.6}
.state-img.s-idle     {opacity:.4}

.state-lbl{
  font-family:var(--fh);font-size:7px;letter-spacing:2px;
  text-align:center;height:14px;transition:color .2s;
}
.state-num{
  font-family:var(--fh);font-size:22px;font-weight:900;
  text-align:center;min-height:36px;display:flex;align-items:center;justify-content:center;
  transition:color .2s;
}
@keyframes numPop{0%{transform:scale(1.5);opacity:.3}100%{transform:scale(1);opacity:1}}
.pop{animation:numPop .3s cubic-bezier(.34,1.56,.64,1)}

/* ── Progress bar (producer) ────────────────────────────────── */
.prog-wrap{margin-top:8px}
.prog-rail{height:2px;background:var(--border);border-radius:2px;overflow:hidden}
.prog-bar{height:100%;background:var(--pro);border-radius:2px;width:0%;transition:width .4s ease}
.prog-lbl{text-align:center;font-size:9px;color:var(--dim);margin-top:4px;letter-spacing:1px}

/* ── Distrib rows ───────────────────────────────────────────── */
.dist{display:flex;flex-direction:column;gap:3px;margin-top:8px}
.dist-row{display:flex;justify-content:space-between;align-items:center;
  padding:3px 7px;background:var(--bg2);border-radius:4px;font-size:10px}

/* ── Sum box ────────────────────────────────────────────────── */
.sum-box{margin-top:8px;background:var(--bg2);border:1px solid var(--border);
  border-radius:7px;padding:8px;text-align:center}
.sum-val{font-family:var(--fh);font-size:20px;font-weight:700;transition:color .2s}
.sum-sub{font-size:7px;color:var(--dim);letter-spacing:1.5px;margin-top:2px}

/* ── BUFFER CENTER ──────────────────────────────────────────── */
.buf-panel{display:flex;flex-direction:column;gap:10px}

.buf-arrows{
  display:flex;justify-content:space-between;align-items:center;
  padding:0 4px;font-size:8px;color:var(--dim);letter-spacing:1px;
  font-family:var(--fh)
}
.arr-in {color:var(--pro)}
.arr-out{color:var(--even)}

.buf-slots{
  display:grid;
  grid-template-columns:repeat(10,1fr);
  gap:4px;padding:10px 8px;
  background:var(--bg2);border:1px solid var(--border);border-radius:8px;
}

.slot{
  aspect-ratio:1;border-radius:6px;
  border:1px solid var(--border);background:var(--bg3);
  display:flex;align-items:center;justify-content:center;
  font-family:var(--fh);font-size:9px;font-weight:700;
  color:transparent;transition:all .2s;position:relative;
}
/* front marker (first occupied slot) */
.slot.front::after{
  content:'▲';position:absolute;bottom:-13px;left:50%;transform:translateX(-50%);
  font-size:7px;color:var(--white);opacity:.7;
}
.slot.occ-even  {background:rgba(0,212,255,.15);border-color:var(--even);color:var(--even)}
.slot.occ-odd   {background:rgba(255,179,0,.15);border-color:var(--odd);color:var(--odd)}
.slot.occ-prime {background:rgba(255,61,120,.15);border-color:var(--prime);color:var(--prime)}
.slot.occ-critical{border-color:#fff!important;box-shadow:0 0 10px rgba(255,255,255,.3)}
@keyframes slotIn{from{transform:scale(.3) rotate(-10deg);opacity:0}to{transform:scale(1) rotate(0);opacity:1}}
.slot.anim-in{animation:slotIn .28s cubic-bezier(.34,1.56,.64,1)}

.buf-stats{
  display:flex;align-items:center;justify-content:space-between;
  padding:6px 10px;background:var(--bg2);border:1px solid var(--border);
  border-radius:7px;font-family:var(--fh);font-size:8px;letter-spacing:1px;
}
.buf-level-wrap{flex:1;margin:0 12px}
.buf-level-rail{height:3px;background:var(--border);border-radius:2px;overflow:hidden}
.buf-level-bar{height:100%;background:var(--pro);border-radius:2px;transition:width .3s ease}

/* front indicator below buffer */
.front-lbl{
  text-align:left;font-size:8px;color:var(--dim);
  font-family:var(--fh);letter-spacing:1px;padding-left:6px;margin-top:14px;
}
.front-val{display:inline;color:var(--white);font-size:10px;font-weight:700}

/* ── LOG ────────────────────────────────────────────────────── */
.log-sec{padding:0 16px 16px;max-width:1360px;margin:0 auto}
.log-wrap{background:var(--bg1);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.log-hdr{padding:6px 12px;background:var(--bg2);border-bottom:1px solid var(--border);
  font-family:var(--fh);font-size:7px;letter-spacing:2px;
  display:flex;align-items:center;justify-content:space-between}
.log-clr{font-size:7px;background:none;border:1px solid var(--border);color:var(--dim);
  border-radius:3px;padding:2px 7px;cursor:pointer;font-family:var(--fh);letter-spacing:1px}
.log-clr:hover{border-color:var(--bord2);color:var(--txt)}
.log-body{height:160px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--bord2) transparent}
.log-row{display:flex;gap:8px;padding:4px 12px;border-bottom:1px solid var(--border);
  font-size:10px;animation:logIn .12s ease}
@keyframes logIn{from{opacity:0;transform:translateY(-2px)}to{opacity:1;transform:none}}
.log-ts{color:var(--dim);min-width:58px;font-size:9px;flex-shrink:0}
.c-producer{color:var(--pro)}
.c-even    {color:var(--even)}
.c-odd     {color:var(--odd)}
.c-prime   {color:var(--prime)}
.c-blocked {color:var(--red)}
.c-system  {color:var(--dim);font-style:italic}

/* ── FINAL OVERLAY ──────────────────────────────────────────── */
.overlay{display:none;position:fixed;inset:0;
  background:rgba(8,8,16,.92);backdrop-filter:blur(12px);
  z-index:500;align-items:center;justify-content:center}
.overlay.show{display:flex}
.final-card{background:var(--bg1);border:1px solid var(--bord2);border-radius:14px;
  padding:32px;max-width:420px;width:92%;text-align:center;
  animation:cardIn .45s cubic-bezier(.34,1.56,.64,1)}
@keyframes cardIn{from{transform:scale(.85);opacity:0}to{transform:scale(1);opacity:1}}
.fin-title{font-family:var(--fh);font-size:10px;letter-spacing:4px;color:var(--pro);margin-bottom:20px}
.fin-row{display:flex;align-items:center;justify-content:space-between;
  background:var(--bg2);border-radius:7px;padding:10px 14px;margin-bottom:8px;
  font-family:var(--fh);font-size:8px;letter-spacing:2px}
.fin-sum{font-family:var(--fh);font-size:18px;font-weight:700}
.fin-cnt{font-size:9px;color:var(--dim);margin-right:8px}
.btn-rst{font-family:var(--fh);font-size:7px;letter-spacing:2px;
  padding:9px 20px;border:1px solid var(--bord2);background:var(--bg2);
  color:var(--txt);border-radius:6px;cursor:pointer;transition:all .2s;margin-top:12px}
.btn-rst:hover{border-color:var(--pro);color:var(--pro)}
</style>
</head>
<body>
<div class="page">

<!-- cfg (invisible, leído por JS) -->
<div id="cfg"
  data-buf="{{ buffer_size }}"
  data-total="{{ total }}"
  data-n-even="{{ n_even }}"
  data-n-odd="{{ n_odd }}"
  data-n-prime="{{ n_prime }}"
  style="display:none"></div>

<!-- ── HEADER ──────────────────────────────────────────────── -->
<header>
  <div class="logo">PRODUCTOR<b>-CONSUMIDOR</b>&nbsp;/&nbsp;SO — URL</div>
  <div class="hdr-r">
    <span class="pill p-fp">{{ filepath }}</span>
    <span class="pill p-even">PARES {{ n_even }}</span>
    <span class="pill p-odd">IMPARES {{ n_odd }}</span>
    <span class="pill p-prime">PRIMOS {{ n_prime }}</span>
    <button class="btn-go" id="btn-go" onclick="startSim()">▶ INICIAR</button>
  </div>
</header>

<!-- ── MAIN GRID ────────────────────────────────────────────── -->
<div class="main-grid">

<!-- ── LEFT: PRODUCTOR ──────────────────────────────────────── -->
<div>
  <div class="card" id="card-producer">
    <div class="card-hdr">
      <span style="color:var(--pro)">◈ PRODUCTOR</span>
    </div>
    <div class="card-body">
      <div class="actor-img-wrap">
        <img id="img-producer" class="state-img s-idle"
             src="/imagenes/espera.png" alt="estado productor">
        <div class="state-lbl" id="slbl-producer" style="color:var(--dim)">IDLE</div>
      </div>
      <div class="state-num" id="snum-producer" style="color:var(--pro)">—</div>
      <div style="text-align:center;font-size:8px;color:var(--dim);height:14px"
           id="stag-producer"></div>
      <div class="prog-wrap">
        <div class="prog-rail"><div class="prog-bar" id="prog-bar"></div></div>
        <div class="prog-lbl" id="prog-lbl">0 / {{ total }}</div>
      </div>
      <div class="dist">
        <div class="dist-row">
          <span style="color:var(--even)">PARES</span>
          <span style="color:var(--even)">{{ n_even }}</span>
        </div>
        <div class="dist-row">
          <span style="color:var(--odd)">IMPARES</span>
          <span style="color:var(--odd)">{{ n_odd }}</span>
        </div>
        <div class="dist-row">
          <span style="color:var(--prime)">PRIMOS</span>
          <span style="color:var(--prime)">{{ n_prime }}</span>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ── CENTER: BUFFER ÚNICO ─────────────────────────────────── -->
<div>
  <div class="card" id="card-buffer">
    <div class="card-hdr">
      <span style="color:var(--white)">▣ BUFFER COMPARTIDO</span>
      <span id="buf-cnt" style="color:var(--dim);font-size:8px">0 / {{ buffer_size }}</span>
    </div>
    <div class="card-body">

      <div class="buf-arrows">
        <span class="arr-in">PRODUCTOR → INSERTA AL FINAL</span>
        <span class="arr-out">CONSUMIDORES TOMAN EL FRENTE →</span>
      </div>

      <div class="buf-slots" id="buf-slots"></div>

      <div class="front-lbl" id="front-lbl" style="visibility:hidden">
        FRENTE: <span class="front-val" id="front-val">—</span>
        <span id="front-kind" style="font-size:8px;margin-left:6px"></span>
      </div>

      <div class="buf-stats" style="margin-top:16px">
        <span style="font-size:8px;letter-spacing:1px;color:var(--dim)">OCUPACIÓN</span>
        <div class="buf-level-wrap">
          <div class="buf-level-rail">
            <div class="buf-level-bar" id="buf-level"></div>
          </div>
        </div>
        <span id="buf-pct" style="color:var(--pro);font-size:9px">0%</span>
      </div>

      <!-- leyenda de colores -->
      <div style="display:flex;gap:10px;margin-top:10px;justify-content:center">
        <span style="font-size:8px;color:var(--even);font-family:var(--fh);letter-spacing:1px">■ PAR</span>
        <span style="font-size:8px;color:var(--odd);font-family:var(--fh);letter-spacing:1px">■ IMPAR</span>
        <span style="font-size:8px;color:var(--prime);font-family:var(--fh);letter-spacing:1px">■ PRIMO</span>
      </div>
    </div>
  </div>
</div>

<!-- ── RIGHT: CONSUMIDORES ──────────────────────────────────── -->
<div style="display:flex;flex-direction:column;gap:10px">

  <!-- Consumer PARES -->
  <div class="card" id="card-consumer-even">
    <div class="card-hdr">
      <span style="color:var(--even)">◈ CONSUMIDOR PARES</span>
    </div>
    <div class="card-body">
      <div class="actor-img-wrap" style="flex-direction:row;justify-content:flex-start;gap:10px;padding:4px 0">
        <img id="img-consumer-even" class="state-img s-idle"
             src="/imagenes/espera.png" alt="estado pares"
             style="width:64px;height:64px">
        <div>
          <div class="state-lbl" id="slbl-consumer-even"
               style="color:var(--dim);text-align:left">IDLE</div>
          <div class="state-num" id="snum-consumer-even"
               style="color:var(--even);font-size:18px;min-height:28px;justify-content:flex-start">—</div>
        </div>
      </div>
      <div class="sum-box">
        <div class="sum-sub">SUMA ACUMULADA</div>
        <div class="sum-val" id="sum-even" style="color:var(--even)">0</div>
        <div class="sum-sub" id="cnt-even" style="margin-top:2px">0 números</div>
      </div>
    </div>
  </div>

  <!-- Consumer IMPARES -->
  <div class="card" id="card-consumer-odd">
    <div class="card-hdr">
      <span style="color:var(--odd)">◈ CONSUMIDOR IMPARES</span>
    </div>
    <div class="card-body">
      <div class="actor-img-wrap" style="flex-direction:row;justify-content:flex-start;gap:10px;padding:4px 0">
        <img id="img-consumer-odd" class="state-img s-idle"
             src="/imagenes/espera.png" alt="estado impares"
             style="width:64px;height:64px">
        <div>
          <div class="state-lbl" id="slbl-consumer-odd"
               style="color:var(--dim);text-align:left">IDLE</div>
          <div class="state-num" id="snum-consumer-odd"
               style="color:var(--odd);font-size:18px;min-height:28px;justify-content:flex-start">—</div>
        </div>
      </div>
      <div class="sum-box">
        <div class="sum-sub">SUMA ACUMULADA</div>
        <div class="sum-val" id="sum-odd" style="color:var(--odd)">0</div>
        <div class="sum-sub" id="cnt-odd" style="margin-top:2px">0 números</div>
      </div>
    </div>
  </div>

  <!-- Consumer PRIMOS -->
  <div class="card" id="card-consumer-prime">
    <div class="card-hdr">
      <span style="color:var(--prime)">◈ CONSUMIDOR PRIMOS</span>
    </div>
    <div class="card-body">
      <div class="actor-img-wrap" style="flex-direction:row;justify-content:flex-start;gap:10px;padding:4px 0">
        <img id="img-consumer-prime" class="state-img s-idle"
             src="/imagenes/espera.png" alt="estado primos"
             style="width:64px;height:64px">
        <div>
          <div class="state-lbl" id="slbl-consumer-prime"
               style="color:var(--dim);text-align:left">IDLE</div>
          <div class="state-num" id="snum-consumer-prime"
               style="color:var(--prime);font-size:18px;min-height:28px;justify-content:flex-start">—</div>
        </div>
      </div>
      <div class="sum-box">
        <div class="sum-sub">SUMA ACUMULADA</div>
        <div class="sum-val" id="sum-prime" style="color:var(--prime)">0</div>
        <div class="sum-sub" id="cnt-prime" style="margin-top:2px">0 números</div>
      </div>
    </div>
  </div>

</div>
</div><!-- /main-grid -->

<!-- ── LOG ──────────────────────────────────────────────────── -->
<div class="log-sec">
  <div class="log-wrap">
    <div class="log-hdr">
      <span>◉ REGISTRO DE EVENTOS</span>
      <button class="log-clr" onclick="document.getElementById('log-body').innerHTML=''">LIMPIAR</button>
    </div>
    <div class="log-body" id="log-body"></div>
  </div>
</div>

<!-- ── OVERLAY FINAL ────────────────────────────────────────── -->
<div class="overlay" id="overlay">
  <div class="final-card">
    <div class="fin-title">✓ SIMULACIÓN COMPLETADA</div>
    <div class="fin-row">
      <span style="color:var(--even)">PARES</span>
      <span>
        <span class="fin-cnt" id="f-cnt-even">0 núms</span>
        <span class="fin-sum" id="f-sum-even" style="color:var(--even)">0</span>
      </span>
    </div>
    <div class="fin-row">
      <span style="color:var(--odd)">IMPARES</span>
      <span>
        <span class="fin-cnt" id="f-cnt-odd">0 núms</span>
        <span class="fin-sum" id="f-sum-odd" style="color:var(--odd)">0</span>
      </span>
    </div>
    <div class="fin-row">
      <span style="color:var(--prime)">PRIMOS</span>
      <span>
        <span class="fin-cnt" id="f-cnt-prime">0 núms</span>
        <span class="fin-sum" id="f-sum-prime" style="color:var(--prime)">0</span>
      </span>
    </div>
    <button class="btn-rst" onclick="restartUI()">↺ NUEVA EJECUCIÓN</button>
  </div>
</div>

</div><!-- /page -->

<!-- ── JAVASCRIPT ───────────────────────────────────────────── -->
<script>
const D        = document.getElementById('cfg').dataset;
const BUF_SIZE = parseInt(D.buf);
const TOTAL    = parseInt(D.total);

// ── Helpers ──────────────────────────────────────────────────
function isPrime(n) {
  if (n < 2) return false;
  if (n === 2) return true;
  if (n % 2 === 0) return false;
  for (let i = 3; i <= Math.sqrt(n); i += 2) if (n % i === 0) return false;
  return true;
}
function classify(n) {
  if (isPrime(n))   return 'prime';
  if (n % 2 === 0)  return 'even';
  return 'odd';
}

// ── State → image + label + color config ─────────────────────
const IMG = {
  idle:      '/imagenes/espera.png',
  waiting:   '/imagenes/espera.png',
  producing: '/imagenes/consumiendo.png',
  consuming: '/imagenes/consumiendo.png',
  blocked:   '/imagenes/bloqueado.png',
  critical:  '/imagenes/seccion_critica.png',
  done:      '/imagenes/espera.png',
};
const LBL = {
  idle:      'IDLE',
  waiting:   'EN ESPERA',
  producing: 'PRODUCIENDO',
  consuming: 'CONSUMIENDO',
  blocked:   'BLOQUEADO ⚠',
  critical:  'SECC. CRÍTICA',
  done:      'TERMINADO ✓',
};
const KIND_COLOR = {even:'var(--even)', odd:'var(--odd)', prime:'var(--prime)'};

// ── Actor config ─────────────────────────────────────────────
const ACTORS = {
  producer:       { img:'img-producer',       lbl:'slbl-producer',       num:'snum-producer',       card:'card-producer',       kind:null    },
  consumer_even:  { img:'img-consumer-even',  lbl:'slbl-consumer-even',  num:'snum-consumer-even',  card:'card-consumer-even',  kind:'even'  },
  consumer_odd:   { img:'img-consumer-odd',   lbl:'slbl-consumer-odd',   num:'snum-consumer-odd',   card:'card-consumer-odd',   kind:'odd'   },
  consumer_prime: { img:'img-consumer-prime', lbl:'slbl-consumer-prime', num:'snum-consumer-prime', card:'card-consumer-prime', kind:'prime' },
};

let prodIdx = 0;

// ── Update actor state ────────────────────────────────────────
function setActor(key, state) {
  const a = ACTORS[key]; if (!a) return;
  const imgEl  = document.getElementById(a.img);
  const lblEl  = document.getElementById(a.lbl);
  const cardEl = document.getElementById(a.card);

  // Image
  imgEl.src       = IMG[state] || IMG.idle;
  imgEl.className = 'state-img s-' + state;
  // Consumer consuming: tint with their color
  if (state === 'consuming' && a.kind) {
    imgEl.style.filter = `drop-shadow(0 0 8px ${KIND_COLOR[a.kind]})`;
  } else {
    imgEl.style.filter = '';
  }

  // Label
  lblEl.textContent = LBL[state] || state.toUpperCase();
  const col = state === 'blocked'  ? 'var(--red)'
            : state === 'critical' ? 'var(--white)'
            : a.kind ? KIND_COLOR[a.kind]
            : 'var(--pro)';
  lblEl.style.color = col;

  // Card glow
  if (cardEl) {
    cardEl.className = 'card';
    if (state === 'critical') cardEl.classList.add('g-crit');
    else if (state === 'blocked') cardEl.classList.add('g-block');
    else if (state === 'producing') cardEl.classList.add('g-pro');
    else if (state === 'consuming' && a.kind) cardEl.classList.add('g-' + a.kind);
  }
}

function applyStates(states) {
  if (!states) return;
  Object.entries(states).forEach(([k, v]) => setActor(k, v));
}

// ── Buffer render ─────────────────────────────────────────────
function renderBuffer(contents, criticalActor) {
  const wrap  = document.getElementById('buf-slots');
  const cnt   = document.getElementById('buf-cnt');
  const level = document.getElementById('buf-level');
  const pct   = document.getElementById('buf-pct');
  const fl    = document.getElementById('front-lbl');
  const fv    = document.getElementById('front-val');
  const fk    = document.getElementById('front-kind');
  const bufCard = document.getElementById('card-buffer');

  const n = contents.length;
  cnt.textContent = n + ' / ' + BUF_SIZE;
  const p = Math.round(n / BUF_SIZE * 100);
  level.style.width = p + '%';
  pct.textContent   = p + '%';
  // color bar by fullness
  level.style.background = p >= 90 ? 'var(--red)' : p >= 60 ? 'var(--odd)' : 'var(--pro)';

  // Card glow for buffer
  bufCard.className = 'card';
  if (criticalActor) {
    bufCard.classList.add('g-crit');
  }

  wrap.innerHTML = '';
  for (let i = 0; i < BUF_SIZE; i++) {
    const s = document.createElement('div');
    if (i < n) {
      const k = classify(contents[i]);
      s.className = 'slot occ-' + k + (i === 0 ? ' front' : '');
      if (criticalActor && i === 0) s.classList.add('occ-critical');
      s.textContent = contents[i];
    } else {
      s.className = 'slot';
    }
    wrap.appendChild(s);
  }

  // Front indicator
  if (n > 0) {
    const k = classify(contents[0]);
    fl.style.visibility = 'visible';
    fv.textContent = contents[0];
    fv.style.color = KIND_COLOR[k];
    fk.textContent = '(' + {even:'PAR', odd:'IMPAR', prime:'PRIMO'}[k] + ')';
    fk.style.color = KIND_COLOR[k];
  } else {
    fl.style.visibility = 'hidden';
  }
}

// ── Number flash ──────────────────────────────────────────────
function flashNum(elId, value, color) {
  const el = document.getElementById(elId);
  el.textContent = value;
  if (color) el.style.color = color;
  el.classList.remove('pop');
  void el.offsetWidth;
  el.classList.add('pop');
}

// ── Log ───────────────────────────────────────────────────────
function addLog(msg, kind, ts) {
  const body = document.getElementById('log-body');
  const row  = document.createElement('div');
  row.className = 'log-row';
  row.innerHTML = `<span class="log-ts">${ts}</span><span class="c-${kind}">${msg}</span>`;
  body.insertBefore(row, body.firstChild);
  if (body.children.length > 200) body.removeChild(body.lastChild);
}

// ── Socket.IO ─────────────────────────────────────────────────
const socket = io();

socket.on('connect', () =>
  addLog('Conectado al servidor', 'system', new Date().toLocaleTimeString())
);

socket.on('ready', data => {
  renderBuffer(data.buffer || [], null);
});

socket.on('simulation_start', data => {
  prodIdx = 0;
  renderBuffer([], null);
  applyStates(data.states);
  document.getElementById('btn-go').disabled    = true;
  document.getElementById('btn-go').textContent = '⟳ EJECUTANDO';
  document.getElementById('prog-bar').style.width = '0%';
  document.getElementById('prog-lbl').textContent = '0 / ' + TOTAL;
  document.getElementById('snum-producer').textContent = '—';
  document.getElementById('stag-producer').textContent = '';
  ['even','odd','prime'].forEach(k => {
    document.getElementById('sum-'+k).textContent = '0';
    document.getElementById('cnt-'+k).textContent = '0 números';
    document.getElementById('snum-consumer-'+k).textContent = '—';
  });
});

socket.on('insert', data => {
  prodIdx++;
  const pct = prodIdx / TOTAL * 100;
  document.getElementById('prog-bar').style.width = pct + '%';
  document.getElementById('prog-lbl').textContent = prodIdx + ' / ' + TOTAL;

  const col = KIND_COLOR[data.kind];
  flashNum('snum-producer', data.num, col);
  document.getElementById('stag-producer').textContent =
    {even:'PAR', odd:'IMPAR', prime:'PRIMO'}[data.kind];
  document.getElementById('stag-producer').style.color = col;

  renderBuffer(data.buffer, data.critical);
  applyStates(data.states);
});

socket.on('take', data => {
  const col = KIND_COLOR[data.consumer];
  flashNum('snum-consumer-' + data.consumer, data.num, col);
  document.getElementById('sum-' + data.consumer).textContent = data.sum;
  document.getElementById('cnt-' + data.consumer).textContent =
    data.count + ' número' + (data.count !== 1 ? 's' : '');

  renderBuffer(data.buffer, data.critical);
  applyStates(data.states);
});

socket.on('critical_enter', data => {
  renderBuffer(data.buffer, data.critical);
  applyStates(data.states);
});

socket.on('state_update', data => {
  renderBuffer(data.buffer || [], data.critical);
  applyStates(data.states);
  if (data.sums) ['even','odd','prime'].forEach(k =>
    document.getElementById('sum-'+k).textContent = data.sums[k]);
  if (data.counts) ['even','odd','prime'].forEach(k =>
    document.getElementById('cnt-'+k).textContent =
      data.counts[k] + ' número' + (data.counts[k] !== 1 ? 's' : ''));
});

socket.on('log', data => addLog(data.msg, data.kind, data.ts));

socket.on('simulation_end', data => {
  document.getElementById('btn-go').textContent = '✓ COMPLETADO';
  ['even','odd','prime'].forEach(k => {
    document.getElementById('f-sum-'+k).textContent = data.sums[k];
    document.getElementById('f-cnt-'+k).textContent = data.counts[k] + ' núms';
  });
  setTimeout(() => document.getElementById('overlay').classList.add('show'), 1400);
});

// ── Acciones ──────────────────────────────────────────────────
function startSim() { socket.emit('start'); }

function restartUI() {
  document.getElementById('overlay').classList.remove('show');
  const btn = document.getElementById('btn-go');
  btn.disabled = false; btn.textContent = '▶ INICIAR';
  prodIdx = 0;
  document.getElementById('log-body').innerHTML = '';
  document.getElementById('prog-bar').style.width = '0%';
  document.getElementById('prog-lbl').textContent = '0 / ' + TOTAL;
  document.getElementById('snum-producer').textContent = '—';
  document.getElementById('stag-producer').textContent = '';
  ['even','odd','prime'].forEach(k => {
    document.getElementById('sum-'+k).textContent = '0';
    document.getElementById('cnt-'+k).textContent = '0 números';
    document.getElementById('snum-consumer-'+k).textContent = '—';
    setActor('consumer_'+k, 'idle');
  });
  setActor('producer', 'idle');
  renderBuffer([], null);
}

// Init
renderBuffer([], null);
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
def main():
    global _numbers, _filepath

    if len(sys.argv) > 1:
        fp = sys.argv[1]
    else:
        fp = input("\nIngrese la ruta del archivo .txt: ").strip()

    if not os.path.exists(fp):
        print(f"\n[ERROR] No se encontró: '{fp}'")
        sys.exit(1)

    nums, skipped = [], []
    with open(fp, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line: continue
            try:    nums.append(int(line))
            except: skipped.append((i, line))

    if skipped:
        print(f"[AVISO] Se omitieron {len(skipped)} líneas no numéricas.")
    if not nums:
        print("[ERROR] No hay números válidos."); sys.exit(1)

    _numbers  = nums
    _filepath = os.path.basename(fp)

    evens  = [n for n in nums if classify(n) == 'even']
    odds   = [n for n in nums if classify(n) == 'odd']
    primes = [n for n in nums if classify(n) == 'prime']

    print(f"\n{'─'*54}")
    print(f"  Productor-Consumidor — SO — URL")
    print(f"{'─'*54}")
    print(f"  Archivo   : {_filepath}")
    print(f"  Total     : {len(nums)}")
    print(f"  Pares     : {len(evens)}  {evens}")
    print(f"  Impares   : {len(odds)}  {odds}")
    print(f"  Primos    : {len(primes)}  {primes}")
    print(f"  Buffer    : {BUFFER_SIZE}")
    print(f"{'─'*54}")
    print(f"  → http://localhost:5000")
    print(f"  (El navegador se abrirá automáticamente)\n")

    # Verificar que exista la carpeta imagenes
    img_dir = os.path.join(SCRIPT_DIR, 'imagenes')
    if not os.path.isdir(img_dir):
        print(f"[AVISO] No se encontró la carpeta 'imagenes/' en {SCRIPT_DIR}")
        print(f"        Las imágenes de estado no se mostrarán.\n")

    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()

    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=False,
        allow_unsafe_werkzeug=True,
    )


if __name__ == '__main__':
    main()