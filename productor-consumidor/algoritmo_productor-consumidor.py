#!/usr/bin/env python3
"""
=============================================================
  PRODUCTOR-CONSUMIDOR — Backend
  Sistemas Operativos — Universidad Rafael Landívar
=============================================================
  Servidor Flask + SocketIO con lógica de simulación.
  El frontend vive en templates/index.html.

  Los números se cargan desde el navegador mediante /upload.
  No se requiere argumento de línea de comandos.

  Uso:
      python backend.py
=============================================================
"""

import threading
import time
import os
import webbrowser
from flask import Flask, render_template, send_from_directory, request, jsonify
from flask_socketio import SocketIO

# CONFIGURACIÓN 
BUFFER_SIZE    = 10    # Capacidad máxima del buffer compartido
PRODUCER_DELAY = 0.50  # Pausa entre producciones
CONSUMER_DELAY = 0.70  # Pausa al procesar un número consumido
CRITICAL_HOLD  = 0.35  # Pausa visible dentro de la sección crítica

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(SCRIPT_DIR, 'templates', 'imagenes')


# FLASK + SOCKETIO
app = Flask(
    __name__,
    template_folder=os.path.join(SCRIPT_DIR, 'templates'),
)
app.config['SECRET_KEY'] = 'so-url-2025'
socketio = SocketIO(
    app,
    async_mode='threading',
    cors_allowed_origins='*',
    logger=False,
    engineio_logger=False,
)

#  FUNCIONES AUXILIARES PARA CLASIFICACIÓN
def is_prime(n: int) -> bool:
    """Retorna True si n es primo."""
    if n < 2:      return False
    if n == 2:     return True
    if n % 2 == 0: return False
    for i in range(3, int(n ** 0.5) + 1, 2):
        if n % i == 0: return False
    return True


def classify(n: int) -> str:
    """
    Clasifica un número según prioridad: primo > par > impar.
    Un número primo (aunque sea impar) se clasifica como 'prime'.
    """
    if is_prime(n):  return 'prime'
    if n % 2 == 0:   return 'even'
    return 'odd'


# ESTADO COMPARTIDO DE LA SIMULACIÓN
_numbers: list = [] # Números cargados desde el archivo
_filepath: str = '' # Nombre del archivo cargado

# Buffer compartido y variable de condición (lock + señalización)
_buffer: list = []
condition = threading.Condition()
_prod_done: bool = False

# Estado de cada actor para la UI 
_states: dict = {}
_states_lock = threading.Lock()

# Actor actualmente en sección crítica (None = nadie)
_critical_actor: str = None
_critical_lock = threading.Lock()

# Estadísticas de consumidores
_sums: dict = {'even': 0, 'odd': 0, 'prime': 0}
_counts: dict = {'even': 0, 'odd': 0, 'prime': 0}
_stats_lock = threading.Lock()

# Control de ejecución
_sim_running: bool = False
_sim_lock = threading.Lock()


# GESTIÓN DE ESTADO
def reset() -> None:
    """Reinicia el estado de la simulación para permitir una nueva ejecución."""
    global _buffer, _prod_done, _states, _critical_actor, _sums, _counts

    _buffer         = []
    _prod_done      = False
    _critical_actor = None
    _states = {
        'producer': 'idle',
        'consumer_even': 'idle',
        'consumer_odd': 'idle',
        'consumer_prime': 'idle',
    }
    _sums   = {'even': 0, 'odd': 0, 'prime': 0}
    _counts = {'even': 0, 'odd': 0, 'prime': 0}


def set_state(actor: str, state: str) -> None:
    with _states_lock:
        _states[actor] = state


def set_critical(actor) -> None:
    """Establece qué actor está en sección crítica (None para liberar)."""
    global _critical_actor
    with _critical_lock:
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
        'buffer':   list(_buffer),
        'sums':     su,
        'counts':   co,
        'states':   st,
        'critical': ca,
    }


def emit_event(event: str, extra: dict = None) -> None:
    data = snapshot()
    if extra:
        data.update(extra)
    socketio.emit(event, data)


