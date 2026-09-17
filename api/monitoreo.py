"""
================================================================================
 api/monitoreo.py — Blueprint: GET /api/monitoreo/live
 Sistema de Autodiagnóstico MPFM VOX-X4
================================================================================
 Retorna el último registro de tb_telemetria con:
   • Variables de campo:  P, T, DP_cuña, DP_laminar, WC, Viscosidad, GVF, Nivel
   • Health Score y estado (HEALTHY / WARNING / CRITICAL)
   • Timestamp del último ciclo de adquisición
   • Snapshot de caudales (Q_liq, Q_crudo, Q_agua, Q_gas)

 Caché en memoria (TTL=0.8s) para soportar polling a 1 Hz desde Vue.js
 sin golpear MySQL en cada petición del frontend.
================================================================================
"""
from __future__ import annotations

import time
import threading
import logging
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, current_app

import db as DB
from config import DEFAULT_WELL_ID, CACHE_TTL_LIVE

logger = logging.getLogger("api.monitoreo")
bp = Blueprint("monitoreo", __name__)

# ---------------------------------------------------------------------------
# Caché en memoria (thread-safe) para /live
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "data":       None,
    "expires_at": 0.0,
}

# ---------------------------------------------------------------------------
# SQL: último registro de tb_telemetria
# ---------------------------------------------------------------------------
_SQL_LIVE = """
SELECT
    -- Metadatos del ciclo
    t.id                        AS ciclo_id,
    t.timestamp_normal          AS timestamp_utc,
    t.well_id,
    t.plc_ip,

    -- Estado del medidor
    t.health_score_pct          AS health_score,
    t.status_label              AS status,

    -- Presiones
    t.p_gas_psig                AS presion_gas_psig,
    t.p_oil_psig                AS presion_aceite_psig,
    t.presion_entrada_psig      AS presion_entrada_psig,
    t.pdt_01_psi                AS dp_pdt01_psi,
    t.pdt_03_psi                AS dp_pdt03_psi,

    -- Temperaturas
    t.t_oil_f                   AS temperatura_aceite_f,
    t.t_oil_c                   AS temperatura_aceite_c,
    t.t_gas_c                   AS temperatura_gas_c,

    -- Diferenciales de presión (medidores de flujo)
    t.dp_w_inh2o                AS dp_cunia_inH2O,
    t.dp_l_inh2o                AS dp_laminar_inH2O,
    t.dp_simeflum_inh2o         AS dp_simeflum_inH2O,

    -- Corte de agua y viscosidad
    t.wc_pct                    AS corte_agua_pct,
    t.v_oil_medida_cp           AS viscosidad_medida_cp,
    t.miu_oil_cp                AS viscosidad_calculada_cp,

    -- Gas Void Fraction (SONAR)
    t.gvoidf_pct                AS gvf_sonar_pct,

    -- Nivel del separador
    t.lit_001_pct               AS nivel_separador_pct,
    t.level_pid_cv_pct          AS lcv_posicion_pct,
    t.press_pid_cv_pct          AS pcv_posicion_pct,

    -- Caudales medidos
    t.q_liquido_bpd             AS caudal_liquido_bpd,
    t.q_crudo_bpd               AS caudal_crudo_bpd,
    t.q_w_bpd                   AS caudal_agua_bpd,
    t.q_gas_std                 AS caudal_gas_std,

    -- Coriolis
    t.coriolis_density_kgm3     AS densidad_coriolis_kgm3,
    t.coriolis_vol_flow_m3h     AS caudal_coriolis_m3h,

    -- Flags de estado del PLC
    t.numero_prueba,
    t.prueba_estatus,
    t.deshabilita_pid,
    t.status_error_caudal,
    t.vi_sw                     AS viscosimetro_activo

FROM tb_telemetria t
WHERE t.well_id = %s
ORDER BY t.id DESC
LIMIT 1
"""


