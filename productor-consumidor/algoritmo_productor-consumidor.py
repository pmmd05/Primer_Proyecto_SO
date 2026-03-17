"""Backend de la simulación Productor-Consumidor.

Este módulo expone una aplicación Flask + SocketIO que coordina:
- un productor que inserta enteros en un buffer FIFO compartido,
- tres consumidores (pares, impares y primos) que extraen solo cuando
    el frente del buffer corresponde a su tipo.

La sincronización se implementa con ``threading.Condition`` para evitar
carreras en el acceso al buffer y para señalizar cambios de estado.
"""

import threading
import time
import os
import webbrowser
from flask import Flask, render_template, send_from_directory, request, jsonify
from flask_socketio import SocketIO

# Configuración de la simulación.
BUFFER_SIZE    = 10    # Capacidad máxima del buffer compartido
PRODUCER_DELAY = 0.50  # Pausa entre producciones
CONSUMER_DELAY = 0.70  # Pausa al procesar un número consumido
CRITICAL_HOLD  = 0.35  # Pausa visible dentro de la sección crítica

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(SCRIPT_DIR, 'templates', 'imagenes')


# Inicialización de Flask + SocketIO.
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

def is_prime(n: int) -> bool:
    """Retorna ``True`` si ``n`` es primo."""
    if n < 2:      return False
    if n == 2:     return True
    if n % 2 == 0: return False
    for i in range(3, int(n ** 0.5) + 1, 2):
        if n % i == 0: return False
    return True


def classify(n: int) -> str:
    """Clasifica ``n`` como ``prime``, ``even`` u ``odd``.

    Regla de prioridad: primo > par > impar.
    """
    if is_prime(n):  return 'prime'
    if n % 2 == 0:   return 'even'
    return 'odd'


# Estado compartido de la simulación.
_numbers: list = []  # Números cargados desde el archivo.
_filepath: str = ''  # Nombre del archivo cargado.

# Buffer compartido y condición para sincronización.
_buffer: list = []
condition = threading.Condition()
_prod_done: bool = False

# Estado de cada actor para la UI.
_states: dict = {}
_states_lock = threading.Lock()

# Actor actualmente en sección crítica (None = nadie).
_critical_actor: str = None
_critical_lock = threading.Lock()

# Estadísticas de consumidores.
_sums: dict = {'even': 0, 'odd': 0, 'prime': 0}
_counts: dict = {'even': 0, 'odd': 0, 'prime': 0}
_stats_lock = threading.Lock()

# Control de ejecución de simulación.
_sim_running: bool = False
_sim_lock = threading.Lock()


def reset() -> None:
    """Reinicia estado compartido para iniciar una nueva simulación."""
    global _buffer, _prod_done, _states, _critical_actor, _sums, _counts

    # Restablece estructuras compartidas para evitar arrastrar datos previos.
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
    """Actualiza el estado visible de un actor para la interfaz."""
    with _states_lock:
        _states[actor] = state


def set_critical(actor) -> None:
    """Marca qué actor está en sección crítica; ``None`` libera la marca."""
    global _critical_actor
    with _critical_lock:
        _critical_actor = actor


def snapshot() -> dict:
    """Retorna una copia consistente del estado para emitir a clientes."""
    # Se adquieren locks por separado y se copian estructuras para no exponer
    # referencias mutables a otros hilos ni al frontend.
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
    """Emite un evento SocketIO con el snapshot actual y datos extra."""
    data = snapshot()
    if extra:
        data.update(extra)
    socketio.emit(event, data)


def log(msg: str, kind: str = 'system') -> None:
    """Emite una entrada de log con timestamp para el frontend."""
    ts = time.strftime('%H:%M:%S')
    socketio.emit('log', {'msg': msg, 'kind': kind, 'ts': ts})