def log(msg: str, kind: str = 'system') -> None:
    ts = time.strftime('%H:%M:%S')
    socketio.emit('log', {'msg': msg, 'kind': kind, 'ts': ts})


#  PRODUCTOR
def producer() -> None:
    """
    Lee los números del archivo ya cargado y los inserta al FINAL del buffer (FIFO).
    Usa condition.wait() cuando el buffer está lleno (productor bloqueado).

    Sección crítica: bloque 'with condition'.
    Solo un hilo puede ejecutar código dentro de ese bloque a la vez.
    """
    global _prod_done

    set_state('producer', 'producing')
    log(f"PRODUCTOR iniciando — {len(_numbers)} números en cola", 'producer')
    emit_event('state_update')

    for i, num in enumerate(_numbers):
        kind  = classify(num)
        label = {'even': 'PAR', 'odd': 'IMPAR', 'prime': 'PRIMO'}[kind]
        time.sleep(PRODUCER_DELAY)

        with condition: # ADQUIERE LOCK

            # Esperar si el buffer está lleno, productor bloqueado
            if len(_buffer) >= BUFFER_SIZE:
                set_state('producer', 'blocked')
                log(f"PRODUCTOR bloqueado — buffer lleno ({len(_buffer)}/{BUFFER_SIZE})", 'blocked')
                emit_event('state_update')
                while len(_buffer) >= BUFFER_SIZE:
                    condition.wait() # libera lock, espera notify

            # SECCIÓN CRÍTICA: insertar en buffer
            set_state('producer', 'critical')
            set_critical('producer')
            emit_event('critical_enter', {'actor': 'producer', 'num': num, 'kind': kind})

            time.sleep(CRITICAL_HOLD) # pausa visual en SC
            _buffer.append(num) # insertar al final (FIFO)
            buf_size = len(_buffer)
            buf_snap = list(_buffer)

            set_critical(None)
            condition.notify_all()                      # despertar consumidores
        # LIBERA LOCK

        set_state('producer', 'producing')
        log(f"PRODUCTOR insertó {num} ({label}) → buffer [{buf_size}/{BUFFER_SIZE}]", 'producer')
        emit_event('insert', {
            'num':    num,
            'kind':   kind,
            'idx':    i + 1,
            'total':  len(_numbers),
            'buffer': buf_snap,
        })

    # Señal de fin: todos los números fueron producidos
    with condition:
        _prod_done = True
        condition.notify_all()

    set_state('producer', 'done')
    log("PRODUCTOR terminado — señal de fin enviada", 'producer')
    emit_event('state_update')


# ──────────────────────────────────────────────────────────────
#  CONSUMIDOR (genérico, parametrizado por tipo)
# ──────────────────────────────────────────────────────────────
def consumer(kind: str) -> None:
    """
    Extrae números del FRENTE del buffer solo si corresponden a su tipo (FIFO estricto).
    Espera con condition.wait() cuando el buffer está vacío o el frente no es su tipo.

    Terminación:
        Cuando _prod_done=True y ya no quedan números de su tipo en el buffer.
        No hay deadlock: si un tipo está bloqueado, los otros tipos van consumiendo
        sus elementos, liberando espacio y despertando al productor.
    """
    actor_key = f'consumer_{kind}'
    label_map = {'even': 'PARES', 'odd': 'IMPARES', 'prime': 'PRIMOS'}
    name      = f"CONSUMIDOR {label_map[kind]}"

    set_state(actor_key, 'waiting')
    log(f"{name} en espera de números...", kind)
    emit_event('state_update')

    while True:
        with condition:                                 # ADQUIERE LOCK

            while True:
                # ¿El frente del buffer es mi tipo? → puedo tomar
                if _buffer and classify(_buffer[0]) == kind:
                    break

                # ¿El productor terminó y ya no hay elementos de mi tipo?
                if _prod_done and not any(classify(n) == kind for n in _buffer):
                    set_state(actor_key, 'done')
                    log(f"{name} recibió señal de fin", kind)
                    emit_event('state_update')
                    with _stats_lock:
                        s = _sums[kind]
                        c = _counts[kind]
                    log(f"{name} FIN — {c} números procesados | suma total = {s}", kind)
                    return

                # Seguir esperando
                if _states.get(actor_key) != 'waiting':
                    set_state(actor_key, 'waiting')
                    emit_event('state_update')
                condition.wait()                        # libera lock, espera notify

            # SECCIÓN CRÍTICA: extraer del frente (FIFO)
            set_state(actor_key, 'critical')
            set_critical(actor_key)
            emit_event('critical_enter', {'actor': actor_key, 'kind': kind})

            time.sleep(CRITICAL_HOLD)                   # pausa visual en SC
            num      = _buffer.pop(0)                   # extraer frente
            buf_size = len(_buffer)
            buf_snap = list(_buffer)

            set_critical(None)
            condition.notify_all()                      # despertar productor
        # LIBERA LOCK 

        # Procesar número FUERA de la sección crítica
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

