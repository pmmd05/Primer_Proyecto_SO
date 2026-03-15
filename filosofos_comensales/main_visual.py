#!/usr/bin/env python3
"""
=============================================================
  FILÓSOFOS COMENSALES — Visualización Web en Tiempo Real
  Sistemas Operativos — Universidad Rafael Landívar
=============================================================
  Mesa circular animada con Flask + SocketIO.
  Cada filósofo muestra su imagen de estado en tiempo real.

  Estructura de carpetas esperada:
      main_visual.py
      imagenes/
          pensando.png
          esperando.png
          comiendo.png

  Uso:
      python main_visual.py          (5 filósofos, 8 ciclos)
      python main_visual.py 5 10     (N filósofos, ciclos)
=============================================================
"""

import threading
import time
import random
import sys
import os
import webbrowser
from flask import Flask, render_template_string, send_from_directory
from flask_socketio import SocketIO

# ──────────────────────────────────────────────────────────────
#  CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────
DEFAULT_N      = 5
DEFAULT_CYCLES = 10
THINK_MIN      = 0.8
THINK_MAX      = 2.0
EAT_MIN        = 0.8
EAT_MAX        = 1.8
ACQUIRE_PAUSE  = 0.25   # pausa visual al intentar adquirir tenedor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

NAMES = ["Platón", "Aristóteles", "Sócrates", "Confucio",
         "Descartes", "Kant", "Nietzsche", "Hume", "Locke", "Spinoza"]

# ──────────────────────────────────────────────────────────────
#  FLASK + SOCKETIO
# ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = 'filosofos-url-2025'
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*',
                    logger=False, engineio_logger=False)

# ──────────────────────────────────────────────────────────────
#  ESTADO COMPARTIDO
# ──────────────────────────────────────────────────────────────
_n_philosophers : int  = DEFAULT_N
_cycles         : int  = DEFAULT_CYCLES
_sim_running    : bool = False
_sim_lock       = threading.Lock()

# Por filósofo: estado, comidas
# Estados: 'idle' | 'thinking' | 'waiting' | 'eating' | 'done'
_phil_states : list = []
_eat_counts  : list = []
_state_lock  = threading.Lock()

# Por tenedor: libre(False) / ocupado(True) y quién lo tiene
_fork_held   : list = []   # bool
_fork_by     : list = []   # índice del filósofo que lo tiene, o -1
_fork_lock   = threading.Lock()

# Primitivas de sincronización para los tenedores
_forks : list = []   # lista de threading.Lock()

# ──────────────────────────────────────────────────────────────
#  GESTIÓN DE ESTADO
# ──────────────────────────────────────────────────────────────
def reset(n: int, cycles: int):
    global _phil_states, _eat_counts, _fork_held, _fork_by, _forks
    global _n_philosophers, _cycles

    _n_philosophers = n
    _cycles         = cycles
    _phil_states    = ['idle'] * n
    _eat_counts     = [0] * n
    _fork_held      = [False] * n
    _fork_by        = [-1] * n
    _forks          = [threading.Lock() for _ in range(n)]


def snapshot() -> dict:
    with _state_lock:
        states = list(_phil_states)
        counts = list(_eat_counts)
    with _fork_lock:
        fheld  = list(_fork_held)
        fby    = list(_fork_by)
    return {
        'states':    states,
        'counts':    counts,
        'fork_held': fheld,
        'fork_by':   fby,
        'n':         _n_philosophers,
        'cycles':    _cycles,
        'names':     NAMES[:_n_philosophers],
    }


def set_phil_state(i: int, state: str):
    with _state_lock:
        _phil_states[i] = state


def set_fork(fork_idx: int, held: bool, by: int = -1):
    with _fork_lock:
        _fork_held[fork_idx] = held
        _fork_by[fork_idx]   = by


def emit_ev(event: str, extra: dict = None):
    data = snapshot()
    if extra:
        data.update(extra)
    socketio.emit(event, data)


def log(msg: str, kind: str = 'system'):
    ts = time.strftime('%H:%M:%S')
    socketio.emit('log', {'msg': msg, 'kind': kind, 'ts': ts})


