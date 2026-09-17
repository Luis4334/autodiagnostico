"""
================================================================================
 app.py — Flask Application Factory
 Sistema de Autodiagnóstico Continuo MPFM VOX-X4
================================================================================
 Monta:
   • CORS (flask-cors) configurado para polling 1 Hz desde Vue.js
   • Blueprints: /api/monitoreo, /api/configuracion, /api/alarmas
   • Health check: GET /health
   • Manejadores de error globales (400, 404, 405, 500)
   • Logging estructurado

 Modo de ejecución:
   python app.py                      ← desarrollo (auto-reload)
   gunicorn -w 4 -b 0.0.0.0:5000 "app:create_app()"  ← producción
================================================================================
"""
from __future__ import annotations

import logging
import os
import sys
import time

from flask import Flask, jsonify, g, send_from_directory
from flask_cors import CORS

from config import FlaskConfig, CORS_ORIGINS
from api import register_blueprints

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.Formatter.converter = time.localtime   # hora local, no UTC
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("api_server.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("app")

# ---------------------------------------------------------------------------
# Referencia global al BackendAutodiagnostico
# Seteada por launcher.py antes de que lleguen requests.
# ---------------------------------------------------------------------------
_BACKEND = None

def set_backend(backend) -> None:
    """Inyecta la instancia de BackendAutodiagnostico para que las rutas la lean."""
    global _BACKEND
    _BACKEND = backend
    logger.info("BackendAutodiagnostico registrado vía set_backend().")


# ---------------------------------------------------------------------------
# Application Factory
# ---------------------------------------------------------------------------
def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(FlaskConfig)

    # Inyectar backend PLC si ya fue registrado vía set_backend()
    if _BACKEND is not None:
        app.config["BACKEND"] = _BACKEND
        logger.info("BACKEND inyectado en app.config desde _BACKEND global.")

    # ── CORS ────────────────────────────────────────────────────────────────
    # Configurado para soportar polling a 1 Hz desde el frontend Vue.js.
    # Permite Authorization header para futuras rutas protegidas.
    CORS(
        app,
        origins=CORS_ORIGINS,
        supports_credentials=True,
        methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        expose_headers=["X-Cache", "X-Response-Time-ms"],
        max_age=600,   # pre-flight cacheado 10 min para no saturar en polling
    )

    # ── Blueprints ───────────────────────────────────────────────────────────
    register_blueprints(app)

    # ── Medición de latencia por request ─────────────────────────────────────
    @app.before_request
    def _start_timer():
        g.t_start = time.monotonic()

    @app.after_request
    def _add_timing_header(response):
        if hasattr(g, "t_start"):
            elapsed_ms = (time.monotonic() - g.t_start) * 1000
            response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.2f}"
        # Seguridad básica
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "SAMEORIGIN"
        return response

    # ── Frontend Widget HTML ──────────────────────────────────────────────────
    @app.route("/", methods=["GET"])
    def index():
        """Sirve el widget de monitoreo autodiagnóstico continuo."""
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        return send_from_directory(static_dir, "index.html")

    # ── Health Check ─────────────────────────────────────────────────────────
    @app.route("/health", methods=["GET"])
    def health():
        """
        GET /health

        Verifica que la API y la base de datos están disponibles.
        Usado por el frontend para detectar si el servidor está vivo.

        Response 200: { "status": "ok", "db": "ok",   "uptime_s": 123.4 }
        Response 503: { "status": "ok", "db": "error", "detail": "..." }
        """
        import db as DB
        _start = time.monotonic()
        try:
            result = DB.query_one("SELECT 1 AS ping", ())
            db_status = "ok" if result else "empty"
        except Exception as exc:
            return jsonify({
                "status": "ok",
                "db":     "error",
                "detail": str(exc),
            }), 503

        return jsonify({
            "status":           "ok",
            "db":               db_status,
            "db_latency_ms":    round((time.monotonic() - _start) * 1000, 2),
            "version":          "1.0.0",
        }), 200

    # ── Manejadores de error globales ────────────────────────────────────────
    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"ok": False, "error": "Solicitud inválida.",
                        "detail": str(e)}), 400

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"ok": False,
                        "error": f"Endpoint no encontrado: {e}"}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"ok": False,
                        "error": "Método HTTP no permitido en este endpoint."}), 405

    @app.errorhandler(500)
    def internal_error(e):
        logger.exception("Error interno no controlado: %s", e)
        return jsonify({"ok": False,
                        "error": "Error interno del servidor.",
                        "detail": str(e)}), 500

    logger.info("Flask app creada. Blueprints registrados. CORS habilitado para: %s",
                CORS_ORIGINS)
    return app


# ---------------------------------------------------------------------------
# Punto de entrada para desarrollo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    app = create_app()
    # use_reloader=False cuando no hay consola (pythonw) para evitar crash silencioso
    IS_INTERACTIVE = sys.stdout is not None and hasattr(sys.stdout, 'fileno')
    try:
        IS_INTERACTIVE = sys.stdout.fileno() >= 0
    except Exception:
        IS_INTERACTIVE = False

    logger.info("Iniciando servidor de desarrollo Flask en http://0.0.0.0:5000")
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=IS_INTERACTIVE,
        use_reloader=False,   # desactivado: el reloader necesita consola interactiva
        threaded=True,        # cada request en su propio hilo → compatible con polling 1 Hz
    )
