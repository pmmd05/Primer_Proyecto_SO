#!/usr/bin/env python3
"""
=============================================================
  PRODUCTOR-CONSUMIDOR — Visualización Web en Tiempo Real
  Sistemas Operativos — Universidad Rafael Landívar
=============================================================
  Servidor Flask + SocketIO que ejecuta los hilos y transmite
  eventos al navegador para su animación en tiempo real.

  Uso:
      python main_visual.py prueba1.txt
      python main_visual.py              (pedirá la ruta)
=============================================================
"""

import threading
import time
import sys
import os
import webbrowser
from flask import Flask, render_template_string
from flask_socketio import SocketIO

# ──────────────────────────────────────────────────────────────
#  CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────
BUFFER_SIZE    = 10    # Capacidad de cada buffer
PRODUCER_DELAY = 0.50  # Pausa entre producciones (segundos)
CONSUMER_DELAY = 0.80  # Pausa procesando cada número consumido
CRITICAL_HOLD  = 0.20  # Pausa visual dentro de la sección crítica

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
    if n < 2: return False
    if n == 2: return True
    if n % 2 == 0: return False
    for i in range(3, int(n**0.5) + 1, 2):
        if n % i == 0: return False
    return True

def classify(n: int) -> str:
    """Prima > par > impar (prioridad de clasificación)."""
    if is_prime(n):  return 'prime'
    if n % 2 == 0:   return 'even'
    return 'odd'

# ──────────────────────────────────────────────────────────────
#  ESTADO COMPARTIDO DE LA SIMULACIÓN
# ──────────────────────────────────────────────────────────────
_numbers  : list = []
_filepath : str  = ''

# Buffers compartidos (uno por tipo)
_buffers : dict = {'even': [], 'odd': [], 'prime': []}
_buf_locks       = {k: threading.Lock() for k in ('even','odd','prime')}

# Semáforos (se reinician en reset())
_empty_sems : dict = {}
_full_sems  : dict = {}

# Estadísticas de consumidores
_sums   : dict = {'even': 0, 'odd': 0, 'prime': 0}
_counts : dict = {'even': 0, 'odd': 0, 'prime': 0}
_stats_lock = threading.Lock()

# Estado de cada actor
_states : dict = {}
_states_lock = threading.Lock()

# Secciones críticas activas
_critical : dict = {'even': False, 'odd': False, 'prime': False}
_critical_lock = threading.Lock()

# Control de simulación
_sim_running : bool = False
_sim_lock = threading.Lock()

# ──────────────────────────────────────────────────────────────
#  GESTIÓN DE ESTADO
# ──────────────────────────────────────────────────────────────
def reset():
    """Reinicia todo el estado para una nueva ejecución."""
    global _sums, _counts, _states, _critical, _empty_sems, _full_sems
    for k in _buffers:
        _buffers[k].clear()
    _sums    = {'even': 0, 'odd': 0, 'prime': 0}
    _counts  = {'even': 0, 'odd': 0, 'prime': 0}
    _states  = {
        'producer':       'waiting',
        'consumer_even':  'waiting',
        'consumer_odd':   'waiting',
        'consumer_prime': 'waiting',
    }
    _critical   = {'even': False, 'odd': False, 'prime': False}
    _empty_sems = {k: threading.Semaphore(BUFFER_SIZE) for k in ('even','odd','prime')}
    _full_sems  = {k: threading.Semaphore(0)           for k in ('even','odd','prime')}


def snapshot() -> dict:
    """Captura completa del estado actual para enviar al cliente."""
    return {
        'buffers':  {k: list(v) for k, v in _buffers.items()},
        'sums':     dict(_sums),
        'counts':   dict(_counts),
        'states':   dict(_states),
        'critical': dict(_critical),
    }


def set_state(actor: str, state: str):
    with _states_lock:
        _states[actor] = state


def set_critical(kind: str, active: bool):
    with _critical_lock:
        _critical[kind] = active


