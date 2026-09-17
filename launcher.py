"""
================================================================================
 LAUNCHER WIDGET ESCRITORIO - MPFM AUTODIAGNÓSTICO CONTINUO VOX-X4
 Archivo : launcher.py
 Autor   : Senior Automation Engineer
 Versión : 1.0.0
 Fecha   : 2026-09-14
================================================================================
 Descripción:
   Orquestador principal de escritorio nativo con PyWebView:
     1. Inicia el servidor Flask REST API (o verifica si ya está corriendo).
     2. Inicia el hilo daemon de adquisición y diagnóstico PLC (BackendAutodiagnostico).
     3. Abre la ventana nativa de PyWebView apuntando a la UI del widget.
     4. Expone API JS bidireccional (resize, close_app, minimize, get_status).
     5. Cierre sincronizado y limpio (graceful shutdown) de hilos y conexiones al salir.

 Uso:
   python launcher.py                   # Modo widget compacto (frameless)
   python launcher.py --expanded        # Abre directamente en vista completa
   python launcher.py --windowed        # Con marco estándar del sistema operativo
   python launcher.py --no-plc          # Solo UI + API (sin conexión PLC)
   python launcher.py --top             # Siempre encima de otras ventanas (Always-On-Top)
================================================================================
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import Optional

from werkzeug.serving import make_server
import webview

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.Formatter.converter = time.localtime   # hora local, no UTC
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-8s] [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler("launcher.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("Launcher")

# --------------------------------------------------------------------------- #
# Constantes de dimensiones
# --------------------------------------------------------------------------- #
COMPACT_WIDTH   = 420
COMPACT_HEIGHT  = 175
EXPANDED_WIDTH  = 920
EXPANDED_HEIGHT = 720
DEFAULT_HOST    = "127.0.0.1"
DEFAULT_PORT    = 5000


# Referencia global: el BackendAutodiagnostico se setea antes de que Flask
# reciba su primer request. FlaskServerThread.run() lo inyecta en app.config.
_global_backend = None

# =========================================================================== #
#  SERVIDOR FLASK EN SEGUNDO PLANO                                            #
# =========================================================================== #

class FlaskServerThread(threading.Thread):
    """
    Ejecuta el servidor WSGI Werkzeug en un hilo independiente para permitir
    apagado limpio mediante server.shutdown().
    """

    def __init__(self, host: str, port: int) -> None:
        super().__init__(name="FlaskServerThread", daemon=True)
        self.host = host
        self.port = port
        self._server = None
        self._is_running = threading.Event()

    def run(self) -> None:
        try:
            import app as flask_app_module
            # Inyectar backend antes de crear la app Flask para que
            # el primer request ya tenga BACKEND disponible en app.config
            if _global_backend is not None:
                flask_app_module.set_backend(_global_backend)
            self._flask_app = flask_app_module.create_app()
            self._server = make_server(self.host, self.port, self._flask_app, threaded=True)
            logger.info("Servidor Flask WSGI iniciado en http://%s:%d", self.host, self.port)
            self._is_running.set()
            self._server.serve_forever()
        except Exception as exc:
            logger.error("Error en servidor Flask: %s", exc, exc_info=True)
        finally:
            logger.info("Servidor Flask WSGI finalizado.")

    def shutdown(self) -> None:
        if self._server is not None:
            logger.info("Deteniendo servidor Flask WSGI...")
            self._server.shutdown()


def is_port_in_use(host: str, port: int, timeout_s: float = 0.5) -> bool:
    """Comprueba si el puerto ya está escuchando conexiones."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_s)
        return s.connect_ex((host, port)) == 0


def wait_for_api(url: str, timeout_s: float = 5.0) -> bool:
    """Espera a que el endpoint /health responda antes de abrir el navegador."""
    import urllib.request
    start = time.monotonic()
    health_url = f"{url.rstrip('/')}/health"
    while time.monotonic() - start < timeout_s:
        try:
            with urllib.request.urlopen(health_url, timeout=0.8) as resp:
                if resp.status == 200:
                    logger.info("API lista y respondiendo en %s (%.2fs)", health_url, time.monotonic() - start)
                    return True
        except Exception:
            time.sleep(0.15)
    logger.warning("Tiempo de espera agotado esperando a la API en %s", health_url)
    return False