def producer() -> None:
    """
        Inserta números en el buffer FIFO desde ``_numbers``.

        Comportamiento de sincronización:
        - Si el buffer está lleno, el productor pasa a estado ``blocked`` y espera.
        - La inserción se realiza dentro de la sección crítica protegida por
            ``condition``.
        - Tras insertar, notifica a los consumidores con ``notify_all``.
    """
    global _prod_done

    set_state('producer', 'producing')
    log(f"PRODUCTOR iniciando — {len(_numbers)} números en cola", 'producer')
    emit_event('state_update')

    # Recorre los números cargados y los publica uno por uno en el buffer.
    for i, num in enumerate(_numbers):
        kind  = classify(num)
        label = {'even': 'PAR', 'odd': 'IMPAR', 'prime': 'PRIMO'}[kind]
        time.sleep(PRODUCER_DELAY)

        with condition:
            # Si el buffer está lleno, el productor cede el lock y espera señal.
            if len(_buffer) >= BUFFER_SIZE:
                set_state('producer', 'blocked')
                log(f"PRODUCTOR bloqueado — buffer lleno ({len(_buffer)}/{BUFFER_SIZE})", 'blocked')
                emit_event('state_update')
                while len(_buffer) >= BUFFER_SIZE:
                    # wait() libera el lock de condition y lo recupera al despertar.
                    condition.wait()

            # Sección crítica: inserción FIFO al final del buffer.
            set_state('producer', 'critical')
            set_critical('producer')
            emit_event('critical_enter', {'actor': 'producer', 'num': num, 'kind': kind})

            time.sleep(CRITICAL_HOLD)
            _buffer.append(num)
            buf_size = len(_buffer)
            buf_snap = list(_buffer)

            set_critical(None)
            # Se despiertan todos porque cualquier consumidor podría poder tomar.
            condition.notify_all()

        set_state('producer', 'producing')
        log(f"PRODUCTOR insertó {num} ({label}) → buffer [{buf_size}/{BUFFER_SIZE}]", 'producer')
        emit_event('insert', {
            'num':    num,
            'kind':   kind,
            'idx':    i + 1,
            'total':  len(_numbers),
            'buffer': buf_snap,
        })

    # Señal de fin: ya no habrá nuevas inserciones.
    with condition:
        _prod_done = True
        # Importante para que los consumidores puedan evaluar condición de salida.
        condition.notify_all()

    set_state('producer', 'done')
    log("PRODUCTOR terminado — señal de fin enviada", 'producer')
    emit_event('state_update')


def consumer(kind: str) -> None:
    """
    Consume números del frente si coinciden con el tipo ``kind``.

    Estados de espera:
    - ``blocked`` cuando el buffer está vacío.
    - ``waiting`` cuando hay frente, pero no corresponde a su tipo.

    Termina cuando el productor ya finalizó y no quedan números de su tipo en
    el buffer.
    """
    actor_key = f'consumer_{kind}'
    label_map = {'even': 'PARES', 'odd': 'IMPARES', 'prime': 'PRIMOS'}
    name      = f"CONSUMIDOR {label_map[kind]}"

    set_state(actor_key, 'blocked')
    log(f"{name} bloqueado — buffer vacío", 'blocked')
    emit_event('state_update')

    while True:
        with condition:

            while True:
                # Puede consumir solo si el frente coincide con su tipo.
                if _buffer and classify(_buffer[0]) == kind:
                    break

                # Finaliza cuando su tipo ya no podrá aparecer.
                if _prod_done and not any(classify(n) == kind for n in _buffer):
                    set_state(actor_key, 'done')
                    log(f"{name} recibió señal de fin", kind)
                    emit_event('state_update')
                    with _stats_lock:
                        s = _sums[kind]
                        c = _counts[kind]
                    log(f"{name} FIN — {c} números procesados | suma total = {s}", kind)
                    return

                # Diferencia explícita de causa de espera para la UI.
                if not _buffer:
                    if _states.get(actor_key) != 'blocked':
                        set_state(actor_key, 'blocked')
                        log(f"{name} bloqueado — buffer vacío", 'blocked')
                        emit_event('state_update')
                else:
                    if _states.get(actor_key) != 'waiting':
                        set_state(actor_key, 'waiting')
                        log(f"{name} en espera — frente no corresponde ({_buffer[0]})", kind)
                        emit_event('state_update')
                # El hilo cede el lock y espera hasta nuevo cambio en el buffer.
                condition.wait()

            # Sección crítica: extracción FIFO desde el frente.
            set_state(actor_key, 'critical')
            set_critical(actor_key)
            emit_event('critical_enter', {'actor': actor_key, 'kind': kind})

            time.sleep(CRITICAL_HOLD)
            num      = _buffer.pop(0)
            buf_size = len(_buffer)
            buf_snap = list(_buffer)

            set_critical(None)
            # Puede liberar al productor (espacio disponible) o a otro consumidor.
            condition.notify_all()

        # El procesamiento ocurre fuera de la sección crítica para no retener el lock.
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

