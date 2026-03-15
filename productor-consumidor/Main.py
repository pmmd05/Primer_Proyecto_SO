#!/usr/bin/env python3
"""
=============================================================
  PROBLEMA PRODUCTOR-CONSUMIDOR
  Sistemas Operativos — Universidad Rafael Landívar
=============================================================
  Descripción:
    - 1 Productor lee números de un archivo .txt (uno por línea)
    - 3 Consumidores procesan números según su tipo:
        · Consumidor 1 → números PARES (no primos)
        · Consumidor 2 → números IMPARES (no primos)
        · Consumidor 3 → números PRIMOS (prioridad sobre impar/par)
    - Cada buffer tiene capacidad máxima BUFFER_SIZE
    - Sincronización mediante semáforos + mutex (threading)
=============================================================
"""

import threading
import time
import sys
import os

# ──────────────────────────────────────────────────────────────
#  CONFIGURACIÓN GLOBAL
# ──────────────────────────────────────────────────────────────
BUFFER_SIZE    = 10     # Capacidad máxima de cada buffer
PRODUCER_DELAY = 0.07   # Segundos entre producciones (simula trabajo del productor)
CONSUMER_DELAY = 0.12   # Segundos entre consumos    (simula trabajo del consumidor)

# Códigos ANSI para colores en consola
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

# ──────────────────────────────────────────────────────────────
#  BUFFERS COMPARTIDOS (uno por tipo de consumidor)
# ──────────────────────────────────────────────────────────────
even_buffer  = []   # números pares (no primos)
odd_buffer   = []   # números impares (no primos)
prime_buffer = []   # números primos

# Mutex: garantizan acceso exclusivo a cada buffer (sección crítica)
even_lock  = threading.Lock()
odd_lock   = threading.Lock()
prime_lock = threading.Lock()

# Semáforos de espacios LIBRES en cada buffer (inician en BUFFER_SIZE)
even_empty  = threading.Semaphore(BUFFER_SIZE)
odd_empty   = threading.Semaphore(BUFFER_SIZE)
prime_empty = threading.Semaphore(BUFFER_SIZE)

# Semáforos de elementos DISPONIBLES en cada buffer (inician en 0)
even_full  = threading.Semaphore(0)
odd_full   = threading.Semaphore(0)
prime_full = threading.Semaphore(0)

# Mutex global solo para imprimir (evita mezcla de líneas en consola)
print_lock = threading.Lock()

# ──────────────────────────────────────────────────────────────
#  UTILIDADES
# ──────────────────────────────────────────────────────────────
def is_prime(n: int) -> bool:
    """Verifica si un número es primo. 1 NO es primo."""
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    for i in range(3, int(n ** 0.5) + 1, 2):
        if n % i == 0:
            return False
    return True


def classify(n: int) -> str:
    """
    Clasifica un número con la siguiente prioridad:
      1. PRIMO  → 'prime'  (tiene prioridad sobre par e impar)
      2. PAR    → 'even'
      3. IMPAR  → 'odd'
    """
    if is_prime(n):
        return 'prime'
    elif n % 2 == 0:
        return 'even'
    else:
        return 'odd'


def log(msg: str):
    """Impresión thread-safe en consola."""
    with print_lock:
        print(msg, flush=True)


