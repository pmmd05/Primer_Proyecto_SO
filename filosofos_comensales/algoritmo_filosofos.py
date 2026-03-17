
"""
  FILÓSOFOS COMENSALES — Backend
  Servidor Flask + SocketIO con lógica de simulación.
  El frontend vive en templates/index.html.

    Modelo de concurrencia:
    - 1 hilo por filósofo.
    - 1 lock por tenedor.
    - Estado global protegido por locks para alimentar la UI.
        - Mitigación de interbloqueo global con orden asimétrico al tomar tenedores.
            (No implica garantía estricta de equidad entre filósofos.)
"""

import threading
import time
import random
import sys
import os
import webbrowser
from flask import Flask, render_template, send_from_directory
from flask_socketio import SocketIO

# Parámetros base de la simulación (ajustables según necesidad didáctica).
filosofos_predeterminados = 5
comidas_predeterminadas = 10
min_pensar = 0.8
max_pensar = 2.0
min_comer = 0.8
max_comer = 1.8
pausa_visual = 0.25   # pausa visual al intentar adquirir tenedor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(SCRIPT_DIR, 'templates', 'imagenes')

# Nombres mostrados en UI y logs (el frontend limita n a 10).
nombres = ["Platón", "Aristóteles", "Sócrates", "Confucio",
         "Descartes", "Kant", "Nietzsche", "Hume", "Locke", "Tales de Mileto"]


#  FLASK + SOCKETIO

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

# Estado compartido de la simulación.
_n_filosofos: int = filosofos_predeterminados
_comidas: int = comidas_predeterminadas
_sim_running: bool = False
_sim_lock = threading.Lock()

# Estado por filósofo (índice i => filósofo i).
_estado_filosofos: list = []
_conteo_comidas: list = []
_state_lock = threading.Lock()

# Estado observable de cada tenedor para representar en frontend.
_tenedor_sostenido: list = []   # bool: True si está tomado
_sostenido_por: list = []       # índice del filósofo que lo tiene, -1 si libre
_fork_lock = threading.Lock()

# Mutex real de cada tenedor: controla exclusión mutua física del recurso.
_forks: list = []

# Funciones auxiliares de estado.
def reset(num_filosofos: int, num_comidas: int) -> None:
    """Reinicia todas las estructuras para una nueva simulación."""
    global _estado_filosofos, _conteo_comidas, _tenedor_sostenido, _sostenido_por, _forks
    global _n_filosofos, _comidas

    # Guarda parámetros efectivos de la corrida actual.
    _n_filosofos = num_filosofos
    _comidas = num_comidas

    # Cada estructura se dimensiona a n para indexación directa por filósofo/tenedor.
    _estado_filosofos = ['idle'] * num_filosofos
    _conteo_comidas = [0] * num_filosofos
    _tenedor_sostenido = [False] * num_filosofos
    _sostenido_por = [-1] * num_filosofos

    # Se crea un lock independiente por tenedor.
    _forks = [threading.Lock() for _ in range(num_filosofos)]


def snapshot() -> dict:
    """Construye una instantánea del estado para enviarla al cliente.

    Nota: la copia se hace por bloques (_state_lock y _fork_lock), por lo que
    puede existir un desfase mínimo entre ambos grupos de datos.
    """
    # Se copian listas bajo lock para evitar referencias mutables compartidas.
    with _state_lock:
        estados = list(_estado_filosofos)
        conteo = list(_conteo_comidas)
    with _fork_lock:
        tenedor_sostenido = list(_tenedor_sostenido)
        sostenido_por   = list(_sostenido_por)

    # Se exponen llaves en español e inglés por compatibilidad con frontend.
    return {
        'states': estados,
        'estados': estados,
        'counts': conteo,
        'conteo': conteo,
        'fork_held': tenedor_sostenido,
        'tenedor_sostenido': tenedor_sostenido,
        'fork_by': sostenido_por,
        'sostenido_por': sostenido_por,
        'n': _n_filosofos,
        'cycles': _comidas,
        'comidas': _comidas,
        'names': nombres[:_n_filosofos],
        'nombres': nombres[:_n_filosofos],
    }


def set_phil_state(i: int, state: str) -> None:
    """Actualiza el estado visible del filósofo i."""
    with _state_lock:
        _estado_filosofos[i] = state


