"""
================================================================================
 api/alarmas.py — Blueprint: GET /api/alarmas
 Sistema de Autodiagnóstico MPFM VOX-X4
================================================================================
 GET /api/alarmas   → Últimas N alarmas de tb_historico_alarmas

 Query params:
   limit    (int, 1-200, default 50) — número de alarmas a retornar
   well_id  (str, opcional)          — filtrar por pozo
   severidad (str, opcional)         — INFO | WARNING | CRITICAL | FATAL
   capa     (str, opcional)          — CAPA_1 | CAPA_2 | CAPA_3 | SISTEMA
================================================================================
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
import threading
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request, Response

import db as DB
from config import (
    DEFAULT_WELL_ID,
    ALARMAS_DEFAULT_LIMIT,
    ALARMAS_MAX_LIMIT,
    CACHE_TTL_ALARMS,
)

logger = logging.getLogger("api.alarmas")
bp = Blueprint("alarmas", __name__)

# ---------------------------------------------------------------------------
# Caché en memoria (thread-safe) por clave de filtro
# ---------------------------------------------------------------------------
_cache_lock   = threading.Lock()
_alarm_cache: Dict[str, Dict[str, Any]] = {}


def _get_cached(key: str) -> Optional[Any]:
    with _cache_lock:
        entry = _alarm_cache.get(key)
        if entry and time.monotonic() < entry["expires_at"]:
            return entry["data"]
    return None


def _set_cached(key: str, data: Any) -> None:
    with _cache_lock:
        _alarm_cache[key] = {
            "data":       data,
            "expires_at": time.monotonic() + CACHE_TTL_ALARMS,
        }
        # Limpiar entradas expiradas (máx 50 claves)
        if len(_alarm_cache) > 50:
            now = time.monotonic()
            expired = [k for k, v in _alarm_cache.items() if v["expires_at"] < now]
            for k in expired:
                del _alarm_cache[k]


# ---------------------------------------------------------------------------
# SQL base de alarmas
# ---------------------------------------------------------------------------
_SQL_ALARMAS_BASE = """
SELECT
    a.id,
    a.well_id,
    a.telemetria_id,
    a.timestamp_evento_utc,
    a.severidad,
    a.capa_diagnostico,
    a.codigo_alarma,
    a.health_score_pct,
    a.status_label,
    a.total_checks,
    a.failed_checks,
    a.anomalias_json,
    a.p_line_snapshot_psig,
    a.t_proc_snapshot_f,
    a.q_liquid_snapshot_bpd,
    a.q_gas_snapshot_mmscfd,
    a.wc_snapshot_pct,
    a.app_version,
    a.row_hash_sha256