def _build_live_response(row: Dict[str, Any]) -> Dict[str, Any]:
    """Estructura la respuesta JSON del endpoint /live."""
    return {
        "ok":        True,
        "ciclo_id":  row.get("ciclo_id"),
        "timestamp": row.get("timestamp_utc"),
        "well_id":   row.get("well_id"),
        "plc_ip":    row.get("plc_ip"),

        # ── Estado global del medidor ──────────────────────────────────
        "diagnostico": {
            "health_score": row.get("health_score"),
            "status":       row.get("status"),
        },

        # ── Presiones ──────────────────────────────────────────────────
        "presiones": {
            "gas_psig":       row.get("presion_gas_psig"),
            "aceite_psig":    row.get("presion_aceite_psig"),
            "entrada_psig":   row.get("presion_entrada_psig"),
            "dp_pdt01_psi":   row.get("dp_pdt01_psi"),
            "dp_pdt03_psi":   row.get("dp_pdt03_psi"),
        },

        # ── Temperaturas ───────────────────────────────────────────────
        "temperaturas": {
            "aceite_f":  row.get("temperatura_aceite_f"),
            "aceite_c":  row.get("temperatura_aceite_c"),
            "gas_c":     row.get("temperatura_gas_c"),
        },

        # ── Diferenciales de presión (medidores de flujo) ──────────────
        "dp_medidores": {
            "cunia_inH2O":    row.get("dp_cunia_inH2O"),
            "laminar_inH2O":  row.get("dp_laminar_inH2O"),
            "simeflum_inH2O": row.get("dp_simeflum_inH2O"),
        },

        # ── Análisis de fluido ─────────────────────────────────────────
        "fluido": {
            "corte_agua_pct":         row.get("corte_agua_pct"),
            "viscosidad_medida_cp":   row.get("viscosidad_medida_cp"),
            "viscosidad_calculada_cp":row.get("viscosidad_calculada_cp"),
            "gvf_sonar_pct":          row.get("gvf_sonar_pct"),
            "densidad_coriolis_kgm3": row.get("densidad_coriolis_kgm3"),
        },

        # ── Nivel y control de válvulas ────────────────────────────────
        "control": {
            "nivel_separador_pct": row.get("nivel_separador_pct"),
            "lcv_posicion_pct":    row.get("lcv_posicion_pct"),
            "pcv_posicion_pct":    row.get("pcv_posicion_pct"),
        },

        # ── Caudales ───────────────────────────────────────────────────
        "caudales": {
            "gas_std":      row.get("caudal_gas_std"),
            "liquido_bpd":  row.get("caudal_liquido_bpd"),
            "crudo_bpd":    row.get("caudal_crudo_bpd"),
            "agua_bpd":     row.get("caudal_agua_bpd"),
        },

        # ── Estado del PLC ─────────────────────────────────────────────
        "plc": {
            "numero_prueba":       row.get("numero_prueba"),
            "prueba_activa":       bool(row.get("prueba_estatus")),
            "pid_deshabilitado":   bool(row.get("deshabilita_pid")),
            "error_caudal":        bool(row.get("status_error_caudal")),
            "viscosimetro_activo": bool(row.get("viscosimetro_activo")),
        },
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@bp.route("/live", methods=["GET"])
def live():
    """
    GET /api/monitoreo/live

    Retorna el último registro de telemetría con estado de salud del medidor.
    Respuesta cacheada en memoria (TTL=0.8s) para soportar polling 1 Hz.

    Response 200:
        {
          "ok": true,
          "ciclo_id": 1234,
          "timestamp": "2026-09-14T17:00:00.123",
          "diagnostico": { "health_score": 97.5, "status": "WARNING" },
          "presiones":   { "gas_psig": 240.1, ... },
          "temperaturas":{ "aceite_f": 135.2, ... },
          "dp_medidores":{ "cunia_inH2O": 121.4, "laminar_inH2O": 5.1 },
          "fluido":      { "corte_agua_pct": 65.2, "viscosidad_calculada_cp": 11.6, ... },
          "control":     { "nivel_separador_pct": 50.1, ... },
          "caudales":    { "liquido_bpd": 3200.0, ... },
          "plc":         { "numero_prueba": 7, ... }
        }

    Response 204: Sin datos aún en tb_telemetria.
    Response 503: MySQL no disponible.
    """
    now = time.monotonic()

    # ── Servir desde caché si está vigente ────────────────────────────────
    with _cache_lock:
        if _cache["data"] is not None and now < _cache["expires_at"]:
            resp = jsonify(_cache["data"])
            resp.headers["X-Cache"] = "HIT"
            resp.headers["Cache-Control"] = "no-store"
            return resp, 200

    # ── Consultar MySQL ────────────────────────────────────────────────────
    try:
        row = DB.query_one(_SQL_LIVE, (DEFAULT_WELL_ID,))
    except Exception as exc:
        logger.error("Error consultando tb_telemetria: %s", exc)
        return jsonify({"ok": False, "error": "Base de datos no disponible",
                        "detail": str(exc)}), 503

    if row is None:
        return jsonify({"ok": False,
                        "error": "Sin datos de telemetría disponibles aún."}), 204

    payload = _build_live_response(row)

    # ── Inyectar estado PLC en la respuesta live ───────────────────────────
    backend = current_app.config.get("BACKEND")
    if backend is not None:
        plc_st = backend.plc_status
        payload["plc_connected"] = plc_st["plc_connected"]
        payload["plc_last_ok_ago_s"] = plc_st["last_ok_ago_s"]
    else:
        payload["plc_connected"]    = None   # desconocido
        payload["plc_last_ok_ago_s"] = None

    # Calcular edad del dato (segundos desde timestamp_utc del último registro)
    try:
        import datetime as _dt
        ts_str = row.get("timestamp_utc")
        if ts_str:
            ts_row = _dt.datetime.fromisoformat(str(ts_str))
            age_s  = round((
                _dt.datetime.utcnow() - ts_row.replace(tzinfo=None)
            ).total_seconds(), 1)
            payload["data_age_s"] = age_s
    except Exception:
        payload["data_age_s"] = None

    # ── Actualizar caché ───────────────────────────────────────────────────
    with _cache_lock:
        _cache["data"]       = payload
        _cache["expires_at"] = now + CACHE_TTL_LIVE

    resp = jsonify(payload)
    resp.headers["X-Cache"]       = "MISS"
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200


# ---------------------------------------------------------------------------
# Endpoint: Estado de conexión con el PLC
# ---------------------------------------------------------------------------
@bp.route("/plc-status", methods=["GET"])
def plc_status():
    """
    GET /api/monitoreo/plc-status

    Retorna el estado de conexión en tiempo real con el PLC Allen-Bradley,
    leyendo el flag en memoria del hilo BackendAutodiagnostico sin tocar la DB.

    Response 200:
        {
          "ok": true,
          "plc_connected": true,
          "plc_ip": "172.17.32.220",
          "last_ok_ago_s": 1.4,
          "last_error": "",
          "backend_alive": true
        }
    Response 503: Backend no inicializado.
    """
    backend = current_app.config.get("BACKEND")
    if backend is None:
        try:
            import app as flask_app_module
            backend = flask_app_module._BACKEND
        except ImportError:
            pass

    if backend is None:
        return jsonify({"ok": False, "error": "Backend no inicializado"}), 503

    st = backend.plc_status
    return jsonify({"ok": True, **st}), 200