# ──────────────────────────────────────────────────────────────
#  FILÓSOFO (hilo)
# ──────────────────────────────────────────────────────────────
def philosopher(idx: int, n: int, cycles: int):
    """
    Lógica completa de un filósofo con eventos para la UI.

    Prevención de deadlock — ordenamiento asimétrico:
      Filósofo n-1 toma derecho→izquierdo en lugar de izquierdo→derecho.
      Rompe la espera circular garantizando que nunca todos los filósofos
      bloqueen simultáneamente esperando su segundo tenedor.
    """
    name  = NAMES[idx % len(NAMES)]
    left  = idx
    right = (idx + 1) % n

    # Ordenamiento asimétrico: último filósofo invierte el orden
    if idx == n - 1:
        first_fork, second_fork = right, left
    else:
        first_fork, second_fork = left, right

    set_phil_state(idx, 'thinking')
    log(f"{name} se sienta a la mesa (tenedores {left}↔{right})", str(idx))
    emit_ev('state_update')

    for cycle in range(1, cycles + 1):

        # ── PENSANDO ──────────────────────────────────────────
        set_phil_state(idx, 'thinking')
        log(f"{name} pensando... (ciclo {cycle}/{cycles})", str(idx))
        emit_ev('state_update')
        time.sleep(random.uniform(THINK_MIN, THINK_MAX))

        # ── ESPERANDO primer tenedor ───────────────────────────
        set_phil_state(idx, 'waiting')
        log(f"{name} intenta tomar tenedor {first_fork}", str(idx))
        emit_ev('fork_attempt', {'philosopher': idx, 'fork': first_fork})

        _forks[first_fork].acquire()           # puede bloquearse

        set_fork(first_fork, True, idx)
        log(f"{name} tomó tenedor {first_fork}", str(idx))
        emit_ev('fork_taken', {'philosopher': idx, 'fork': first_fork})
        time.sleep(ACQUIRE_PAUSE)

        # ── ESPERANDO segundo tenedor ──────────────────────────
        log(f"{name} intenta tomar tenedor {second_fork}", str(idx))
        emit_ev('fork_attempt', {'philosopher': idx, 'fork': second_fork})

        _forks[second_fork].acquire()          # puede bloquearse

        set_fork(second_fork, True, idx)
        log(f"{name} tomó tenedor {second_fork}", str(idx))
        emit_ev('fork_taken', {'philosopher': idx, 'fork': second_fork})

        # ── COMIENDO (sección crítica) ─────────────────────────
        set_phil_state(idx, 'eating')
        eat_t = random.uniform(EAT_MIN, EAT_MAX)
        log(f"{name} COMIENDO con tenedores {first_fork} y {second_fork} ({eat_t:.2f}s)", str(idx))
        emit_ev('eating_start', {'philosopher': idx,
                                  'forks': [first_fork, second_fork],
                                  'duration': eat_t})
        time.sleep(eat_t)

        with _state_lock:
            _eat_counts[idx] += 1
            count = _eat_counts[idx]

        # ── LIBERAR TENEDORES ─────────────────────────────────
        _forks[first_fork].release()
        set_fork(first_fork, False, -1)

        _forks[second_fork].release()
        set_fork(second_fork, False, -1)

        log(f"{name} soltó tenedores {first_fork} y {second_fork} (comidas: {count})", str(idx))
        emit_ev('eating_end', {'philosopher': idx,
                                'forks': [first_fork, second_fork],
                                'count': count})

    # ── TERMINADO ─────────────────────────────────────────────
    set_phil_state(idx, 'done')
    with _state_lock:
        final = _eat_counts[idx]
    log(f"{name} terminó — comió {final} veces", str(idx))
    emit_ev('state_update')


# ──────────────────────────────────────────────────────────────
#  RUNNER
# ──────────────────────────────────────────────────────────────
def run_simulation(n: int, cycles: int):
    global _sim_running

    reset(n, cycles)
    socketio.emit('simulation_start', snapshot())
    log(f"════ SIMULACIÓN INICIADA — {n} filósofos, {cycles} ciclos ════", 'system')

    threads = [
        threading.Thread(target=philosopher, args=(i, n, cycles),
                         name=NAMES[i % len(NAMES)], daemon=True)
        for i in range(n)
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    with _sim_lock:
        _sim_running = False

    with _state_lock:
        final_counts = list(_eat_counts)

    log("════ SIMULACIÓN COMPLETADA ════", 'system')
    socketio.emit('simulation_end', {
        'counts': final_counts,
        'names':  NAMES[:n],
        'cycles': cycles,
    })


# ──────────────────────────────────────────────────────────────
#  FLASK ROUTES
# ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML,
        default_n      = DEFAULT_N,
        default_cycles = DEFAULT_CYCLES,
        names_js       = str(NAMES),
    )

@app.route('/imagenes/<path:filename>')
def serve_image(filename):
    img_dir = os.path.join(SCRIPT_DIR, 'imagenes')
    return send_from_directory(img_dir, filename)

@socketio.on('connect')
def on_connect():
    socketio.emit('ready', snapshot())