FROM tb_historico_alarmas a
WHERE 1=1
"""

SEVERIDADES_VALIDAS = {"INFO", "WARNING", "CRITICAL", "FATAL"}
CAPAS_VALIDAS       = {"CAPA_1", "CAPA_2", "CAPA_3", "SISTEMA"}


def _parse_anomalias(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parsea el campo anomalias_json (puede venir como str o list dependiendo
    de la versión de PyMySQL/MariaDB).
    """
    raw = row.get("anomalias_json")
    if isinstance(raw, str):
        try:
            row["anomalias_json"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            row["anomalias_json"] = [raw]
    elif raw is None:
        row["anomalias_json"] = []
    return row


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@bp.route("/alarmas", methods=["GET"])
def get_alarmas():
    """
    GET /api/alarmas

    Retorna las últimas N alarmas registradas en tb_historico_alarmas.

    Query params:
        limit     (int)    — número de registros (1-200, default=50)
        well_id   (str)    — código de pozo (default=FUL-01)
        severidad (str)    — filtro de severidad (INFO|WARNING|CRITICAL|FATAL)
        capa      (str)    — filtro de capa (CAPA_1|CAPA_2|CAPA_3|SISTEMA)

    Response 200:
        {
          "ok": true,
          "total": 12,
          "filtros": { "well_id": "FUL-01", "severidad": null, "capa": null },
          "alarmas": [
            {
              "id": 45,
              "timestamp_evento_utc": "2026-09-14T17:05:10.123",
              "severidad": "WARNING",
              "capa_diagnostico": "CAPA_1",
              "codigo_alarma": "ALM-SENSOR-001",
              "health_score_pct": 85.7,
              "status_label": "WARNING",
              "total_checks": 14,
              "failed_checks": 2,
              "anomalias_json": ["ALERTA: Outlier detectado en 'WaterCut_pct'..."],
              "p_line_snapshot_psig": 241.3,
              ...
            }
          ]
        }
    """
    # ── Parámetros de consulta ────────────────────────────────────────────
    try:
        limit = min(int(request.args.get("limit", ALARMAS_DEFAULT_LIMIT)), ALARMAS_MAX_LIMIT)
        limit = max(1, limit)
    except (TypeError, ValueError):
        limit = ALARMAS_DEFAULT_LIMIT

    well_id   = request.args.get("well_id",   DEFAULT_WELL_ID).strip().upper()
    severidad = request.args.get("severidad",  "").strip().upper() or None
    capa      = request.args.get("capa",       "").strip().upper() or None

    # Validar enums
    if severidad and severidad not in SEVERIDADES_VALIDAS:
        return jsonify({
            "ok": False,
            "error": f"Severidad inválida '{severidad}'. Válidas: {sorted(SEVERIDADES_VALIDAS)}"
        }), 400

    if capa and capa not in CAPAS_VALIDAS:
        return jsonify({
            "ok": False,
            "error": f"Capa inválida '{capa}'. Válidas: {sorted(CAPAS_VALIDAS)}"
        }), 400

    # ── Clave de caché ────────────────────────────────────────────────────
    cache_key = f"{well_id}:{limit}:{severidad}:{capa}"
    cached = _get_cached(cache_key)
    if cached is not None:
        resp = jsonify(cached)
        resp.headers["X-Cache"] = "HIT"
        return resp, 200

    # ── Construir SQL dinámico ────────────────────────────────────────────
    sql    = _SQL_ALARMAS_BASE
    params: list = []

    sql += " AND a.well_id = %s"
    params.append(well_id)

    if severidad:
        sql += " AND a.severidad = %s"
        params.append(severidad)

    if capa:
        sql += " AND a.capa_diagnostico = %s"
        params.append(capa)

    sql += " ORDER BY a.id DESC LIMIT %s"
    params.append(limit)

    # ── Consultar MySQL ───────────────────────────────────────────────────
    try:
        rows: List[Dict[str, Any]] = DB.query_all(sql, tuple(params))
    except Exception as exc:
        logger.error("Error consultando tb_historico_alarmas: %s", exc)
        return jsonify({"ok": False, "error": "Base de datos no disponible",
                        "detail": str(exc)}), 503

    # Parsear JSON embebido en el campo anomalias_json
    rows = [_parse_anomalias(r) for r in rows]

    payload = {
        "ok":     True,
        "total":  len(rows),
        "filtros": {
            "well_id":   well_id,
            "severidad": severidad,
            "capa":      capa,
            "limit":     limit,
        },
        "alarmas": rows,
    }

    _set_cached(cache_key, payload)

    resp = jsonify(payload)
    resp.headers["X-Cache"]       = "MISS"
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200

# ── CSV DOWNLOAD ────────────────────────────────────────────────────────
@bp.route("/alarmas/csv", methods=["GET"])
def get_alarmas_csv():
    """
    Descarga el histórico de alarmas en formato CSV.
    """
    well_id = request.args.get("well_id", DEFAULT_WELL_ID)
    limit   = request.args.get("limit", 1000, type=int)

    sql = _SQL_ALARMAS_BASE + " AND a.well_id = %s ORDER BY a.id DESC LIMIT %s"
    try:
        rows: List[Dict[str, Any]] = DB.query_all(sql, (well_id, limit))
    except Exception as exc:
        logger.error("Error consultando CSV de alarmas: %s", exc)
        return jsonify({"ok": False, "error": "Error de base de datos"}), 503

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([
        "ID", "Timestamp (UTC)", "Severidad", "Capa", "Código", 
        "Health Score (%)", "Check Count", "Failed Checks", "Anomalías (JSON)"
    ])
    
    for r in rows:
        cw.writerow([
            r.get("id", ""),
            r.get("timestamp_evento_utc", ""),
            r.get("severidad", ""),
            r.get("capa_diagnostico", ""),
            r.get("codigo_alarma", ""),
            r.get("health_score_pct", ""),
            r.get("total_checks", ""),
            r.get("failed_checks", ""),
            r.get("anomalias_json", "")
        ])
        
    output = si.getvalue()
    si.close()
    
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=auditoria_alarmas_{well_id}.csv"}
    )