def set_fork(fork_idx: int, sostenido: bool, por: int = -1) -> None:
    """Sincroniza en memoria el estado visual de un tenedor."""
    with _fork_lock:
        _tenedor_sostenido[fork_idx] = sostenido
        _sostenido_por[fork_idx]   = por


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


# FILÓSOFO (hilo)
def philosopher(idx: int, n: int, cycles: int) -> None:
    """
    Lógica de un filósofo modelado como hilo.

        Mitigación de interbloqueo — ordenamiento asimétrico:
      El último filósofo (idx == n-1) adquiere los tenedores en orden
      derecho→izquierdo en lugar de izquierdo→derecho.
            Esto rompe la espera circular del esquema clásico con adquisición
            izquierda→derecha en todos los hilos.
    """
    # Convención circular clásica:
    # - tenedor izquierdo de i es i
    # - tenedor derecho de i es (i + 1) mod n
    name  = nombres[idx % len(nombres)]
    left  = idx
    right = (idx + 1) % n

    # Ordenamiento asimétrico para el último filósofo.
    # Todos menos el último toman izquierda->derecha; el último invierte.
    # Esta inversión rompe la condición de espera circular (deadlock).
    if idx == n - 1:
        first_fork, second_fork = right, left
    else:
        first_fork, second_fork = left, right

    set_phil_state(idx, 'thinking')
    log(f"{name} se sienta a la mesa (tenedores {left}↔{right})", str(idx))
    emit_ev('state_update')

    for cycle in range(1, cycles + 1):

        # Fase 1: pensar (sin recursos compartidos).
        set_phil_state(idx, 'thinking')
        log(f"{name} pensando... (ciclo {cycle}/{cycles})", str(idx))
        emit_ev('state_update')
        time.sleep(random.uniform(min_pensar, max_pensar))

        # Fase 2: intentar primer tenedor.
        # acquire() bloquea si otro hilo ya lo tiene.
        set_phil_state(idx, 'waiting')
        log(f"{name} intenta tomar tenedor {first_fork}", str(idx))
        emit_ev('fork_attempt', {'philosopher': idx, 'fork': first_fork})

        _forks[first_fork].acquire()

        # Se actualiza estado visual del tenedor una vez adquirido el lock real.
        set_fork(first_fork, True, idx)
        log(f"{name} tomó tenedor {first_fork}", str(idx))
        emit_ev('fork_taken', {'philosopher': idx, 'fork': first_fork})
        # Pausa didáctica para visualizar que posee un solo tenedor temporalmente.
        time.sleep(pausa_visual)

        # Fase 3: intentar segundo tenedor.
        log(f"{name} intenta tomar tenedor {second_fork}", str(idx))
        emit_ev('fork_attempt', {'philosopher': idx, 'fork': second_fork})

        _forks[second_fork].acquire()

        set_fork(second_fork, True, idx)
        log(f"{name} tomó tenedor {second_fork}", str(idx))
        emit_ev('fork_taken', {'philosopher': idx, 'fork': second_fork})

        # Fase 4: comer.
        # Conceptualmente esta es la sección crítica de alto nivel porque
        # el filósofo posee ambos recursos exclusivos a la vez.
        set_phil_state(idx, 'eating')
        eat_t = random.uniform(min_comer, max_comer)
        log(f"{name} COMIENDO con tenedores {first_fork} y {second_fork} ({eat_t:.2f}s)", str(idx))
        emit_ev('eating_start', {
            'philosopher': idx,
            'forks':       [first_fork, second_fork],
            'duration':    eat_t,
        })
        time.sleep(eat_t)

        with _state_lock:
            _conteo_comidas[idx] += 1
            conteo = _conteo_comidas[idx]

        # Fase 5: liberar tenedores en orden consistente con adquisición actual.
        # Liberar pronto reduce contención y mejora fluidez de la simulación.
        _forks[first_fork].release()
        set_fork(first_fork, False, -1)

        _forks[second_fork].release()
        set_fork(second_fork, False, -1)

        log(f"{name} soltó tenedores {first_fork} y {second_fork} (comidas: {conteo})", str(idx))
        emit_ev('eating_end', {
            'philosopher': idx,
            'forks':       [first_fork, second_fork],
            'conteo':       conteo,
        })

    # Estado final del hilo: no participa en más ciclos.
    set_phil_state(idx, 'done')
    with _state_lock:
        final = _conteo_comidas[idx]
    log(f"{name} terminó — comió {final} veces", str(idx))
    emit_ev('state_update')



