#!/usr/bin/env python3
"""
=============================================================
  FILÓSOFOS COMENSALES — Backend
  Sistemas Operativos — Universidad Rafael Landívar
=============================================================
  Servidor Flask + SocketIO con lógica de simulación.
  El frontend vive en templates/index.html.

  Uso:
      python backend.py          (5 filósofos, 10 ciclos)
      python backend.py 5 12     (N filósofos, ciclos)
=============================================================
"""

import threading
import time
import random
import sys
import os
import webbrowser
from flask import Flask, render_template, send_from_directory
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
IMAGE_DIR = os.path.join(SCRIPT_DIR, 'templates', 'imagenes')

NAMES = ["Platón", "Aristóteles", "Sócrates", "Confucio",
         "Descartes", "Kant", "Nietzsche", "Hume", "Locke", "Spinoza"]

# ──────────────────────────────────────────────────────────────
#  FLASK + SOCKETIO
# ──────────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(SCRIPT_DIR, 'templates'),
)
app.config['SECRET_KEY'] = 'filosofos-url-2025'
socketio = SocketIO(
    app,
    async_mode='threading',
    cors_allowed_origins='*',
    logger=False,
    engineio_logger=False,
)

# ──────────────────────────────────────────────────────────────
#  ESTADO COMPARTIDO
# ──────────────────────────────────────────────────────────────
_n_philosophers: int  = DEFAULT_N
_cycles:         int  = DEFAULT_CYCLES
_sim_running:    bool = False
_sim_lock             = threading.Lock()

# Estado de cada filósofo: 'idle' | 'thinking' | 'waiting' | 'eating' | 'done'
_phil_states: list = []
_eat_counts:  list = []
_state_lock        = threading.Lock()

# Estado de cada tenedor
_fork_held: list = []   # bool — True si está tomado
_fork_by:   list = []   # índice del filósofo que lo tiene, -1 si libre
_fork_lock       = threading.Lock()

# Mutex de cada tenedor (primitiva de sincronización real)
_forks: list = []   # lista de threading.Lock()

# ──────────────────────────────────────────────────────────────
#  GESTIÓN DE ESTADO
# ──────────────────────────────────────────────────────────────
def reset(n: int, cycles: int) -> None:
    """Reinicia todas las estructuras para una nueva simulación."""
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
    """Captura atómica del estado completo para enviar al cliente."""
    with _state_lock:
        states = list(_phil_states)
        counts = list(_eat_counts)
    with _fork_lock:
        fheld = list(_fork_held)
        fby   = list(_fork_by)
    return {
        'states':    states,
        'counts':    counts,
        'fork_held': fheld,
        'fork_by':   fby,
        'n':         _n_philosophers,
        'cycles':    _cycles,
        'names':     NAMES[:_n_philosophers],
    }


def set_phil_state(i: int, state: str) -> None:
    with _state_lock:
        _phil_states[i] = state


def set_fork(fork_idx: int, held: bool, by: int = -1) -> None:
    with _fork_lock:
        _fork_held[fork_idx] = held
        _fork_by[fork_idx]   = by


def emit_ev(event: str, extra: dict = None) -> None:
    """Emite un evento con el snapshot actual más datos opcionales."""
    data = snapshot()
    if extra:
        data.update(extra)
    socketio.emit(event, data)


def log(msg: str, kind: str = 'system') -> None:
    """Emite una entrada de log al cliente."""
    ts = time.strftime('%H:%M:%S')
    socketio.emit('log', {'msg': msg, 'kind': kind, 'ts': ts})


# ──────────────────────────────────────────────────────────────
#  FILÓSOFO (hilo)
# ──────────────────────────────────────────────────────────────
def philosopher(idx: int, n: int, cycles: int) -> None:
    """
    Lógica de un filósofo modelado como hilo.

    Prevención de deadlock — ordenamiento asimétrico:
      El último filósofo (idx == n-1) adquiere los tenedores en orden
      derecho→izquierdo en lugar de izquierdo→derecho.
      Esto rompe la espera circular: nunca todos los filósofos pueden
      quedar bloqueados esperando su segundo tenedor simultáneamente.
    """
    name  = NAMES[idx % len(NAMES)]
    left  = idx
    right = (idx + 1) % n

    # Ordenamiento asimétrico para el último filósofo
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

        _forks[first_fork].acquire()           # bloquea si el tenedor está ocupado

        set_fork(first_fork, True, idx)
        log(f"{name} tomó tenedor {first_fork}", str(idx))
        emit_ev('fork_taken', {'philosopher': idx, 'fork': first_fork})
        time.sleep(ACQUIRE_PAUSE)

        # ── ESPERANDO segundo tenedor ──────────────────────────
        log(f"{name} intenta tomar tenedor {second_fork}", str(idx))
        emit_ev('fork_attempt', {'philosopher': idx, 'fork': second_fork})

        _forks[second_fork].acquire()          # bloquea si el tenedor está ocupado

        set_fork(second_fork, True, idx)
        log(f"{name} tomó tenedor {second_fork}", str(idx))
        emit_ev('fork_taken', {'philosopher': idx, 'fork': second_fork})

        # ── COMIENDO (sección crítica) ─────────────────────────
        set_phil_state(idx, 'eating')
        eat_t = random.uniform(EAT_MIN, EAT_MAX)
        log(f"{name} COMIENDO con tenedores {first_fork} y {second_fork} ({eat_t:.2f}s)", str(idx))
        emit_ev('eating_start', {
            'philosopher': idx,
            'forks':       [first_fork, second_fork],
            'duration':    eat_t,
        })
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
        emit_ev('eating_end', {
            'philosopher': idx,
            'forks':       [first_fork, second_fork],
            'count':       count,
        })

    # ── TERMINADO ─────────────────────────────────────────────
    set_phil_state(idx, 'done')
    with _state_lock:
        final = _eat_counts[idx]
    log(f"{name} terminó — comió {final} veces", str(idx))
    emit_ev('state_update')


# ──────────────────────────────────────────────────────────────
#  RUNNER DE SIMULACIÓN
# ──────────────────────────────────────────────────────────────
def run_simulation(n: int, cycles: int) -> None:
    """Lanza todos los hilos de filósofos y espera a que terminen."""
    global _sim_running

    reset(n, cycles)
    socketio.emit('simulation_start', snapshot())
    log(f"════ SIMULACIÓN INICIADA — {n} filósofos, {cycles} ciclos ════", 'system')

    threads = [
        threading.Thread(
            target=philosopher,
            args=(i, n, cycles),
            name=NAMES[i % len(NAMES)],
            daemon=True,
        )
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

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
#  FLASK — RUTAS
# ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template(
        'index.html',
        default_n      = DEFAULT_N,
        default_cycles = DEFAULT_CYCLES,
        names_js       = NAMES,
    )


@app.route('/imagenes/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(IMAGE_DIR, 'pensando.png', mimetype='image/png')


# ──────────────────────────────────────────────────────────────
#  SOCKETIO — EVENTOS
# ──────────────────────────────────────────────────────────────
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
    threading.Thread(
        target=run_simulation,
        args=(n, cycles),
        daemon=True,
    ).start()


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

    if not os.path.isdir(IMAGE_DIR):
        print(f"[AVISO] No se encontró 'templates/imagenes/' — las imágenes no se mostrarán.\n")

    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()
    socketio.run(app, host='0.0.0.0', port=5000,
                 debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()