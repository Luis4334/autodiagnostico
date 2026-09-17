"""
================================================================================
 config.py — Configuración centralizada de la API REST
 Sistema de Autodiagnóstico MPFM VOX-X4
================================================================================
"""
import os

# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host":    os.getenv("MPFM_DB_HOST",   "127.0.0.1"),
    "port":    int(os.getenv("MPFM_DB_PORT", 3306)),
    "user":    os.getenv("MPFM_DB_USER",   "root"),
    "passwd":  os.getenv("MPFM_DB_PASS",   ""),
    "db":      os.getenv("MPFM_DB_NAME",   "db_autodiagnostico"),
    "charset": "utf8mb4",
    "connect_timeout": 5,
    "autocommit": True,
}

# Pool de conexiones (DBUtils PooledDB)
DB_POOL_CONFIG = {
    "mincached":  2,     # conexiones abiertas al inicio
    "maxcached":  8,     # conexiones inactivas en caché
    "maxshared":  4,     # conexiones compartidas entre hilos
    "maxconnections": 12, # límite total
    "blocking":   True,  # espera si el pool está lleno
    "ping":       1,     # verificar conexión antes de usar
}

# ---------------------------------------------------------------------------
# Pozo activo por defecto
# ---------------------------------------------------------------------------
DEFAULT_WELL_ID = os.getenv("MPFM_WELL_ID", "FUL-01")

# ---------------------------------------------------------------------------
# CORS — Orígenes permitidos para el frontend Vue.js
# ---------------------------------------------------------------------------
CORS_ORIGINS = [
    "http://localhost:5173",   # Vite dev server
    "http://localhost:3000",   # alternativa
    "http://localhost:8080",   # Vue CLI
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8080",
    "http://localhost",        # XAMPP htdocs producción
    "http://127.0.0.1",
]

# ---------------------------------------------------------------------------
# Caché en memoria (segundos de TTL por endpoint)
# ---------------------------------------------------------------------------
CACHE_TTL_LIVE   = 0.8   # /api/monitoreo/live   → refrescado cada 2s por el backend
CACHE_TTL_ALARMS = 2.0   # /api/alarmas
CACHE_TTL_CONFIG = 10.0  # /api/configuracion

# ---------------------------------------------------------------------------
# Paginación de alarmas
# ---------------------------------------------------------------------------
ALARMAS_DEFAULT_LIMIT = 50
ALARMAS_MAX_LIMIT     = 200

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
class FlaskConfig:
    SECRET_KEY  = os.getenv("FLASK_SECRET", "mpfm-vox-x4-secret-2026")
    JSON_SORT_KEYS     = False   # preservar orden de campos en respuesta
    JSONIFY_PRETTYPRINT_REGULAR = False
    MAX_CONTENT_LENGTH = 64 * 1024  # 64 KB máximo en POST