def run_simulation() -> None:
    """Ejecuta una corrida completa y emite los resultados finales."""
    global _sim_running

    # Se limpia estado y se notifica inicio antes de lanzar hilos.
    reset()
    socketio.emit('simulation_start', snapshot())
    log("════ SIMULACIÓN INICIADA ════", 'system')

    threads = [
        threading.Thread(target=consumer, args=('even',),  name='C-Pares',   daemon=True),
        threading.Thread(target=consumer, args=('odd',),   name='C-Impares', daemon=True),
        threading.Thread(target=consumer, args=('prime',), name='C-Primos',  daemon=True),
        threading.Thread(target=producer,                  name='Productor', daemon=True),
    ]
    # Los hilos daemon terminan con el proceso; join asegura cierre ordenado.
    for t in threads: t.start()
    for t in threads: t.join()

    with _sim_lock:
        _sim_running = False

    with _stats_lock:
        final_sums   = dict(_sums)
        final_counts = dict(_counts)

    log("════ SIMULACIÓN COMPLETADA ════", 'system')
    socketio.emit('simulation_end', {'sums': final_sums, 'counts': final_counts})


@app.route('/')
def index():
    """Renderiza la interfaz principal de la simulación."""
    return render_template('index.html', buffer_size=BUFFER_SIZE)


@app.route('/upload', methods=['POST'])
def upload_file():
    """
    Carga números desde un archivo de texto enviado por el frontend.

    El archivo debe contener un entero por línea. Retorna métricas para
    actualizar la interfaz y habilitar la ejecución.
    """
    global _numbers, _filepath

    f = request.files.get('file')
    if not f:
        return jsonify({'success': False, 'error': 'No se recibió ningún archivo.'})

    # Lectura y parseo defensivo: líneas vacías se ignoran.
    try:
        content = f.read().decode('utf-8')
    except UnicodeDecodeError:
        return jsonify({'success': False, 'error': 'El archivo debe estar en UTF-8.'})

    nums, skipped = [], []
    # Se conserva conteo de líneas inválidas para reportarlo al usuario.
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

    # Estos conteos son métricas para la UI (no afectan la sincronización).
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


@socketio.on('connect')
def on_connect():
    """Envía al cliente recién conectado el estado actual del servidor."""
    socketio.emit('ready', {
        'filepath':    _filepath,
        'total':       len(_numbers),
        'file_loaded': len(_numbers) > 0,
        **snapshot(),
    })


@socketio.on('start')
def on_start():
    """Inicia la simulación si hay datos cargados y no hay otra en curso."""
    global _sim_running
    if not _numbers:
        socketio.emit('error', {'msg': 'Carga un archivo antes de iniciar.'})
        return
    with _sim_lock:
        # Guardia de concurrencia para evitar ejecuciones solapadas.
        if _sim_running:
            return
        _sim_running = True
    # La simulación corre en segundo plano para no bloquear el hilo de SocketIO.
    threading.Thread(target=run_simulation, daemon=True).start()


def main():
    """Punto de entrada: muestra resumen de arranque y levanta el servidor."""
    print(f"\n{'─'*54}")
    print(f"  Productor-Consumidor — SO — URL")
    print(f"{'─'*54}")
    print(f"  Buffer    : {BUFFER_SIZE} slots")
    print(f"  Consumid. : 3 (pares, impares, primos)")
    print(f"  Servidor  : http://localhost:5000")
    print(f"  Carga tu archivo .txt desde el navegador.\n")

    if not os.path.isdir(IMAGE_DIR):
        print(f"[AVISO] No se encontró 'templates/imagenes/' — las imágenes de estado no se mostrarán.\n")

    # Abre navegador automáticamente con leve retraso para dar tiempo al servidor.
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