# =========================================================================== #
#  PUENTE JAVASCRIPT <-> PYTHON (JS API)                                      #
# =========================================================================== #

class WidgetJsApi:
    """
    Métodos expuestos a la ventana PyWebView accesibles desde Vue.js mediante:
        window.pywebview.api.<metodo>(...)
    """

    def __init__(self) -> None:
        self._window: Optional[webview.Window] = None
        self._backend_ref = None

    def set_window(self, window: webview.Window) -> None:
        self._window = window

    def set_backend(self, backend) -> None:
        self._backend_ref = backend

    def resize(self, width: int, height: int) -> dict:
        """Redimensiona dinámicamente la ventana del widget."""
        try:
            if self._window:
                w, h = int(width), int(height)
                logger.info("JS API resize: %dx%d", w, h)
                self._window.resize(w, h)
                return {"ok": True, "width": w, "height": h}
        except Exception as exc:
            logger.error("Error en resize: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "No window"}

    def close_app(self) -> dict:
        """Cierra la ventana nativa y desencadena la parada limpia del sistema."""
        logger.info("JS API close_app invocado por el usuario.")
        try:
            if self._window:
                self._window.destroy()
            return {"ok": True}
        except Exception as exc:
            logger.error("Error al destruir ventana: %s", exc)
            return {"ok": False, "error": str(exc)}

    def minimize(self) -> dict:
        """Minimiza la ventana del widget a la barra de tareas."""
        try:
            if self._window:
                self._window.minimize()
                return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "No window"}

    def get_status(self) -> dict:
        """Retorna estado operacional del backend PLC para diagnóstico rápido."""
        stats = {}
        if self._backend_ref and hasattr(self._backend_ref, "stats"):
            stats = self._backend_ref.stats
        return {
            "ok": True,
            "backend_alive": self._backend_ref.is_alive if self._backend_ref else False,
            "stats": stats,
            "timestamp": time.time(),
        }


# =========================================================================== #
#  ORQUESTADOR PRINCIPAL (LAUNCHER)                                           #
# =========================================================================== #