# SIMULACIÓN
def run_simulation() -> None:
    """Lanza el productor y los 3 consumidores, espera a que todos terminen."""
    global _sim_running

    reset()
    socketio.emit('simulation_start', snapshot())
    log("════ SIMULACIÓN INICIADA ════", 'system')

    threads = [
        threading.Thread(target=consumer, args=('even',),  name='C-Pares',   daemon=True),
        threading.Thread(target=consumer, args=('odd',),   name='C-Impares', daemon=True),
        threading.Thread(target=consumer, args=('prime',), name='C-Primos',  daemon=True),
        threading.Thread(target=producer,                  name='Productor', daemon=True),
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
    """Sirve el frontend. Los datos del archivo se cargan vía /upload."""
    return render_template('index.html', buffer_size=BUFFER_SIZE)


@app.route('/upload', methods=['POST'])
def upload_file():
    """
    Recibe un archivo .txt del navegador, lo parsea y carga los números.
    Retorna estadísticas en JSON para que el frontend actualice la UI.
    """
    global _numbers, _filepath

    f = request.files.get('file')
    if not f:
        return jsonify({'success': False, 'error': 'No se recibió ningún archivo.'})

    # Leer y parsear línea por línea
    try:
        content = f.read().decode('utf-8')
    except UnicodeDecodeError:
        return jsonify({'success': False, 'error': 'El archivo debe estar en UTF-8.'})

    nums, skipped = [], []
    for i, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            nums.append(int(line))
        except ValueError:
            skipped.append((i, line))

    if not nums:
        return jsonify({'success': False, 'error': 'No se encontraron números válidos en el archivo.'})

    _numbers  = nums
    _filepath = f.filename or 'archivo.txt'

    evens  = [n for n in nums if classify(n) == 'even']
    odds   = [n for n in nums if classify(n) == 'odd']
    primes = [n for n in nums if classify(n) == 'prime']

    return jsonify({
        'success':  True,
        'filepath': _filepath,
        'total':    len(nums),
        'n_even':   len(evens),
        'n_odd':    len(odds),
        'n_prime':  len(primes),
        'skipped':  len(skipped),
    })


@app.route('/imagenes/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)


# ──────────────────────────────────────────────────────────────
#  SOCKETIO — EVENTOS
# ──────────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    socketio.emit('ready', {
        'filepath':    _filepath,
        'total':       len(_numbers),
        'file_loaded': len(_numbers) > 0,
        **snapshot(),
    })


@socketio.on('start')
def on_start():
    global _sim_running
    if not _numbers:
        socketio.emit('error', {'msg': 'Carga un archivo antes de iniciar.'})
        return
    with _sim_lock:
        if _sim_running:
            return
        _sim_running = True
    threading.Thread(target=run_simulation, daemon=True).start()


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
def main():
    print(f"\n{'─'*54}")
    print(f"  Productor-Consumidor — SO — URL")
    print(f"{'─'*54}")
    print(f"  Buffer    : {BUFFER_SIZE} slots")
    print(f"  Consumid. : 3 (pares, impares, primos)")
    print(f"  Servidor  : http://localhost:5000")
    print(f"  Carga tu archivo .txt desde el navegador.\n")

    if not os.path.isdir(IMAGE_DIR):
        print(f"[AVISO] No se encontró 'templates/imagenes/' — las imágenes de estado no se mostrarán.\n")

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