@socketio.on('start')
def on_start(data):
    global _sim_running
    with _sim_lock:
        if _sim_running:
            return
        _sim_running = True
    n      = max(2, min(10, int(data.get('n',      DEFAULT_N))))
    cycles = max(1, min(50, int(data.get('cycles', DEFAULT_CYCLES))))
    threading.Thread(target=run_simulation, args=(n, cycles), daemon=True).start()


# ──────────────────────────────────────────────────────────────
#  HTML COMPLETO
# ──────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Filósofos Comensales — SO / URL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;900&family=JetBrains+Mono:wght@300;400;600&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
/* ── Variables ─────────────────────────────────────────────── */
:root {
  --bg:      #0a0705;
  --bg1:     #110e08;
  --bg2:     #1a1408;
  --bg3:     #231c0e;
  --border:  #2e2210;
  --bord2:   #45330f;
  --gold:    #d4a017;
  --gold2:   #f0c040;
  --amber:   #ff9500;
  --red:     #ff4444;
  --green:   #44cc88;
  --blue:    #44aaff;
  --txt:     #e8d8b0;
  --dim:     #6b5a30;
  --white:   #fff8ee;
  --table:   #3d2810;
  --table2:  #5a3c18;
  --fh: 'Cinzel', serif;
  --fm: 'JetBrains Mono', monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;width:100%}
body{
  background:var(--bg);
  color:var(--txt);
  font-family:var(--fm);
  font-size:12px;
  min-height:100vh;
  overflow-x:hidden;
}

/* Parchment texture overlay */
body::before{
  content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(ellipse at 20% 20%, rgba(212,160,23,.04) 0%, transparent 60%),
    radial-gradient(ellipse at 80% 80%, rgba(255,149,0,.04) 0%, transparent 60%),
    radial-gradient(ellipse at 50% 50%, rgba(10,7,5,0) 0%, rgba(10,7,5,.5) 100%);
}

/* ── Header ────────────────────────────────────────────────── */
header{
  position:sticky;top:0;z-index:300;
  background:rgba(10,7,5,.96);backdrop-filter:blur(16px);
  border-bottom:1px solid var(--bord2);
  padding:8px 20px;
  display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
}
.logo{font-family:var(--fh);font-size:11px;font-weight:900;
  letter-spacing:3px;color:var(--gold2);white-space:nowrap;
  text-shadow:0 0 20px rgba(212,160,23,.4)}
.hdr-r{display:flex;align-items:center;gap:10px;flex-wrap:wrap}

/* controls */
.ctrl-group{display:flex;align-items:center;gap:6px}
.ctrl-lbl{font-family:var(--fh);font-size:7px;letter-spacing:2px;color:var(--dim)}
.ctrl-input{
  width:52px;padding:4px 6px;background:var(--bg2);border:1px solid var(--bord2);
  color:var(--gold2);font-family:var(--fm);font-size:11px;border-radius:4px;
  text-align:center;
}
.ctrl-input:focus{outline:none;border-color:var(--gold)}

.btn-go{
  font-family:var(--fh);font-size:8px;letter-spacing:2px;font-weight:600;
  padding:8px 18px;border:1px solid var(--gold);background:rgba(212,160,23,.08);
  color:var(--gold2);border-radius:5px;cursor:pointer;transition:all .2s;
}
.btn-go:hover:not(:disabled){
  background:rgba(212,160,23,.2);
  box-shadow:0 0 20px rgba(212,160,23,.25);
}
.btn-go:disabled{opacity:.35;cursor:default}

/* ── Main layout ────────────────────────────────────────────── */
.page{position:relative;z-index:1}
.layout{display:flex;flex-direction:column;align-items:center;padding:16px}

/* ── TABLE SCENE ────────────────────────────────────────────── */
.scene-wrap{
  position:relative;
  /* size is set dynamically in JS, default 540px */
  width:540px;height:540px;
  flex-shrink:0;
}