def buf_bar(current: int, maximum: int, width: int = 10) -> str:
    """Genera una barra visual del nivel del buffer."""
    filled = int((current / maximum) * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {current}/{maximum}"

# ──────────────────────────────────────────────────────────────
#  PRODUCTOR
# ──────────────────────────────────────────────────────────────
def producer(numbers: list):
    """
    Hilo productor: lee los números ya cargados, los clasifica
    e inserta en el buffer correspondiente.

    Sección crítica: el momento de hacer append al buffer.
    Se protege con: semáforo_empty → mutex → append → semáforo_full
    """
    log(f"\n{GREEN}{BOLD}[PRODUCTOR]{RESET} Iniciando. Total de números a producir: {len(numbers)}")

    for num in numbers:
        kind = classify(num)
        time.sleep(PRODUCER_DELAY)

        if kind == 'even':
            even_empty.acquire()                          # esperar espacio libre
            with even_lock:                               # ── SECCIÓN CRÍTICA ──
                even_buffer.append(num)
                size = len(even_buffer)
            even_full.release()                           # señalizar elemento listo
            log(f"{GREEN}[PRODUCTOR]{RESET}  Insertó {BOLD}{num:>6}{RESET} "
                f"(PAR)    → buffer_pares   {buf_bar(size, BUFFER_SIZE)}")

        elif kind == 'odd':
            odd_empty.acquire()
            with odd_lock:                                # ── SECCIÓN CRÍTICA ──
                odd_buffer.append(num)
                size = len(odd_buffer)
            odd_full.release()
            log(f"{GREEN}[PRODUCTOR]{RESET}  Insertó {BOLD}{num:>6}{RESET} "
                f"(IMPAR)  → buffer_impares {buf_bar(size, BUFFER_SIZE)}")

        else:  # prime
            prime_empty.acquire()
            with prime_lock:                              # ── SECCIÓN CRÍTICA ──
                prime_buffer.append(num)
                size = len(prime_buffer)
            prime_full.release()
            log(f"{GREEN}[PRODUCTOR]{RESET}  Insertó {BOLD}{num:>6}{RESET} "
                f"(PRIMO)  → buffer_primos  {buf_bar(size, BUFFER_SIZE)}")

    # ── Centinelas de fin (None) para señalar a cada consumidor que no hay más datos ──
    for empty_sem, lock, buf, full_sem in [
        (even_empty,  even_lock,  even_buffer,  even_full),
        (odd_empty,   odd_lock,   odd_buffer,   odd_full),
        (prime_empty, prime_lock, prime_buffer, prime_full),
    ]:
        empty_sem.acquire()
        with lock:
            buf.append(None)
        full_sem.release()

    log(f"{GREEN}{BOLD}[PRODUCTOR]{RESET} Terminó. Señales de fin enviadas a los 3 consumidores.")


# ──────────────────────────────────────────────────────────────
#  CONSUMIDORES
# ──────────────────────────────────────────────────────────────
def _consume(name: str, color: str, full_sem, lock, buf, empty_sem):
    """
    Lógica genérica de consumidor.

    Sección crítica: el momento de hacer pop del buffer.
    Se protege con: semáforo_full → mutex → pop → semáforo_empty
    """
    total = 0
    count = 0
    log(f"{color}[{name}]{RESET} En espera de números...")

    while True:
        full_sem.acquire()                               # esperar elemento disponible
        with lock:                                       # ── SECCIÓN CRÍTICA ──
            num = buf.pop(0)
        empty_sem.release()                              # liberar espacio en buffer

        if num is None:                                  # centinela de fin
            break

        time.sleep(CONSUMER_DELAY)
        total += num
        count += 1
        log(f"  {color}[{name}]{RESET}  Consumió {BOLD}{num:>6}{RESET} "
            f"| suma acumulada = {BOLD}{total}{RESET}")

    log(f"\n{color}{BOLD}[{name}]{RESET} "
        f"FIN — Consumidos: {count} números | {BOLD}Suma total = {total}{RESET}")
    return total


def consumer_even():
    _consume(
        name      = "CONSUMIDOR PARES  ",
        color     = CYAN,
        full_sem  = even_full,
        lock      = even_lock,
        buf       = even_buffer,
        empty_sem = even_empty,
    )


def consumer_odd():
    _consume(
        name      = "CONSUMIDOR IMPARES",
        color     = YELLOW,
        full_sem  = odd_full,
        lock      = odd_lock,
        buf       = odd_buffer,
        empty_sem = odd_empty,
    )


def consumer_prime():
    _consume(
        name      = "CONSUMIDOR PRIMOS ",
        color     = RED,
        full_sem  = prime_full,
        lock      = prime_lock,
        buf       = prime_buffer,
        empty_sem = prime_empty,
    )


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
def main():
    print(f"\n{'═'*62}")
    print(f"  {BOLD}PRODUCTOR-CONSUMIDOR — Sistemas Operativos — URL{RESET}")
    print(f"{'═'*62}")

    # ── Solicitar archivo ──────────────────────────────────────
    if len(sys.argv) > 1:
        filepath = sys.argv[1]
    else:
        filepath = input("\nIngrese la ruta del archivo .txt con los números: ").strip()

    if not os.path.exists(filepath):
        print(f"\n{RED}Error:{RESET} No se encontró el archivo '{filepath}'")
        sys.exit(1)

    # ── Leer y validar números del archivo ────────────────────
    numbers = []
    skipped = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                numbers.append(int(line))
            except ValueError:
                skipped.append((i, line))

    if skipped:
        print(f"\n{YELLOW}Advertencia:{RESET} Se omitieron {len(skipped)} línea(s) no numéricas:")
        for lineno, val in skipped:
            print(f"  Línea {lineno}: '{val}'")

    if not numbers:
        print(f"\n{RED}Error:{RESET} El archivo no contiene números válidos.")
        sys.exit(1)

    # ── Resumen previo ─────────────────────────────────────────
    evens  = [n for n in numbers if classify(n) == 'even']
    odds   = [n for n in numbers if classify(n) == 'odd']
    primes = [n for n in numbers if classify(n) == 'prime']

    print(f"\n  Archivo     : {filepath}")
    print(f"  Total nums  : {len(numbers)}")
    print(f"  → Pares     : {len(evens)}   {DIM}{evens}{RESET}")
    print(f"  → Impares   : {len(odds)}   {DIM}{odds}{RESET}")
    print(f"  → Primos    : {len(primes)}  {DIM}{primes}{RESET}")
    print(f"  Buffer size : {BUFFER_SIZE}")
    print(f"\n{'─'*62}")
    input("  Presione ENTER para iniciar la simulación...")
    print(f"{'─'*62}\n")

    # ── Crear y lanzar hilos ───────────────────────────────────
    threads = [
        threading.Thread(target=consumer_even,       name="Consumidor-Pares"),
        threading.Thread(target=consumer_odd,        name="Consumidor-Impares"),
        threading.Thread(target=consumer_prime,      name="Consumidor-Primos"),
        threading.Thread(target=producer, args=(numbers,), name="Productor"),
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    print(f"\n{'═'*62}")
    print(f"  {BOLD}Simulación completada.{RESET}")
    print(f"{'═'*62}\n")


if __name__ == "__main__":
    main()