class MPFMLauncher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.host = args.host
        self.port = args.port
        self.base_url = f"http://{self.host}:{self.port}"
        self.js_api = WidgetJsApi()
        self.flask_thread: Optional[FlaskServerThread] = None
        self.backend = None
        self.window: Optional[webview.Window] = None
        self._shutdown_lock = threading.Lock()
        self._is_shutting_down = False

    def start_flask(self) -> None:
        """Inicia Flask si no está corriendo externamente en el puerto configurado."""
        if is_port_in_use(self.host, self.port):
            logger.info("Puerto %d en uso. Se asume servidor Flask existente en %s.", self.port, self.base_url)
            return

        logger.info("Iniciando servidor Flask interno...")
        self.flask_thread = FlaskServerThread(self.host, self.port)
        self.flask_thread.start()

    def start_plc_backend(self) -> None:
        """Inicia el hilo daemon de lectura de tags PLC y autodiagnóstico continuo."""
        global _global_backend
        if self.args.no_plc:
            logger.info("Bandera --no-plc activa. Monitoreo PLC en segundo plano omitido.")
            return

        try:
            from backend_autodiagnostico import BackendAutodiagnostico
            logger.info("Instanciando e iniciando BackendAutodiagnostico...")
            self.backend = BackendAutodiagnostico()
            self.backend.start()
            self.js_api.set_backend(self.backend)

            # 1) Guardar en variable global para que FlaskServerThread la use
            #    si Flask aún no arrancó (caso normal: PLC inicia después de Flask)
            _global_backend = self.backend

            # 2) Si Flask ya está corriendo, inyectar directamente en app.set_backend()
            try:
                import app as flask_app_module
                flask_app_module.set_backend(self.backend)
                logger.info("BackendAutodiagnostico inyectado en Flask app vía set_backend().")
            except Exception as inject_exc:
                logger.warning("No se pudo inyectar BACKEND en Flask app: %s", inject_exc)

            logger.info("Hilo de adquisición PLC iniciado correctamente.")
        except Exception as exc:
            logger.error("No se pudo iniciar BackendAutodiagnostico: %s", exc, exc_info=True)

    def shutdown(self) -> None:
        """Apagado atómico y limpio de todos los componentes en ejecución."""
        with self._shutdown_lock:
            if self._is_shutting_down:
                return
            self._is_shutting_down = True

        logger.info("Iniciando parada limpia del sistema...")

        # 1. Detener bucle PLC
        if self.backend is not None:
            try:
                logger.info("Deteniendo bucle de adquisición PLC...")
                self.backend.stop(timeout_s=3.0)
            except Exception as exc:
                logger.error("Error deteniendo backend PLC: %s", exc)

        # 2. Detener servidor Flask interno
        if self.flask_thread is not None:
            try:
                self.flask_thread.shutdown()
            except Exception as exc:
                logger.error("Error deteniendo Flask: %s", exc)

        logger.info("Cierre completo y limpio finalizado. ¡Hasta luego!")

    def run(self) -> None:
        """Punto de entrada de ejecución del launcher."""
        # Configurar manejador de señales (Ctrl+C en consola)
        def _signal_handler(signum, frame):
            logger.info("Señal %d interceptada en proceso principal.", signum)
            if self.window:
                self.window.destroy()
            self.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        # 1. Iniciar servicios
        self.start_flask()
        self.start_plc_backend()

        # 2. Esperar a que la API responda
        wait_for_api(self.base_url, timeout_s=6.0)

        # 3. Determinar dimensiones y modo de ventana
        initial_width = EXPANDED_WIDTH if self.args.expanded else COMPACT_WIDTH
        initial_height = EXPANDED_HEIGHT if self.args.expanded else COMPACT_HEIGHT
        is_frameless = not self.args.windowed

        logger.info(
            "Creando ventana PyWebView [%dx%d | Frameless: %s | OnTop: %s] -> %s",
            initial_width, initial_height, is_frameless, self.args.top, self.base_url
        )

        # 4. Crear ventana PyWebView
        self.window = webview.create_window(
            title="MPFM Autodiagnóstico Continuo — VOX-X4",
            url=self.base_url,
            js_api=self.js_api,
            width=initial_width,
            height=initial_height,
            min_size=(380, 150),
            resizable=True,
            frameless=is_frameless,
            easy_drag=True,
            on_top=self.args.top,
            background_color="#07090E",
            text_select=False,
        )
        self.js_api.set_window(self.window)

        # 5. Suscribir evento de cierre de ventana
        def on_window_closed():
            logger.info("Ventana de PyWebView cerrada.")
            self.shutdown()

        self.window.events.closed += on_window_closed

        # 6. Iniciar bucle de eventos de la GUI (bloquea hasta cerrar la ventana)
        try:
            webview.start(debug=self.args.debug)
        finally:
            self.shutdown()


# =========================================================================== #
#  CLI ARGUMENTS & MAIN                                                       #
# =========================================================================== #

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launcher de escritorio nativo para el widget de Autodiagnóstico MPFM VOX-X4."
    )
    parser.add_argument(
        "--expanded", action="store_true",
        help="Inicia el widget directamente en la vista expandida (920x720)."
    )
    parser.add_argument(
        "--windowed", action="store_true",
        help="Usa un marco de ventana estándar del sistema en lugar de diseño frameless."
    )
    parser.add_argument(
        "--no-plc", action="store_true",
        help="No inicia el hilo de adquisición del PLC (útil para pruebas de UI/API)."
    )
    parser.add_argument(
        "--top", action="store_true",
        help="Mantiene la ventana siempre visible en primer plano (Always on Top)."
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Host para el servidor Flask (por defecto: {DEFAULT_HOST})."
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Puerto para el servidor Flask (por defecto: {DEFAULT_PORT})."
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Habilita herramientas de depuración (DevTools) en la ventana webview."
    )
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    launcher = MPFMLauncher(cli_args)
    launcher.run()