def emit_event(event: str, extra: dict = None):
    """Emite un evento SocketIO con el estado completo adjunto."""
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
    Hilo productor: lee _numbers, clasifica cada número e inserta
    en el buffer correspondiente.

    Sincronización:
      1. acquire(empty_sem)  → esperar espacio libre
      2. acquire(buf_lock)   → SECCIÓN CRÍTICA: modificar buffer
      3. release(buf_lock)
      4. release(full_sem)   → señalizar elemento disponible
    """
    set_state('producer', 'producing')
    log(f"PRODUCTOR iniciando — {len(_numbers)} números en cola", 'producer')
    emit_event('state_update')

    for i, num in enumerate(_numbers):
        kind = classify(num)
        time.sleep(PRODUCER_DELAY)
        set_state('producer', 'producing')

        # ── Verificar si el buffer está lleno (sin bloquear) ──────────
        will_block = not _empty_sems[kind].acquire(blocking=False)
        if will_block:
            set_state('producer', 'blocked')
            log(f"PRODUCTOR bloqueado — buffer_{kind} lleno ({BUFFER_SIZE}/{BUFFER_SIZE})", 'blocked')
            emit_event('state_update')
            _empty_sems[kind].acquire()  # Esperar hasta que haya espacio

        # ── SECCIÓN CRÍTICA ───────────────────────────────────────────
        set_state('producer', 'critical')
        set_critical(kind, True)
        emit_event('critical_enter', {'actor': 'producer', 'kind': kind, 'num': num})

        with _buf_locks[kind]:               # acquire mutex
            _buffers[kind].append(num)
            size = len(_buffers[kind])
            time.sleep(CRITICAL_HOLD)        # pausa visual dentro del mutex

        set_critical(kind, False)            # release mutex (implícito)
        # ── FIN SECCIÓN CRÍTICA ───────────────────────────────────────

        set_state('producer', 'producing')
        _full_sems[kind].release()

        label = {'even': 'PAR', 'odd': 'IMPAR', 'prime': 'PRIMO'}[kind]
        log(f"PRODUCTOR insertó {num} ({label}) → buffer_{kind} [{size}/{BUFFER_SIZE}]", 'producer')
        emit_event('insert', {'num': num, 'kind': kind, 'idx': i + 1, 'total': len(_numbers)})

    # ── Señales centinela de fin (None) ───────────────────────────────
    for k in ('even', 'odd', 'prime'):
        _empty_sems[k].acquire()
        with _buf_locks[k]:
            _buffers[k].append(None)
        _full_sems[k].release()

    set_state('producer', 'done')
    log("PRODUCTOR terminado — señales de fin enviadas a los 3 consumidores", 'producer')
    emit_event('state_update')


# ──────────────────────────────────────────────────────────────
#  CONSUMIDOR (función genérica)
# ──────────────────────────────────────────────────────────────
def consumer(kind: str):
    """
    Hilo consumidor genérico parametrizado por tipo.

    Sincronización:
      1. acquire(full_sem)   → esperar elemento disponible
      2. acquire(buf_lock)   → SECCIÓN CRÍTICA: extraer del buffer
      3. release(buf_lock)
      4. release(empty_sem)  → señalizar espacio libre
    """
    set_state(f'consumer_{kind}', 'waiting')
    log(f"CONSUMIDOR {kind.upper()} en espera de números...", kind)
    emit_event('state_update')

    while True:
        # ── Verificar si el buffer está vacío (sin bloquear) ──────────
        will_block = not _full_sems[kind].acquire(blocking=False)
        if will_block:
            set_state(f'consumer_{kind}', 'blocked')
            emit_event('state_update')
            _full_sems[kind].acquire()  # Esperar hasta que haya elemento

        # ── SECCIÓN CRÍTICA ───────────────────────────────────────────
        set_state(f'consumer_{kind}', 'critical')
        set_critical(kind, True)
        emit_event('critical_enter', {'actor': f'consumer_{kind}', 'kind': kind})

        with _buf_locks[kind]:           # acquire mutex
            num = _buffers[kind].pop(0)
            time.sleep(CRITICAL_HOLD)    # pausa visual dentro del mutex

        set_critical(kind, False)        # release mutex (implícito)
        # ── FIN SECCIÓN CRÍTICA ───────────────────────────────────────

        _empty_sems[kind].release()

        # Centinela de fin
        if num is None:
            set_state(f'consumer_{kind}', 'done')
            log(f"CONSUMIDOR {kind.upper()} recibió señal de fin", kind)
            emit_event('state_update')
            break

        set_state(f'consumer_{kind}', 'consuming')
        time.sleep(CONSUMER_DELAY)

        with _stats_lock:
            _sums[kind]   += num
            _counts[kind] += 1
            s = _sums[kind]
            c = _counts[kind]

        log(f"CONSUMIDOR {kind.upper()} consumió {num} | suma acumulada = {s}", kind)
        emit_event('take', {'consumer': kind, 'num': num, 'sum': s, 'count': c})

    with _stats_lock:
        s, c = _sums[kind], _counts[kind]
    log(f"CONSUMIDOR {kind.upper()} FIN — {c} números procesados | suma total = {s}", kind)


# ──────────────────────────────────────────────────────────────
#  RUNNER DE SIMULACIÓN
# ──────────────────────────────────────────────────────────────
def run_simulation():
    global _sim_running
    reset()
    socketio.emit('simulation_start', snapshot())
    log("════ SIMULACIÓN INICIADA ════", 'system')

    threads = [
        threading.Thread(target=consumer, args=('even',),  name='Consumidor-Pares',   daemon=True),
        threading.Thread(target=consumer, args=('odd',),   name='Consumidor-Impares', daemon=True),
        threading.Thread(target=consumer, args=('prime',), name='Consumidor-Primos',  daemon=True),
        threading.Thread(target=producer,                  name='Productor',           daemon=True),
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
#  FLASK ROUTES & SOCKETIO EVENTS
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
#  HTML + CSS + JS  (plantilla Jinja2 embebida)
# ──────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Productor-Consumidor — SO / URL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=JetBrains+Mono:ital,wght@0,300;0,400;0,600;0,700;1,400&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
:root {
  --bg:      #07070f;
  --bg1:     #0d0d1f;
  --bg2:     #111128;
  --bg3:     #161638;
  --border:  #1c1c40;
  --border2: #262660;
  --pro:     #00ff88;
  --even:    #00ccff;
  --odd:     #ffaa00;
  --prime:   #ff3d71;
  --txt:     #c8c8ff;
  --dim:     #444488;
  --red:     #ff4444;
  --fh: 'Orbitron', monospace;
  --fm: 'JetBrains Mono', monospace;
}
*, *::before, *::after { box-sizing: border-box; margin:0; padding:0; }

body {
  background: var(--bg);
  color: var(--txt);
  font-family: var(--fm);
  min-height: 100vh;
  font-size: 13px;
  overflow-x: hidden;
}

/* subtle dot-grid background */
body::before {
  content: '';
  position: fixed; inset: 0;
  background-image: radial-gradient(circle, rgba(0,204,255,0.06) 1px, transparent 1px);
  background-size: 32px 32px;
  pointer-events: none;
  z-index: 0;
}

/* ─ HEADER ──────────────────────────────────── */
header {
  position: sticky; top: 0; z-index: 200;
  background: rgba(7,7,15,0.94);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border2);
  padding: 9px 20px;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
.logo {
  font-family: var(--fh);
  font-size: 10px; font-weight: 900; letter-spacing: 3px;
  white-space: nowrap;
}
.logo b { color: var(--pro); }
.hdr-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

.pill {
  padding: 3px 9px; border-radius: 20px;
  font-size: 9px; letter-spacing: 1px; border: 1px solid;
  font-family: var(--fh); font-weight: 700;
}
.p-file  { color: var(--dim);  border-color: var(--border2); }
.p-even  { color: var(--even);  border-color: var(--even);  background: rgba(0,204,255,0.05); }
.p-odd   { color: var(--odd);   border-color: var(--odd);   background: rgba(255,170,0,0.05); }
.p-prime { color: var(--prime); border-color: var(--prime); background: rgba(255,61,113,0.05); }

.btn-start {
  font-family: var(--fh); font-size: 9px; letter-spacing: 3px; font-weight: 700;
  padding: 8px 18px; border: 1px solid var(--pro);
  background: rgba(0,255,136,0.06); color: var(--pro);
  border-radius: 6px; cursor: pointer; transition: all .2s;
}
.btn-start:hover:not(:disabled) {
  background: rgba(0,255,136,0.18);
  box-shadow: 0 0 20px rgba(0,255,136,0.18);
}
.btn-start:disabled { opacity:.4; cursor:default; }

/* ─ LAYOUT ───────────────────────────────────── */
.page { position: relative; z-index: 1; }
.grid {
  display: grid;
  grid-template-columns: 210px 1fr 210px;
  gap: 12px; padding: 12px 18px;
  max-width: 1380px; margin: 0 auto;
}
@media (max-width:880px) { .grid { grid-template-columns: 1fr; } }

/* ─ CARDS ────────────────────────────────────── */
.card {
  background: var(--bg1); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden;
  transition: border-color .25s, box-shadow .25s;
}
.card + .card { margin-top: 10px; }

.card-hdr {
  padding: 8px 12px; background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-family: var(--fh); font-size: 8px; letter-spacing: 2px; font-weight: 700;
  display: flex; align-items: center; justify-content: space-between;
}
.card-body { padding: 12px; }

/* ─ STATE BADGE ──────────────────────────────── */
.badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 8px; border-radius: 20px; border: 1px solid var(--border2);
  font-family: var(--fh); font-size: 7px; letter-spacing: 1.5px;
  color: var(--dim); transition: all .2s;
}
.dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--dim); flex-shrink: 0; transition: background .2s;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.25} }
.blink { animation: blink .9s infinite; }

/* ─ BIG NUMBER ───────────────────────────────── */
.big-num {
  font-family: var(--fh); font-size: 32px; font-weight: 900;
  letter-spacing: 2px; text-align: center;
  min-height: 52px; display: flex; align-items: center; justify-content: center;
  transition: color .2s;
}
@keyframes numIn { 0%{transform:scale(1.5);opacity:.4} 100%{transform:scale(1);opacity:1} }
.num-flash { animation: numIn .35s cubic-bezier(.34,1.56,.64,1); }

.type-tag {
  text-align: center; font-size: 9px; letter-spacing: 2px; color: var(--dim);
  height: 16px; transition: color .2s;
}

/* ─ PROGRESS ─────────────────────────────────── */
.prog-wrap { margin-top: 10px; }
.prog-bar  { height: 2px; background: var(--border); border-radius: 2px; overflow: hidden; }
.prog-fill { height: 100%; background: var(--pro); border-radius: 2px; width:0%; transition: width .4s ease; }
.prog-lbl  { text-align:center; font-size:10px; color:var(--dim); margin-top:5px; letter-spacing:1px; }

/* ─ DISTRIB ──────────────────────────────────── */
.distrib { margin-top: 10px; display: flex; flex-direction: column; gap: 3px; }
.distrib-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 4px 8px; background: var(--bg2); border-radius: 4px; font-size: 10px;
}

/* ─ SUM BOX ──────────────────────────────────── */
.sum-box {
  margin-top: 10px; background: var(--bg2); border: 1px solid var(--border);
  border-radius: 8px; padding: 9px; text-align: center;
}
.sum-val  { font-family: var(--fh); font-size: 22px; font-weight: 700; transition: color .2s; }
.sum-lbl  { font-size: 8px; color: var(--dim); letter-spacing: 1.5px; margin-top: 2px; }

/* ─ BUFFER SLOTS ─────────────────────────────── */
.buf-slots { display: flex; flex-wrap: wrap; gap: 4px; padding: 8px 0 4px; }

.slot {
  width: 40px; height: 40px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--bg2);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--fh); font-size: 10px; font-weight: 700;
  color: transparent; transition: all .25s;
}
.slot.on {
  animation: slotIn .3s cubic-bezier(.34,1.56,.64,1) both;
}
@keyframes slotIn {
  from { transform: scale(.4) rotate(-8deg); opacity: 0; }
  to   { transform: scale(1)  rotate(0deg);  opacity: 1; }
}
.slot.off { animation: slotOut .2s ease both; }
@keyframes slotOut {
  from { transform: scale(1); opacity: 1; }
  to   { transform: scale(.3); opacity: 0; }
}

.buf-footer {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 9px; color: var(--dim); padding-top: 4px;
}
.buf-bar { flex:1; height:2px; background:var(--border); border-radius:2px; overflow:hidden; margin:0 8px; }
.buf-fill { height:100%; border-radius:2px; transition: width .3s ease; }

/* ─ CRITICAL / BLOCKED CARD GLOW ─────────────── */
.card.glow-crit    { border-color:#fff !important; box-shadow:0 0 22px rgba(255,255,255,.15) !important; }
.card.glow-blocked { border-color:var(--red) !important; box-shadow:0 0 18px rgba(255,68,68,.18) !important; }
.card.glow-even    { border-color:var(--even)  !important; box-shadow:0 0 18px rgba(0,204,255,.15) !important; }
.card.glow-odd     { border-color:var(--odd)   !important; box-shadow:0 0 18px rgba(255,170,0,.15) !important; }
.card.glow-prime   { border-color:var(--prime) !important; box-shadow:0 0 18px rgba(255,61,113,.15) !important; }

/* ─ LOG ──────────────────────────────────────── */
.log-sec { padding: 0 18px 18px; max-width:1380px; margin:0 auto; }
.log-wrap { background:var(--bg1); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
.log-hdr {
  padding: 7px 12px; background: var(--bg2); border-bottom: 1px solid var(--border);
  font-family: var(--fh); font-size: 8px; letter-spacing: 2px;
  display: flex; align-items: center; justify-content: space-between;
}
.log-clr {
  font-size: 8px; background: none; border: 1px solid var(--border2);
  color: var(--dim); border-radius: 3px; padding: 2px 7px; cursor: pointer;
  font-family: var(--fh); letter-spacing: 1px;
}
.log-clr:hover { border-color: var(--border); color: var(--txt); }
.log-body { height: 170px; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--border2) transparent; }
.log-row {
  display: flex; gap: 10px; padding: 4px 12px;
  border-bottom: 1px solid var(--border); font-size: 11px;
  animation: logIn .15s ease;
}
@keyframes logIn { from{opacity:0;transform:translateY(-3px)} to{opacity:1;transform:none} }
.log-ts     { color:var(--dim); min-width:62px; font-size:10px; flex-shrink:0; }
.c-producer { color:var(--pro);   }
.c-even     { color:var(--even);  }
.c-odd      { color:var(--odd);   }
.c-prime    { color:var(--prime); }
.c-blocked  { color:var(--red);   }
.c-system   { color:var(--dim); font-style:italic; }

/* ─ FINAL OVERLAY ────────────────────────────── */
.overlay {
  display:none; position:fixed; inset:0;
  background:rgba(7,7,15,.9); backdrop-filter:blur(10px);
  z-index:500; align-items:center; justify-content:center;
}
.overlay.show { display:flex; }
.final-card {
  background:var(--bg1); border:1px solid var(--border2); border-radius:14px;
  padding:34px; max-width:440px; width:92%; text-align:center;
  box-shadow: 0 0 60px rgba(0,255,136,.06), 0 0 120px rgba(0,204,255,.04);
  animation: cardIn .5s cubic-bezier(.34,1.56,.64,1);
}
@keyframes cardIn { from{transform:scale(.85);opacity:0} to{transform:scale(1);opacity:1} }
.final-title { font-family:var(--fh); font-size:11px; letter-spacing:4px; color:var(--pro); margin-bottom:22px; }
.final-row {
  display:flex; align-items:center; justify-content:space-between;
  background:var(--bg2); border-radius:8px; padding:11px 15px; margin-bottom:9px;
  font-family:var(--fh); font-size:9px; letter-spacing:2px;
}
.final-sum { font-family:var(--fh); font-size:20px; font-weight:700; }
.btn-rst {
  font-family:var(--fh); font-size:8px; letter-spacing:2px;
  padding:9px 22px; border:1px solid var(--border2); background:var(--bg2);
  color:var(--txt); border-radius:6px; cursor:pointer; transition:all .2s; margin-top:14px;
}
.btn-rst:hover { border-color:var(--pro); color:var(--pro); }
</style>
</head>
<body>
<div class="page">

<!-- ─ CONFIG DATA (leído por JS) ────────────────── -->
<div id="cfg"
  data-buf="{{ buffer_size }}"
  data-total="{{ total }}"
  data-n-even="{{ n_even }}"
  data-n-odd="{{ n_odd }}"
  data-n-prime="{{ n_prime }}"
  data-fp="{{ filepath }}"
  style="display:none"></div>

<!-- ─ HEADER ───────────────────────────────────── -->
<header>
  <div class="logo">PRODUCTOR<b>-CONSUMIDOR</b> &nbsp;/&nbsp; SISTEMAS OPERATIVOS — URL</div>
  <div class="hdr-right">
    <span class="pill p-file">{{ filepath }}</span>
    <span class="pill p-even">PARES&nbsp;{{ n_even }}</span>
    <span class="pill p-odd">IMPARES&nbsp;{{ n_odd }}</span>
    <span class="pill p-prime">PRIMOS&nbsp;{{ n_prime }}</span>
    <button class="btn-start" id="btn-start" onclick="startSim()">▶ INICIAR</button>
  </div>
</header>

<!-- ─ MAIN GRID ─────────────────────────────────── -->
<div class="grid">

<!-- LEFT: Productor ─────────────────────────────── -->
<div>
  <div class="card" id="card-producer">
    <div class="card-hdr">
      <span style="color:var(--pro)">◈ PRODUCTOR</span>
      <span class="badge" id="badge-producer">
        <span class="dot blink" id="dot-producer"></span>
        <span id="lbl-producer">IDLE</span>
      </span>
    </div>
    <div class="card-body">
      <div class="big-num" id="num-producer" style="color:var(--pro)">—</div>
      <div class="type-tag" id="tag-producer">—</div>
      <div class="prog-wrap">
        <div class="prog-bar"><div class="prog-fill" id="prog-fill"></div></div>
        <div class="prog-lbl" id="prog-lbl">0 / <span id="prog-total">{{ total }}</span></div>
      </div>
      <div class="distrib">
        <div class="distrib-row">
          <span style="color:var(--even)">PARES</span>
          <span style="color:var(--even)">{{ n_even }}</span>
        </div>
        <div class="distrib-row">
          <span style="color:var(--odd)">IMPARES</span>
          <span style="color:var(--odd)">{{ n_odd }}</span>
        </div>
        <div class="distrib-row">
          <span style="color:var(--prime)">PRIMOS</span>
          <span style="color:var(--prime)">{{ n_prime }}</span>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- CENTER: Buffers ──────────────────────────────── -->
<div>
  <!-- Buffer Pares -->
  <div class="card" id="card-buf-even" style="margin-bottom:10px">
    <div class="card-hdr">
      <span style="color:var(--even)">▣ BUFFER — PARES</span>
      <span id="cnt-even" style="color:var(--dim);font-size:9px">0 / {{ buffer_size }}</span>
    </div>
    <div class="card-body">
      <div class="buf-slots" id="slots-even"></div>
      <div class="buf-footer">
        <span>0</span>
        <div class="buf-bar"><div class="buf-fill" id="lvl-even" style="background:var(--even);width:0%"></div></div>
        <span>{{ buffer_size }}</span>
      </div>
    </div>
  </div>

  <!-- Buffer Impares -->
  <div class="card" id="card-buf-odd" style="margin-bottom:10px">
    <div class="card-hdr">
      <span style="color:var(--odd)">▣ BUFFER — IMPARES</span>
      <span id="cnt-odd" style="color:var(--dim);font-size:9px">0 / {{ buffer_size }}</span>
    </div>
    <div class="card-body">
      <div class="buf-slots" id="slots-odd"></div>
      <div class="buf-footer">
        <span>0</span>
        <div class="buf-bar"><div class="buf-fill" id="lvl-odd" style="background:var(--odd);width:0%"></div></div>
        <span>{{ buffer_size }}</span>
      </div>
    </div>
  </div>

  <!-- Buffer Primos -->
  <div class="card" id="card-buf-prime">
    <div class="card-hdr">
      <span style="color:var(--prime)">▣ BUFFER — PRIMOS</span>
      <span id="cnt-prime" style="color:var(--dim);font-size:9px">0 / {{ buffer_size }}</span>
    </div>
    <div class="card-body">
      <div class="buf-slots" id="slots-prime"></div>
      <div class="buf-footer">
        <span>0</span>
        <div class="buf-bar"><div class="buf-fill" id="lvl-prime" style="background:var(--prime);width:0%"></div></div>
        <span>{{ buffer_size }}</span>
      </div>
    </div>
  </div>
</div>

<!-- RIGHT: Consumidores ─────────────────────────── -->
<div>
  <!-- Consumidor Pares -->
  <div class="card" id="card-consumer-even" style="margin-bottom:10px">
    <div class="card-hdr">
      <span style="color:var(--even)">◈ CONS. PARES</span>
      <span class="badge" id="badge-consumer-even">
        <span class="dot blink" id="dot-consumer-even"></span>
        <span id="lbl-consumer-even">IDLE</span>
      </span>
    </div>
    <div class="card-body">
      <div class="big-num" id="num-consumer-even" style="color:var(--even)">—</div>
      <div class="sum-box">
        <div class="sum-lbl">SUMA ACUMULADA</div>
        <div class="sum-val" id="sum-even" style="color:var(--even)">0</div>
        <div class="sum-lbl" id="cnt-c-even" style="margin-top:3px">0 números</div>
      </div>
    </div>
  </div>

  <!-- Consumidor Impares -->
  <div class="card" id="card-consumer-odd" style="margin-bottom:10px">
    <div class="card-hdr">
      <span style="color:var(--odd)">◈ CONS. IMPARES</span>
      <span class="badge" id="badge-consumer-odd">
        <span class="dot blink" id="dot-consumer-odd"></span>
        <span id="lbl-consumer-odd">IDLE</span>
      </span>
    </div>
    <div class="card-body">
      <div class="big-num" id="num-consumer-odd" style="color:var(--odd)">—</div>
      <div class="sum-box">
        <div class="sum-lbl">SUMA ACUMULADA</div>
        <div class="sum-val" id="sum-odd" style="color:var(--odd)">0</div>
        <div class="sum-lbl" id="cnt-c-odd" style="margin-top:3px">0 números</div>
      </div>
    </div>
  </div>

  <!-- Consumidor Primos -->
  <div class="card" id="card-consumer-prime">
    <div class="card-hdr">
      <span style="color:var(--prime)">◈ CONS. PRIMOS</span>
      <span class="badge" id="badge-consumer-prime">
        <span class="dot blink" id="dot-consumer-prime"></span>
        <span id="lbl-consumer-prime">IDLE</span>
      </span>
    </div>
    <div class="card-body">
      <div class="big-num" id="num-consumer-prime" style="color:var(--prime)">—</div>
      <div class="sum-box">
        <div class="sum-lbl">SUMA ACUMULADA</div>
        <div class="sum-val" id="sum-prime" style="color:var(--prime)">0</div>
        <div class="sum-lbl" id="cnt-c-prime" style="margin-top:3px">0 números</div>
      </div>
    </div>
  </div>
</div>

</div><!-- end grid -->

<!-- ─ LOG ──────────────────────────────────────── -->
<div class="log-sec">
  <div class="log-wrap">
    <div class="log-hdr">
      <span>◉ REGISTRO DE EVENTOS</span>
      <button class="log-clr" onclick="document.getElementById('log-body').innerHTML=''">LIMPIAR</button>
    </div>
    <div class="log-body" id="log-body"></div>
  </div>
</div>

<!-- ─ FINAL OVERLAY ─────────────────────────────── -->
<div class="overlay" id="overlay">
  <div class="final-card">
    <div class="final-title">✓ SIMULACIÓN COMPLETADA</div>
    <div class="final-row">
      <span style="color:var(--even)">CONSUMIDOR PARES</span>
      <span>
        <span style="color:var(--dim);font-size:10px" id="f-cnt-even">0 núms</span>&nbsp;&nbsp;
        <span class="final-sum" id="f-sum-even" style="color:var(--even)">0</span>
      </span>
    </div>
    <div class="final-row">
      <span style="color:var(--odd)">CONSUMIDOR IMPARES</span>
      <span>
        <span style="color:var(--dim);font-size:10px" id="f-cnt-odd">0 núms</span>&nbsp;&nbsp;
        <span class="final-sum" id="f-sum-odd" style="color:var(--odd)">0</span>
      </span>
    </div>
    <div class="final-row">
      <span style="color:var(--prime)">CONSUMIDOR PRIMOS</span>
      <span>
        <span style="color:var(--dim);font-size:10px" id="f-cnt-prime">0 núms</span>&nbsp;&nbsp;
        <span class="final-sum" id="f-sum-prime" style="color:var(--prime)">0</span>
      </span>
    </div>
    <button class="btn-rst" onclick="restart()">↺ NUEVA EJECUCIÓN</button>
  </div>
</div>

</div><!-- end page -->
<script>
// ── Config desde atributos data-* ──────────────────────
const D = document.getElementById('cfg').dataset;
const BUF_SIZE = parseInt(D.buf);
const TOTAL    = parseInt(D.total);

// ── Variables de estado JS ─────────────────────────────
let prodIdx = 0;
const critTimers = {};

// ── Leer colores CSS ───────────────────────────────────
const cs = getComputedStyle(document.documentElement);
const COL = {
  even:  cs.getPropertyValue('--even').trim(),
  odd:   cs.getPropertyValue('--odd').trim(),
  prime: cs.getPropertyValue('--prime').trim(),
  pro:   cs.getPropertyValue('--pro').trim(),
};

// ── Configuración de estados ───────────────────────────
const SCFG = {
  idle:      { lbl:'IDLE',          dot:'#444488', border:null,     bg:null },
  waiting:   { lbl:'EN ESPERA',     dot:'#5555aa', border:null,     bg:null },
  producing: { lbl:'PRODUCIENDO',   dot:'#00ff88', border:'#00ff88',bg:'rgba(0,255,136,.08)' },
  consuming: { lbl:'CONSUMIENDO',   dot:null,      border:null,     bg:null },
  blocked:   { lbl:'BLOQUEADO ⚠',  dot:'#ff4444', border:'#ff4444',bg:'rgba(255,68,68,.1)' },
  critical:  { lbl:'SEC. CRÍTICA',  dot:'#ffffff', border:'#ffffff',bg:'rgba(255,255,255,.08)' },
  done:      { lbl:'TERMINADO ✓',   dot:'#444488', border:null,     bg:null },
};

// ── Render buffers ─────────────────────────────────────
function renderBuf(kind, contents) {
  const wrap = document.getElementById('slots-' + kind);
  const cnt  = document.getElementById('cnt-' + kind);
  const lvl  = document.getElementById('lvl-' + kind);
  const col  = COL[kind];
  const n    = contents.length;

  cnt.textContent = n + ' / ' + BUF_SIZE;
  lvl.style.width = (n / BUF_SIZE * 100) + '%';

  wrap.innerHTML = '';
  for (let i = 0; i < BUF_SIZE; i++) {
    const s = document.createElement('div');
    s.className = 'slot' + (i < n ? ' on' : '');
    if (i < n) {
      s.style.background  = col + '22';
      s.style.borderColor = col;
      s.style.color       = col;
      s.textContent       = contents[i];
    }
    wrap.appendChild(s);
  }
}

function renderAll(bufs) {
  renderBuf('even',  (bufs && bufs.even)  || []);
  renderBuf('odd',   (bufs && bufs.odd)   || []);
  renderBuf('prime', (bufs && bufs.prime) || []);
}

// ── Actualizar actor (badge + card) ───────────────────
function setActor(key, state, kind) {
  // Derivar IDs
  const isProd  = key === 'producer';
  const suffix  = isProd ? 'producer' : 'consumer-' + kind;
  const cardId  = isProd ? 'card-producer' : 'card-consumer-' + kind;

  const badge = document.getElementById('badge-' + suffix);
  const dot   = document.getElementById('dot-'   + suffix);
  const lbl   = document.getElementById('lbl-'   + suffix);
  const card  = document.getElementById(cardId);
  if (!badge) return;

  const cfg   = SCFG[state] || SCFG.idle;
  const color = kind ? COL[kind] : COL.pro;

  let dotCol = cfg.dot  || color;
  let border = cfg.border || '';
  let bg     = cfg.bg     || '';
  let text   = cfg.lbl;

  if (state === 'consuming') {
    dotCol = color; border = color; bg = color + '15'; text = 'CONSUMIENDO';
  }

  lbl.textContent    = text;
  dot.style.background = dotCol;
  dot.className      = 'dot' + (state === 'waiting' || state === 'blocked' ? ' blink' : '');
  badge.style.borderColor = border;
  badge.style.background  = bg;
  badge.style.color       = border || '';

  // Glow en la tarjeta
  if (card) {
    card.className = 'card';
    if      (state === 'critical') card.classList.add('glow-crit');
    else if (state === 'blocked')  card.classList.add('glow-blocked');
    else if (state === 'consuming' || state === 'producing') {
      // glow suave del color propio
      if (kind) card.classList.add('glow-' + kind);
    }
  }

  // Auto-apagar "critical" después de 500ms si no hay otro update
  if (state === 'critical') {
    clearTimeout(critTimers[key]);
    critTimers[key] = setTimeout(() => {
      if (card) card.className = 'card';
    }, 500);
  }
}

function applyStates(states) {
  if (!states) return;
  setActor('producer',       states.producer,       null);
  setActor('consumer_even',  states.consumer_even,  'even');
  setActor('consumer_odd',   states.consumer_odd,   'odd');
  setActor('consumer_prime', states.consumer_prime, 'prime');
}

// ── Log ───────────────────────────────────────────────
function addLog(msg, kind, ts) {
  const body = document.getElementById('log-body');
  const row  = document.createElement('div');
  row.className = 'log-row';
  row.innerHTML = '<span class="log-ts">' + ts + '</span>'
    + '<span class="c-' + kind + '">' + msg + '</span>';
  body.insertBefore(row, body.firstChild);
  while (body.children.length > 150) body.removeChild(body.lastChild);
}

// ── Socket.IO ─────────────────────────────────────────
const socket = io();

socket.on('connect', () =>
  addLog('Conectado al servidor', 'system', new Date().toLocaleTimeString())
);

socket.on('ready', data => {
  renderAll(data.buffers);
});

socket.on('simulation_start', data => {
  prodIdx = 0;
  renderAll(data.buffers);
  applyStates(data.states);
  document.getElementById('btn-start').disabled    = true;
  document.getElementById('btn-start').textContent = '⟳ EJECUTANDO';
  document.getElementById('prog-fill').style.width = '0%';
  document.getElementById('prog-lbl').innerHTML    = '0 / <span id="prog-total">' + TOTAL + '</span>';
  ['even','odd','prime'].forEach(k => {
    document.getElementById('sum-'     + k).textContent = '0';
    document.getElementById('cnt-c-'  + k).textContent = '0 números';
    document.getElementById('num-consumer-' + k).textContent = '—';
  });
  document.getElementById('num-producer').textContent = '—';
  document.getElementById('tag-producer').textContent = '—';
});

socket.on('insert', data => {
  prodIdx++;
  const pct = prodIdx / TOTAL * 100;
  document.getElementById('prog-fill').style.width = pct + '%';
  document.getElementById('prog-lbl').textContent  = prodIdx + ' / ' + TOTAL;

  const numEl = document.getElementById('num-producer');
  numEl.textContent  = data.num;
  numEl.style.color  = COL[data.kind];
  numEl.classList.remove('num-flash');
  void numEl.offsetWidth;
  numEl.classList.add('num-flash');

  const tagEl = document.getElementById('tag-producer');
  tagEl.textContent = {even:'PAR', odd:'IMPAR', prime:'PRIMO'}[data.kind];
  tagEl.style.color = COL[data.kind];

  renderAll(data.buffers);
  applyStates(data.states);
});

socket.on('take', data => {
  const numEl = document.getElementById('num-consumer-' + data.consumer);
  numEl.textContent = data.num;
  numEl.classList.remove('num-flash');
  void numEl.offsetWidth;
  numEl.classList.add('num-flash');

  document.getElementById('sum-'    + data.consumer).textContent = data.sum;
  document.getElementById('cnt-c-'  + data.consumer).textContent =
    data.count + ' número' + (data.count !== 1 ? 's' : '');

  renderAll(data.buffers);
  applyStates(data.states);
});

socket.on('critical_enter', data => {
  applyStates(data.states);
  renderAll(data.buffers);
});

socket.on('state_update', data => {
  renderAll(data.buffers);
  applyStates(data.states);
  if (data.sums) {
    ['even','odd','prime'].forEach(k =>
      document.getElementById('sum-' + k).textContent = data.sums[k]
    );
  }
  if (data.counts) {
    ['even','odd','prime'].forEach(k =>
      document.getElementById('cnt-c-' + k).textContent =
        data.counts[k] + ' número' + (data.counts[k] !== 1 ? 's' : '')
    );
  }
});

socket.on('log', data => addLog(data.msg, data.kind, data.ts));

socket.on('simulation_end', data => {
  document.getElementById('btn-start').textContent = '✓ COMPLETADO';
  ['even','odd','prime'].forEach(k => {
    document.getElementById('f-sum-' + k).textContent = data.sums[k];
    document.getElementById('f-cnt-' + k).textContent = data.counts[k] + ' núms';
  });
  setTimeout(() =>
    document.getElementById('overlay').classList.add('show'), 1200
  );
});

// ── Acciones del usuario ───────────────────────────────
function startSim() { socket.emit('start'); }

function restart() {
  document.getElementById('overlay').classList.remove('show');
  const btn = document.getElementById('btn-start');
  btn.disabled = false;
  btn.textContent = '▶ INICIAR';
  prodIdx = 0;
  document.getElementById('log-body').innerHTML   = '';
  document.getElementById('num-producer').textContent = '—';
  document.getElementById('tag-producer').textContent = '—';
  document.getElementById('num-producer').style.color = 'var(--pro)';
  document.getElementById('prog-fill').style.width    = '0%';
  document.getElementById('prog-lbl').textContent     = '0 / ' + TOTAL;
  ['even','odd','prime'].forEach(k => {
    document.getElementById('sum-'    + k).textContent = '0';
    document.getElementById('cnt-c-'  + k).textContent = '0 números';
    document.getElementById('num-consumer-' + k).textContent = '—';
    setActor('consumer_' + k, 'idle', k);
    renderBuf(k, []);
  });
  setActor('producer', 'idle', null);
}

// ── Init ───────────────────────────────────────────────
renderAll({even:[], odd:[], prime:[]});
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
def main():
    global _numbers, _filepath

    # Obtener ruta del archivo
    if len(sys.argv) > 1:
        fp = sys.argv[1]
    else:
        fp = input("\nIngrese la ruta del archivo .txt: ").strip()

    if not os.path.exists(fp):
        print(f"\n[ERROR] No se encontró: '{fp}'")
        sys.exit(1)

    # Leer y validar números
    nums, skipped = [], []
    with open(fp, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                nums.append(int(line))
            except ValueError:
                skipped.append((i, line))

    if skipped:
        print(f"[AVISO] Se omitieron {len(skipped)} líneas no numéricas.")
    if not nums:
        print("[ERROR] No hay números válidos en el archivo.")
        sys.exit(1)

    _numbers  = nums
    _filepath = os.path.basename(fp)

    evens  = [n for n in nums if classify(n) == 'even']
    odds   = [n for n in nums if classify(n) == 'odd']
    primes = [n for n in nums if classify(n) == 'prime']

    print(f"\n{'─'*52}")
    print(f"  Productor-Consumidor — Sistemas Operativos — URL")
    print(f"{'─'*52}")
    print(f"  Archivo  : {_filepath}")
    print(f"  Total    : {len(nums)} números")
    print(f"  Pares    : {len(evens)}   {evens}")
    print(f"  Impares  : {len(odds)}   {odds}")
    print(f"  Primos   : {len(primes)}  {primes}")
    print(f"{'─'*52}")
    print(f"  Servidor : http://localhost:5000")
    print(f"  (El navegador se abrirá automáticamente)\n")

    # Abrir navegador tras 1.5 segundos
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