# RUNNER DE SIMULACIÓN
def run_simulation(n: int, cycles: int) -> None:
    """Lanza todos los hilos de filósofos y espera a que terminen."""
    global _sim_running

    # Preparación de estado y señal de inicio para la UI.
    reset(n, cycles)
    socketio.emit('simulation_start', snapshot())
    log(f"════ SIMULACIÓN INICIADA — {n} filósofos, {cycles} ciclos ════", 'system')

    threads = [
        threading.Thread(
            target=philosopher,
            args=(i, n, cycles),
            name=nombres[i % len(nombres)],
            daemon=True,
        )
        for i in range(n)
    ]

    # Se arranca y luego se espera a todos para un cierre ordenado.
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with _sim_lock:
        # Libera bandera global para permitir una nueva ejecución desde UI.
        _sim_running = False

    with _state_lock:
        final_counts = list(_conteo_comidas)

    log("════ SIMULACIÓN COMPLETADA ════", 'system')
    socketio.emit('simulation_end', {
        'counts': final_counts,
        'names':  nombres[:n],
        'cycles': cycles,
    })


#  FLASK — RUTAS
@app.route('/')
def index():
    """Sirve la vista principal con valores por defecto de la simulación."""
    return render_template(
        'index.html',
        default_n      = filosofos_predeterminados,
        default_cycles = comidas_predeterminadas,
        names_js       = nombres,
    )


@app.route('/imagenes/<path:filename>')
def serve_image(filename):
    """Entrega recursos de imagen usados por la interfaz."""
    return send_from_directory(IMAGE_DIR, filename)

# Icono de la pestaña del navegador
@app.route('/favicon.ico')
def favicon():
    """Entrega un favicon simple reutilizando una imagen existente."""
    return send_from_directory(IMAGE_DIR, 'pensando.png', mimetype='image/png')


#  SOCKETIO — EVENTOS
@socketio.on('connect')
def on_connect():
    """Al conectar un cliente, envía snapshot inicial para hidratar la UI."""
    socketio.emit('ready', snapshot())


@socketio.on('start')
def on_start(data):
    """Inicia una simulación con parámetros sanitizados desde frontend."""
    global _sim_running
    with _sim_lock:
        # Evita lanzar dos simulaciones simultáneas.
        if _sim_running:
            return
        _sim_running = True

    # Validación defensiva de rangos para no saturar la simulación.
    num_filosofos = max(2, min(10, int(data.get('n',      filosofos_predeterminados))))
    num_comidas = max(1, min(50, int(data.get('cycles', comidas_predeterminadas))))

    # Runner en segundo plano para no bloquear el hilo principal de SocketIO.
    threading.Thread(
        target=run_simulation,
        args=(num_filosofos, num_comidas),
        daemon=True,
    ).start()



def main():
    """Punto de entrada de consola: parsea argumentos e inicia servidor."""
    # Argumentos opcionales para pruebas rápidas desde terminal.
    num_filosofos = int(sys.argv[1]) if len(sys.argv) > 1 else filosofos_predeterminados
    num_comidas = int(sys.argv[2]) if len(sys.argv) > 2 else comidas_predeterminadas

    print(f"\n{'─'*54}")
    print(f"  Filósofos Comensales — SO — URL")
    print(f"{'─'*54}")
    print(f"  Filósofos  : {num_filosofos}")
    print(f"  Ciclos     : {num_comidas}")
    print(f"  Tenedores  : {num_filosofos}  (uno entre cada par)")
    print(f"  Deadlock   : ordenamiento asimétrico")
    print(f"  Servidor   : http://localhost:5000")
    print(f"  (El navegador se abrirá automáticamente)\n")

    if not os.path.isdir(IMAGE_DIR):
        print(f"[AVISO] No se encontró 'templates/imagenes/' — las imágenes no se mostrarán.\n")

    # Abre navegador después de un breve delay para dar tiempo al arranque.
    threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5000')).start()
    socketio.run(app, host='0.0.0.0', port=5000,
                 debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()