/* Round table */
.table-el{
  position:absolute;
  top:50%;left:50%;
  width:200px;height:200px;
  transform:translate(-50%,-50%);
  border-radius:50%;
  background:
    radial-gradient(circle at 38% 35%, var(--table2) 0%, var(--table) 60%, #1e0f00 100%);
  border:3px solid var(--bord2);
  box-shadow:
    0 0 0 1px #6b4a1a,
    0 8px 40px rgba(0,0,0,.7),
    inset 0 2px 8px rgba(255,180,60,.08);
  display:flex;align-items:center;justify-content:center;
}
.table-text{
  font-family:var(--fh);font-size:9px;letter-spacing:2px;
  color:rgba(212,160,23,.45);text-align:center;line-height:1.6;
}

/* ── PHILOSOPHER SEAT ───────────────────────────────────────── */
.phil-seat{
  position:absolute;
  /* centered at origin, then moved by JS */
  transform-origin:center center;
  display:flex;flex-direction:column;align-items:center;gap:4px;
  /* seat card size */
  width:108px;
}
.phil-card{
  background:var(--bg1);border:1px solid var(--border);border-radius:10px;
  padding:8px 6px 6px;width:100%;
  display:flex;flex-direction:column;align-items:center;gap:4px;
  transition:border-color .3s,box-shadow .3s;
  cursor:default;
}
/* state glows */
.phil-card.g-thinking{border-color:var(--bord2)}
.phil-card.g-waiting {border-color:var(--amber)!important;box-shadow:0 0 16px rgba(255,149,0,.2)!important}
.phil-card.g-eating  {border-color:var(--gold2)!important;box-shadow:0 0 22px rgba(240,192,64,.3)!important}
.phil-card.g-done    {border-color:var(--dim)!important;opacity:.6}

.phil-img{
  width:58px;height:58px;object-fit:contain;border-radius:6px;
  transition:filter .3s,transform .3s;
}
.phil-img.s-thinking{filter:brightness(.75) sepia(.3);transform:scale(.95)}
.phil-img.s-waiting {filter:brightness(.9) sepia(.1) drop-shadow(0 0 5px var(--amber));transform:scale(1)}
.phil-img.s-eating  {filter:brightness(1.1) drop-shadow(0 0 10px var(--gold2));transform:scale(1.06)}
.phil-img.s-done    {filter:grayscale(1) brightness(.5)}
.phil-img.s-idle    {filter:brightness(.5);transform:scale(.9)}

.phil-name{
  font-family:var(--fh);font-size:8px;letter-spacing:1px;
  color:var(--gold);text-align:center;white-space:nowrap;
}
.phil-state-lbl{
  font-family:var(--fh);font-size:6.5px;letter-spacing:1.5px;
  height:12px;text-align:center;transition:color .2s;
}
.phil-eats{
  font-size:9px;color:var(--dim);font-family:var(--fh);
}
.phil-eats b{color:var(--gold2)}

/* eat pulse animation */
@keyframes eatPulse{
  0%  {box-shadow:0 0 22px rgba(240,192,64,.3)}
  50% {box-shadow:0 0 40px rgba(240,192,64,.6)}
  100%{box-shadow:0 0 22px rgba(240,192,64,.3)}
}
.phil-card.eating-anim{animation:eatPulse 1s ease infinite}

/* ── FORK ────────────────────────────────────────────────────── */
.fork-el{
  position:absolute;
  width:28px;height:28px;
  transform-origin:center center;
  display:flex;align-items:center;justify-content:center;
  font-size:18px;
  transition:filter .25s,transform .25s;
  pointer-events:none;
  user-select:none;
}
.fork-el.free{
  filter:brightness(.45);
  transform:rotate(0deg);
}
.fork-el.held{
  filter:brightness(1.3) drop-shadow(0 0 8px var(--gold2));
  transform:rotate(30deg) scale(1.15);
}
@keyframes forkGrab{
  0%  {transform:rotate(0deg) scale(1)}
  40% {transform:rotate(20deg) scale(1.2)}
  100%{transform:rotate(30deg) scale(1.15)}
}
.fork-el.held{animation:forkGrab .3s ease forwards}

/* ── STATS BAR ──────────────────────────────────────────────── */
.stats-bar{
  display:flex;gap:8px;flex-wrap:wrap;justify-content:center;
  max-width:600px;margin-top:14px;
}
.stat-chip{
  display:flex;align-items:center;gap:5px;
  padding:4px 10px;background:var(--bg2);border:1px solid var(--border);
  border-radius:20px;font-family:var(--fh);font-size:8px;letter-spacing:1px;
}
.stat-dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
.stat-chip.s-thinking .stat-dot{background:var(--blue)}
.stat-chip.s-waiting  .stat-dot{background:var(--amber);animation:blink .8s infinite}
.stat-chip.s-eating   .stat-dot{background:var(--gold2)}
.stat-chip.s-done     .stat-dot{background:var(--dim)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

/* ── LOG ────────────────────────────────────────────────────── */
.log-sec{width:100%;max-width:900px;margin-top:14px}
.log-wrap{background:var(--bg1);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.log-hdr{
  padding:6px 12px;background:var(--bg2);border-bottom:1px solid var(--border);
  font-family:var(--fh);font-size:7px;letter-spacing:2px;color:var(--gold);
  display:flex;align-items:center;justify-content:space-between;
}
.log-clr{font-size:7px;background:none;border:1px solid var(--border);color:var(--dim);
  border-radius:3px;padding:2px 7px;cursor:pointer;font-family:var(--fh);letter-spacing:1px}
.log-clr:hover{border-color:var(--bord2);color:var(--txt)}
.log-body{height:150px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--bord2) transparent}
.log-row{display:flex;gap:8px;padding:3px 12px;border-bottom:1px solid var(--border);
  font-size:10px;animation:logIn .12s ease}
@keyframes logIn{from{opacity:0;transform:translateY(-2px)}to{opacity:1;transform:none}}
.log-ts{color:var(--dim);min-width:58px;font-size:9px;flex-shrink:0}
.c-system{color:var(--dim);font-style:italic}
/* philosopher colors (set dynamically) */

/* ── OVERLAY FINAL ──────────────────────────────────────────── */
.overlay{
  display:none;position:fixed;inset:0;
  background:rgba(10,7,5,.94);backdrop-filter:blur(14px);
  z-index:500;align-items:center;justify-content:center;
}
.overlay.show{display:flex}
.fin-card{
  background:var(--bg1);border:1px solid var(--bord2);border-radius:14px;
  padding:32px;max-width:460px;width:92%;text-align:center;
  box-shadow:0 0 60px rgba(212,160,23,.06);
  animation:cardIn .45s cubic-bezier(.34,1.56,.64,1);
}
@keyframes cardIn{from{transform:scale(.85);opacity:0}to{transform:scale(1);opacity:1}}
.fin-title{font-family:var(--fh);font-size:11px;letter-spacing:4px;
  color:var(--gold2);margin-bottom:20px;text-shadow:0 0 20px rgba(240,192,64,.3)}
.fin-row{
  display:flex;align-items:center;justify-content:space-between;
  background:var(--bg2);border-radius:7px;padding:9px 14px;margin-bottom:7px;
  font-family:var(--fh);font-size:8px;letter-spacing:2px;
}
.fin-val{font-family:var(--fh);font-size:17px;font-weight:900;color:var(--gold2)}
.fin-sub{font-size:8px;color:var(--dim)}
.btn-rst{
  font-family:var(--fh);font-size:7px;letter-spacing:2px;margin-top:14px;
  padding:9px 20px;border:1px solid var(--bord2);background:var(--bg2);
  color:var(--txt);border-radius:6px;cursor:pointer;transition:all .2s;
}
.btn-rst:hover{border-color:var(--gold);color:var(--gold2)}
</style>
</head>
<body>
<div class="page">

<!-- ── HEADER ──────────────────────────────────────────────── -->
<header>
  <div class="logo">FILÓSOFOS COMENSALES &nbsp;/&nbsp; SO — URL</div>
  <div class="hdr-r">
    <div class="ctrl-group">
      <span class="ctrl-lbl">FILÓSOFOS</span>
      <input class="ctrl-input" id="inp-n" type="number" min="2" max="10" value="{{ default_n }}">
    </div>
    <div class="ctrl-group">
      <span class="ctrl-lbl">CICLOS</span>
      <input class="ctrl-input" id="inp-c" type="number" min="1" max="50" value="{{ default_cycles }}">
    </div>
    <button class="btn-go" id="btn-go" onclick="startSim()">▶ INICIAR</button>
  </div>
</header>

<!-- ── SCENE ────────────────────────────────────────────────── -->
<div class="layout">

  <div class="scene-wrap" id="scene">
    <!-- Mesa redonda -->
    <div class="table-el" id="table-el">
      <div class="table-text" id="table-text">
        MESA<br>CIRCULAR
      </div>
    </div>
    <!-- Filósofos y tenedores se generan por JS -->
  </div>

  <!-- Stats chips -->
  <div class="stats-bar" id="stats-bar"></div>

  <!-- Log -->
  <div class="log-sec">
    <div class="log-wrap">
      <div class="log-hdr">
        <span>◉ REGISTRO DE EVENTOS</span>
        <button class="log-clr" onclick="document.getElementById('log-body').innerHTML=''">LIMPIAR</button>
      </div>
      <div class="log-body" id="log-body"></div>
    </div>
  </div>

</div>

<!-- ── OVERLAY ──────────────────────────────────────────────── -->
<div class="overlay" id="overlay">
  <div class="fin-card">
    <div class="fin-title">✦ BANQUETE COMPLETADO ✦</div>
    <div id="fin-rows"></div>
    <button class="btn-rst" onclick="restartUI()">↺ NUEVO BANQUETE</button>
  </div>
</div>

<!-- ── JAVASCRIPT ───────────────────────────────────────────── -->
<script>
// ─────────────────────────────────────────────────────────────
//  CONSTANTES Y CONFIG
// ─────────────────────────────────────────────────────────────
const ALL_NAMES   = {{ names_js }};
const DEFAULT_N   = {{ default_n }};
const DEFAULT_CYC = {{ default_cycles }};

// Colores por filósofo (asignados en orden)
const PHIL_COLORS = [
  '#44aaff','#44cc88','#ffb300','#ff6eb4','#ff5555',
  '#a78bfa','#34d399','#fbbf24','#60a5fa','#f87171',
];

// ─────────────────────────────────────────────────────────────
//  ESTADO JS
// ─────────────────────────────────────────────────────────────
let N = DEFAULT_N, cycles = DEFAULT_CYC;

// ─────────────────────────────────────────────────────────────
//  LAYOUT — posicionar elementos en círculo
// ─────────────────────────────────────────────────────────────
const SEAT_W   = 108;
const SEAT_H   = 128;   // approx card height
const SCENE_BASE = 540; // scene width/height base

function sceneRadius(n) {
  // Radio de la escena según cantidad de filósofos
  const base = Math.max(220, n * 52);
  return Math.min(base, 300);
}

function buildScene(n, names) {
  N = n;
  const scene    = document.getElementById('scene');
  const tableEl  = document.getElementById('table-el');
  const sceneSize = Math.max(540, n * 110 + 60);

  scene.style.width  = sceneSize + 'px';
  scene.style.height = sceneSize + 'px';

  const cx = sceneSize / 2;
  const cy = sceneSize / 2;
  const R  = sceneRadius(n);

  // Clear previous philosophers/forks (keep table)
  document.querySelectorAll('.phil-seat, .fork-el').forEach(el => el.remove());

  // Table size relative to scene
  const tableSize = Math.max(160, sceneSize * 0.28);
  tableEl.style.width  = tableSize + 'px';
  tableEl.style.height = tableSize + 'px';

  // Build philosophers
  for (let i = 0; i < n; i++) {
    const angle = (2 * Math.PI * i / n) - Math.PI / 2;  // start from top
    const px = cx + R * Math.cos(angle);
    const py = cy + R * Math.sin(angle);

    const seat = document.createElement('div');
    seat.className = 'phil-seat';
    seat.id = 'seat-' + i;
    seat.style.left = (px - SEAT_W / 2) + 'px';
    seat.style.top  = (py - SEAT_H / 2) + 'px';

    seat.innerHTML = `
      <div class="phil-card g-thinking" id="card-${i}">
        <img class="phil-img s-idle" id="img-${i}"
             src="/imagenes/pensando.png" alt="${names[i]}">
        <div class="phil-name">${names[i]}</div>
        <div class="phil-state-lbl" id="slbl-${i}" style="color:var(--dim)">IDLE</div>
        <div class="phil-eats" id="eats-${i}">comidas: <b>0</b></div>
      </div>`;
    scene.appendChild(seat);
  }

  // Build forks (between philosophers)
  for (let i = 0; i < n; i++) {
    const angle1 = (2 * Math.PI * i / n) - Math.PI / 2;
    const angle2 = (2 * Math.PI * ((i + 1) % n) / n) - Math.PI / 2;
    const midAngle = (angle1 + angle2) / 2;
    const forkR = R * 0.62;

    const fx = cx + forkR * Math.cos(midAngle);
    const fy = cy + forkR * Math.sin(midAngle);

    const fork = document.createElement('div');
    fork.className = 'fork-el free';
    fork.id = 'fork-' + i;
    fork.title = 'Tenedor ' + i;
    fork.style.left = (fx - 14) + 'px';
    fork.style.top  = (fy - 14) + 'px';
    // Rotate fork to point radially
    const rotDeg = (midAngle * 180 / Math.PI) + 90;
    fork.style.transform = `rotate(${rotDeg}deg)`;
    fork.dataset.baseRot = rotDeg;
    fork.innerHTML = '🍴';
    scene.appendChild(fork);
  }

  // Build stats chips
  buildStats(n, names);
}

function buildStats(n, names) {
  const bar = document.getElementById('stats-bar');
  bar.innerHTML = '';
  for (let i = 0; i < n; i++) {
    const chip = document.createElement('div');
    chip.className = 'stat-chip s-idle';
    chip.id = 'chip-' + i;
    chip.innerHTML = `<span class="stat-dot"></span>
      <span style="color:${PHIL_COLORS[i % PHIL_COLORS.length]}">${names[i]}</span>
      <span id="chip-lbl-${i}" style="color:var(--dim)">IDLE</span>`;
    bar.appendChild(chip);
  }
}

// ─────────────────────────────────────────────────────────────
//  STATE IMAGES MAP
// ─────────────────────────────────────────────────────────────
const IMG_MAP = {
  idle:     '/imagenes/pensando.png',
  thinking: '/imagenes/pensando.png',
  waiting:  '/imagenes/esperando.png',
  eating:   '/imagenes/comiendo.png',
  done:     '/imagenes/pensando.png',
};
const LBL_MAP = {
  idle:     'IDLE',
  thinking: 'PENSANDO',
  waiting:  'ESPERANDO',
  eating:   'COMIENDO',
  done:     'TERMINADO ✓',
};
const CARD_CLASS = {
  idle:     'g-thinking',
  thinking: 'g-thinking',
  waiting:  'g-waiting',
  eating:   'g-eating',
  done:     'g-done',
};
const CHIP_CLASS = {
  idle:     's-thinking',
  thinking: 's-thinking',
  waiting:  's-waiting',
  eating:   's-eating',
  done:     's-done',
};

// ─────────────────────────────────────────────────────────────
//  RENDER HELPERS
// ─────────────────────────────────────────────────────────────
function updatePhilosopher(i, state, count, name) {
  const imgEl  = document.getElementById('img-'  + i);
  const cardEl = document.getElementById('card-' + i);
  const lblEl  = document.getElementById('slbl-' + i);
  const eatsEl = document.getElementById('eats-' + i);
  const chipEl = document.getElementById('chip-' + i);
  const cLblEl = document.getElementById('chip-lbl-' + i);
  if (!imgEl) return;

  // Image & class
  imgEl.src       = IMG_MAP[state]  || IMG_MAP.idle;
  imgEl.className = 'phil-img s-' + state;

  // Label color
  const color = state === 'eating'  ? 'var(--gold2)'
              : state === 'waiting' ? 'var(--amber)'
              : state === 'done'    ? 'var(--dim)'
              : PHIL_COLORS[i % PHIL_COLORS.length];
  lblEl.textContent = LBL_MAP[state] || state.toUpperCase();
  lblEl.style.color = color;

  // Card class
  cardEl.className = 'phil-card ' + (CARD_CLASS[state] || 'g-thinking');
  if (state === 'eating') cardEl.classList.add('eating-anim');

  // Eat count
  if (eatsEl) eatsEl.innerHTML = `comidas: <b>${count}</b>`;

  // Stats chip
  if (chipEl) chipEl.className = 'stat-chip ' + (CHIP_CLASS[state] || 's-thinking');
  if (cLblEl) { cLblEl.textContent = LBL_MAP[state] || state; cLblEl.style.color = color; }
}

function updateForks(forkHeld, forkBy) {
  if (!forkHeld) return;
  for (let i = 0; i < forkHeld.length; i++) {
    const el = document.getElementById('fork-' + i);
    if (!el) continue;
    const baseRot = parseFloat(el.dataset.baseRot) || 0;
    if (forkHeld[i]) {
      el.className = 'fork-el held';
      el.style.transform = `rotate(${baseRot + 30}deg) scale(1.15)`;
      el.style.filter = `brightness(1.3) drop-shadow(0 0 8px var(--gold2))`;
    } else {
      el.className = 'fork-el free';
      el.style.transform = `rotate(${baseRot}deg)`;
      el.style.filter = `brightness(.45)`;
    }
  }
}

function applySnapshot(data) {
  if (!data) return;
  const n = data.n || N;
  if (data.states) {
    for (let i = 0; i < n; i++) {
      updatePhilosopher(i, data.states[i] || 'idle',
                           data.counts ? data.counts[i] : 0,
                           data.names  ? data.names[i]  : ALL_NAMES[i]);
    }
  }
  updateForks(data.fork_held, data.fork_by);
}

// ─────────────────────────────────────────────────────────────
//  LOG
// ─────────────────────────────────────────────────────────────
function addLog(msg, kind, ts) {
  const body = document.getElementById('log-body');
  const row  = document.createElement('div');
  row.className = 'log-row';
  const philIdx = parseInt(kind);
  const color   = isNaN(philIdx) ? 'var(--dim)' : PHIL_COLORS[philIdx % PHIL_COLORS.length];
  const cls     = isNaN(philIdx) ? 'c-system'   : '';
  row.innerHTML = `<span class="log-ts">${ts}</span><span class="${cls}" style="color:${cls?'':color}">${msg}</span>`;
  body.insertBefore(row, body.firstChild);
  if (body.children.length > 200) body.removeChild(body.lastChild);
}

// ─────────────────────────────────────────────────────────────
//  SOCKET.IO
// ─────────────────────────────────────────────────────────────
const socket = io();

socket.on('connect', () => {
  addLog('Conectado al servidor', 'system', new Date().toLocaleTimeString());
  buildScene(DEFAULT_N, ALL_NAMES.slice(0, DEFAULT_N));
});

socket.on('ready', data => {
  if (data.n) buildScene(data.n, data.names || ALL_NAMES.slice(0, data.n));
});

socket.on('simulation_start', data => {
  N = data.n;
  cycles = data.cycles;
  buildScene(N, data.names || ALL_NAMES.slice(0, N));
  applySnapshot(data);
  document.getElementById('btn-go').disabled    = true;
  document.getElementById('btn-go').textContent = '⟳ EN CURSO';
  document.getElementById('inp-n').disabled = true;
  document.getElementById('inp-c').disabled = true;
  document.getElementById('table-text').innerHTML = `
    ${N} FILÓSOFOS<br>${cycles} CICLOS`;
});

socket.on('state_update', data => {
  applySnapshot(data);
});

socket.on('fork_attempt', data => {
  applySnapshot(data);
});

socket.on('fork_taken', data => {
  applySnapshot(data);
  updateForks(data.fork_held, data.fork_by);
});

socket.on('eating_start', data => {
  applySnapshot(data);
});

socket.on('eating_end', data => {
  applySnapshot(data);
});

socket.on('log', data => addLog(data.msg, data.kind, data.ts));

socket.on('simulation_end', data => {
  document.getElementById('btn-go').textContent = '✓ COMPLETADO';

  // Build overlay rows
  const rows = document.getElementById('fin-rows');
  rows.innerHTML = '';
  const names = data.names || ALL_NAMES.slice(0, data.counts.length);
  data.counts.forEach((c, i) => {
    const bar = '█'.repeat(Math.round(c / data.cycles * 10)) +
                '░'.repeat(10 - Math.round(c / data.cycles * 10));
    const row = document.createElement('div');
    row.className = 'fin-row';
    row.innerHTML = `
      <span style="color:${PHIL_COLORS[i % PHIL_COLORS.length]}">${names[i]}</span>
      <span style="font-size:8px;color:var(--dim);font-family:var(--fm)">${bar}</span>
      <span class="fin-val">${c}</span>
    `;
    rows.appendChild(row);
  });

  setTimeout(() => document.getElementById('overlay').classList.add('show'), 1600);
});

// ─────────────────────────────────────────────────────────────
//  ACCIONES USUARIO
// ─────────────────────────────────────────────────────────────
function startSim() {
  const n = Math.max(2, Math.min(10, parseInt(document.getElementById('inp-n').value) || DEFAULT_N));
  const c = Math.max(1, Math.min(50, parseInt(document.getElementById('inp-c').value) || DEFAULT_CYC));
  socket.emit('start', {n, cycles: c});
}

function restartUI() {
  document.getElementById('overlay').classList.remove('show');
  const btn = document.getElementById('btn-go');
  btn.disabled = false; btn.textContent = '▶ INICIAR';
  document.getElementById('inp-n').disabled = false;
  document.getElementById('inp-c').disabled = false;
  document.getElementById('log-body').innerHTML = '';
  const n = parseInt(document.getElementById('inp-n').value) || DEFAULT_N;
  buildScene(n, ALL_NAMES.slice(0, n));
  document.getElementById('table-text').innerHTML = 'MESA<br>CIRCULAR';
}

// ajustar escena si cambia N antes de iniciar
document.getElementById('inp-n').addEventListener('change', () => {
  const n = Math.max(2, Math.min(10, parseInt(document.getElementById('inp-n').value) || DEFAULT_N));
  buildScene(n, ALL_NAMES.slice(0, n));
});
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
def main():
    n      = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_N
    cycles = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_CYCLES

    print(f"\n{'─'*54}")
    print(f"  Filósofos Comensales — SO — URL")
    print(f"{'─'*54}")
    print(f"  Filósofos  : {n}")
    print(f"  Ciclos     : {cycles}")
    print(f"  Tenedores  : {n}  (uno entre cada par)")
    print(f"  Deadlock   : ordenamiento asimétrico")
    print(f"  Servidor   : http://localhost:5000")
    print(f"  (El navegador se abrirá automáticamente)\n")

    img_dir = os.path.join(SCRIPT_DIR, 'imagenes')
    if not os.path.isdir(img_dir):
        print(f"[AVISO] No se encontró 'imagenes/' — las imágenes no se mostrarán.\n")

    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()

    socketio.run(app, host='0.0.0.0', port=5000,
                 debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()