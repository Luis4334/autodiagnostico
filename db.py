"""
================================================================================
 db.py — Pool de conexiones PyMySQL con DBUtils
 Sistema de Autodiagnóstico MPFM VOX-X4
================================================================================
 Provee:
   get_conn()  → conexión del pool (usar con context manager 'with')
   query_one() → ejecuta SELECT y devuelve un dict o None
   query_all() → ejecuta SELECT y devuelve lista de dicts
   execute()   → ejecuta INSERT / UPDATE y devuelve lastrowid
================================================================================
"""
from __future__ import annotations

import decimal
import datetime
import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pymysql
import pymysql.cursors

try:
    from dbutils.pooled_db import PooledDB
    _DBUTILS_AVAILABLE = True
except ImportError:
    _DBUTILS_AVAILABLE = False

from config import DB_CONFIG, DB_POOL_CONFIG

logger = logging.getLogger("api.db")

# ---------------------------------------------------------------------------
# Pool singleton
# ---------------------------------------------------------------------------
_pool: Optional[Any] = None


def _create_pool() -> Any:
    """Inicializa el pool de conexiones PyMySQL con DBUtils."""
    cfg = {**DB_CONFIG, "cursorclass": pymysql.cursors.DictCursor}
    if _DBUTILS_AVAILABLE:
        pool = PooledDB(
            creator=pymysql,
            **DB_POOL_CONFIG,
            **cfg,
        )
        logger.info("Pool DBUtils inicializado (min=%d, max=%d).",
                    DB_POOL_CONFIG["mincached"], DB_POOL_CONFIG["maxconnections"])
        return pool
    # Fallback: sin pool (dev/test sin DBUtils instalado)
    logger.warning("DBUtils no disponible — usando conexiones directas (sin pool).")
    return None


def get_pool() -> Any:
    """Retorna el pool singleton, creándolo si no existe."""
    global _pool
    if _pool is None:
        _pool = _create_pool()
    return _pool


def _direct_connection() -> pymysql.connections.Connection:
    """Crea una conexión directa (fallback si no hay pool)."""
    cfg = {**DB_CONFIG, "cursorclass": pymysql.cursors.DictCursor}
    return pymysql.connect(**cfg)


@contextmanager
def get_conn():
    """
    Context manager que provee una conexión del pool.

    Uso:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    pool = get_pool()
    conn = pool.connection() if pool else _direct_connection()
    try:
        yield conn
    except pymysql.err.OperationalError:
        # Reconectar si la conexión del pool expiró
        try:
            conn.ping(reconnect=True)
        except Exception:
            pass
        raise
    finally:
        conn.close()  # devuelve al pool, no cierra físicamente


# ---------------------------------------------------------------------------
# Serialización de tipos MySQL → JSON-safe
# ---------------------------------------------------------------------------
def _serialize(value: Any) -> Any:
    """Convierte tipos no-JSON-nativos de PyMySQL a tipos Python primitivos."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _serialize(v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Helpers de consulta
# ---------------------------------------------------------------------------
def query_one(sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
    """Ejecuta una consulta y retorna la primera fila como dict o None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return _serialize_row(row) if row else None


def query_all(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    """Ejecuta una consulta y retorna todas las filas como lista de dicts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
            return [_serialize_row(r) for r in rows]


def execute(sql: str, args: tuple = ()) -> int:
    """Ejecuta INSERT/UPDATE y retorna lastrowid o rowcount."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            conn.commit()
            return cur.lastrowid or cur.rowcount