# ---------------------------------------------------------------------------
# PURGA DE HISTÓRICO — DELETE /api/alarmas/purge
# ---------------------------------------------------------------------------
@bp.route("/alarmas/purge", methods=["DELETE"])
def purge_historico():
    """
    DELETE /api/alarmas/purge

    Elimina registros en un rango de fechas de tb_historico_alarmas
    y los registros correspondientes en tb_telemetria del mismo período.

    Body JSON requerido:
        {
          "fecha_inicio": "2026-09-01",   ← inclusive (YYYY-MM-DD)
          "fecha_fin":    "2026-09-15",   ← inclusive (YYYY-MM-DD)
          "well_id":      "FUL-01",       ← opcional, default config
          "confirmar":    true            ← DEBE ser true para ejecutar
        }
    """
    import datetime as _dt

    data = request.get_json(silent=True) or {}

    # ── Validar confirmación explícita ─────────────────────────────────────
    if data.get("confirmar") is not True:
        return jsonify({
            "ok": False,
            "error": "Debe enviar 'confirmar': true en el body para ejecutar la purga."
        }), 400

    # ── Fechas ─────────────────────────────────────────────────────────────
    fecha_inicio = data.get("fecha_inicio", "").strip()
    fecha_fin    = data.get("fecha_fin",    "").strip()

    if not fecha_inicio or not fecha_fin:
        return jsonify({
            "ok": False,
            "error": "Los campos 'fecha_inicio' y 'fecha_fin' son requeridos (YYYY-MM-DD)."
        }), 400

    try:
        dt_inicio = _dt.datetime.strptime(fecha_inicio, "%Y-%m-%d")
        dt_fin    = _dt.datetime.strptime(fecha_fin, "%Y-%m-%d").replace(
                        hour=23, minute=59, second=59, microsecond=999000)
    except ValueError:
        return jsonify({
            "ok": False,
            "error": "Formato de fecha inválido. Use YYYY-MM-DD."
        }), 400

    if dt_inicio > dt_fin:
        return jsonify({
            "ok": False,
            "error": "'fecha_inicio' debe ser anterior o igual a 'fecha_fin'."
        }), 400

    well_id = data.get("well_id", DEFAULT_WELL_ID).strip().upper()

    # ── Ejecutar purga ─────────────────────────────────────────────────────
    try:
        with DB.get_conn() as conn:
            with conn.cursor() as cur:
                # 1) Eliminar alarmas en el rango
                cur.execute(
                    """
                    DELETE FROM tb_historico_alarmas
                    WHERE well_id = %s
                      AND timestamp_evento_utc BETWEEN %s AND %s
                    """,
                    (well_id, dt_inicio, dt_fin)
                )
                del_alarmas = cur.rowcount

                # 2) Eliminar telemetría en el mismo rango que ya no tenga alarmas vinculadas
                cur.execute(
                    """
                    DELETE FROM tb_telemetria
                    WHERE well_id = %s
                      AND timestamp_normal BETWEEN %s AND %s
                      AND id NOT IN (
                            SELECT COALESCE(telemetria_id, 0)
                            FROM tb_historico_alarmas
                            WHERE telemetria_id IS NOT NULL
                      )
                    """,
                    (well_id, dt_inicio, dt_fin)
                )
                del_telemetria = cur.rowcount

            conn.commit()

    except Exception as exc:
        logger.error("Error en purga de histórico: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 503

    # Invalidar caché de alarmas
    with _cache_lock:
        _alarm_cache.clear()

    logger.warning(
        "PURGA EJECUTADA | well=%s | rango=%s → %s | alarmas=%d | telemetría=%d",
        well_id, fecha_inicio, fecha_fin, del_alarmas, del_telemetria
    )

    return jsonify({
        "ok":                    True,
        "eliminadas_alarmas":    del_alarmas,
        "eliminadas_telemetria": del_telemetria,
        "rango": {
            "desde":   fecha_inicio,
            "hasta":   fecha_fin,
            "well_id": well_id,
        }
    }), 200
