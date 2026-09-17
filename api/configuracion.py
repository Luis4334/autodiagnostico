"""
================================================================================
 api/configuracion.py — Blueprint: GET y POST /api/configuracion
 Sistema de Autodiagnóstico MPFM VOX-X4
================================================================================
 GET  /api/configuracion          → Lee la envolvente activa del pozo
 POST /api/configuracion          → Actualiza umbrales (desactiva la anterior
                                    y crea una nueva fila versionada)
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify, request

import db as DB
from config import DEFAULT_WELL_ID

logger = logging.getLogger("api.configuracion")
bp = Blueprint("configuracion", __name__)

# ---------------------------------------------------------------------------
# Campos editables vía POST y sus tipos de validación
# ---------------------------------------------------------------------------
EDITABLE_FIELDS: Dict[str, type] = {
    "description":              str,
    "api_gravity":              float,
    "gas_specific_gravity":     float,
    "expected_gor_scf_stb":     float,
    "gor_tolerance_pct":        float,
    "min_liquid_rate_bpd":      float,
    "max_liquid_rate_bpd":      float,
    "min_gas_rate_mmscfd":      float,
    "max_gas_rate_mmscfd":      float,
    "max_dp_psi":               float,
    "viscosity_transition_cp":  float,
    "max_sonar_gas_pct":        float,
    "p_line_min_psig":          float,
    "p_line_max_psig":          float,
    "window_size_samples":      int,
    "frozen_tag_std_threshold": float,
    "outlier_zscore_threshold": float,
    "abs_min_wc_pct":           float,
    "abs_min_p_line_psig":      float,
    "abs_min_t_proc_f":         float,
    "abs_min_dp_wedge_inh2o":   float,
    "abs_min_dp_laminar_inh2o": float,
    "abs_min_viscosity_cp":     float,
    "abs_min_level_pct":        float,
    "abs_min_sonar_gas_pct":    float,
}

# Rangos de validación física mínimos
FIELD_RANGES: Dict[str, Tuple[float, float]] = {
    "api_gravity":              (1.0,   100.0),
    "gas_specific_gravity":     (0.1,   3.0),
    "expected_gor_scf_stb":     (0.0,   100_000.0),
    "gor_tolerance_pct":        (1.0,   99.0),
    "min_liquid_rate_bpd":      (0.0,   50_000.0),
    "max_liquid_rate_bpd":      (1.0,   100_000.0),
    "min_gas_rate_mmscfd":      (0.0,   100.0),
    "max_gas_rate_mmscfd":      (0.001, 1_000.0),
    "max_dp_psi":               (0.1,   500.0),
    "viscosity_transition_cp":  (1.0,   10_000.0),
    "max_sonar_gas_pct":        (0.0,   100.0),
    "p_line_min_psig":          (0.0,   10_000.0),
    "p_line_max_psig":          (1.0,   10_000.0),
    "window_size_samples":      (5,     500),
    "frozen_tag_std_threshold": (1e-9,  100.0),
    "outlier_zscore_threshold": (1.0,   10.0),
    "abs_min_wc_pct":           (0.1,   100.0),
    "abs_min_p_line_psig":      (0.1,   1000.0),
    "abs_min_t_proc_f":         (0.1,   100.0),
    "abs_min_dp_wedge_inh2o":   (0.1,   1000.0),
    "abs_min_dp_laminar_inh2o": (0.1,   100.0),
    "abs_min_viscosity_cp":     (0.1,   100.0),
    "abs_min_level_pct":        (0.1,   100.0),
    "abs_min_sonar_gas_pct":    (0.1,   100.0),
}

# SQL de lectura
_SQL_GET = """
SELECT
    id, well_id, description,
    api_gravity, gas_specific_gravity,
    expected_gor_scf_stb, gor_tolerance_pct,
    min_liquid_rate_bpd, max_liquid_rate_bpd,
    min_gas_rate_mmscfd, max_gas_rate_mmscfd,
    max_dp_psi, viscosity_transition_cp, max_sonar_gas_pct,
    p_line_min_psig, p_line_max_psig,
    window_size_samples, frozen_tag_std_threshold, outlier_zscore_threshold,
    abs_min_wc_pct, abs_min_p_line_psig, abs_min_t_proc_f, abs_min_dp_wedge_inh2o,
    abs_min_dp_laminar_inh2o, abs_min_viscosity_cp, abs_min_level_pct, abs_min_sonar_gas_pct,
    is_active, created_at, updated_at
FROM tb_config_pozo
WHERE well_id = %s AND is_active = 1
ORDER BY id DESC
LIMIT 1
"""


# ---------------------------------------------------------------------------
# GET /api/configuracion
# ---------------------------------------------------------------------------
@bp.route("/configuracion", methods=["GET"])
def get_configuracion():
    """
    GET /api/configuracion

    Lee la configuración activa del pozo (is_active=1).

    Query params:
        well_id (str, opcional): código del pozo (default: FUL-01)

    Response 200:
        {
          "ok": true,
          "well_id": "FUL-01",
          "configuracion": {
            "api_gravity": 26.9,
            "expected_gor_scf_stb": 850.0,
            ...
          }
        }
    Response 404: Pozo no encontrado o sin configuración activa.
    """
    well_id = request.args.get("well_id", DEFAULT_WELL_ID).strip().upper()

    try:
        row = DB.query_one(_SQL_GET, (well_id,))
    except Exception as exc:
        logger.error("Error leyendo tb_config_pozo: %s", exc)
        return jsonify({"ok": False, "error": "Base de datos no disponible",
                        "detail": str(exc)}), 503

    if row is None:
        return jsonify({"ok": False,
                        "error": f"No se encontró configuración activa para el pozo '{well_id}'."}), 404

    return jsonify({"ok": True, "well_id": well_id, "configuracion": row}), 200


# ---------------------------------------------------------------------------
# POST /api/configuracion
# ---------------------------------------------------------------------------
@bp.route("/configuracion", methods=["POST"])
def post_configuracion():
    """
    POST /api/configuracion

    Actualiza la envolvente operativa del pozo.
    Estrategia de versionado: desactiva el registro actual (is_active=0)
    e inserta uno nuevo (is_active=1).

    Body JSON (todos los campos son opcionales):
        {
          "well_id":              "FUL-01",
          "api_gravity":          26.9,
          "expected_gor_scf_stb": 850.0,
          "gor_tolerance_pct":    30.0,
          "max_dp_psi":           10.0,
          ...
        }

    Response 200: { "ok": true, "nuevo_id": 2, "campos_actualizados": [...] }
    Response 400: Validación fallida.
    Response 503: Error de base de datos.
    """
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    well_id = str(body.get("well_id", DEFAULT_WELL_ID)).strip().upper()

    # ── Leer configuración actual como base ────────────────────────────────
    try:
        current = DB.query_one(_SQL_GET, (well_id,))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503

    if current is None:
        return jsonify({
            "ok": False,
            "error": f"No existe configuración base para el pozo '{well_id}'. "
                     "Crea el pozo primero en tb_config_pozo."
        }), 404

    # ── Validar y filtrar campos editables ────────────────────────────────
    updated: Dict[str, Any] = {}
    errors: list = []

    for field, cast in EDITABLE_FIELDS.items():
        if field not in body:
            continue
        raw = body[field]
        # Castear
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            errors.append(f"'{field}': no se puede convertir '{raw}' a {cast.__name__}")
            continue
        # Validar rango
        if field in FIELD_RANGES:
            lo, hi = FIELD_RANGES[field]
            if not (lo <= value <= hi):
                errors.append(f"'{field}': {value} fuera de rango [{lo}, {hi}]")
                continue
        updated[field] = value

    if errors:
        return jsonify({"ok": False, "errores_validacion": errors}), 400

    if not updated:
        return jsonify({"ok": False,
                        "error": "No se recibió ningún campo editable en el cuerpo."}), 400

    # Validación cruzada: min < max
    new_min_liq = updated.get("min_liquid_rate_bpd", current["min_liquid_rate_bpd"])
    new_max_liq = updated.get("max_liquid_rate_bpd", current["max_liquid_rate_bpd"])
    if new_min_liq >= new_max_liq:
        return jsonify({"ok": False,
                        "error": "min_liquid_rate_bpd debe ser menor que max_liquid_rate_bpd."}), 400

    new_p_min = updated.get("p_line_min_psig", current["p_line_min_psig"])
    new_p_max = updated.get("p_line_max_psig", current["p_line_max_psig"])
    if new_p_min >= new_p_max:
        return jsonify({"ok": False,
                        "error": "p_line_min_psig debe ser menor que p_line_max_psig."}), 400

    # ── Merge: base actual + nuevos valores ───────────────────────────────
    merged = {
        "well_id":                  well_id,
        "description":              updated.get("description",             current.get("description")),
        "api_gravity":              updated.get("api_gravity",             current["api_gravity"]),
        "gas_specific_gravity":     updated.get("gas_specific_gravity",    current["gas_specific_gravity"]),
        "expected_gor_scf_stb":     updated.get("expected_gor_scf_stb",    current["expected_gor_scf_stb"]),
        "gor_tolerance_pct":        updated.get("gor_tolerance_pct",       current["gor_tolerance_pct"]),
        "min_liquid_rate_bpd":      updated.get("min_liquid_rate_bpd",     current["min_liquid_rate_bpd"]),
        "max_liquid_rate_bpd":      updated.get("max_liquid_rate_bpd",     current["max_liquid_rate_bpd"]),
        "min_gas_rate_mmscfd":      updated.get("min_gas_rate_mmscfd",     current["min_gas_rate_mmscfd"]),
        "max_gas_rate_mmscfd":      updated.get("max_gas_rate_mmscfd",     current["max_gas_rate_mmscfd"]),
        "max_dp_psi":               updated.get("max_dp_psi",              current["max_dp_psi"]),
        "viscosity_transition_cp":  updated.get("viscosity_transition_cp", current["viscosity_transition_cp"]),
        "max_sonar_gas_pct":        updated.get("max_sonar_gas_pct",       current["max_sonar_gas_pct"]),
        "p_line_min_psig":          updated.get("p_line_min_psig",         current["p_line_min_psig"]),
        "p_line_max_psig":          updated.get("p_line_max_psig",         current["p_line_max_psig"]),
        "window_size_samples":      updated.get("window_size_samples",     current["window_size_samples"]),
        "frozen_tag_std_threshold": updated.get("frozen_tag_std_threshold",current["frozen_tag_std_threshold"]),
        "outlier_zscore_threshold": updated.get("outlier_zscore_threshold",current["outlier_zscore_threshold"]),
        "abs_min_wc_pct":           updated.get("abs_min_wc_pct",          current["abs_min_wc_pct"]),
        "abs_min_p_line_psig":      updated.get("abs_min_p_line_psig",     current["abs_min_p_line_psig"]),
        "abs_min_t_proc_f":         updated.get("abs_min_t_proc_f",        current["abs_min_t_proc_f"]),
        "abs_min_dp_wedge_inh2o":   updated.get("abs_min_dp_wedge_inh2o",  current["abs_min_dp_wedge_inh2o"]),
        "abs_min_dp_laminar_inh2o": updated.get("abs_min_dp_laminar_inh2o",current["abs_min_dp_laminar_inh2o"]),
        "abs_min_viscosity_cp":     updated.get("abs_min_viscosity_cp",    current["abs_min_viscosity_cp"]),
        "abs_min_level_pct":        updated.get("abs_min_level_pct",       current["abs_min_level_pct"]),
        "abs_min_sonar_gas_pct":    updated.get("abs_min_sonar_gas_pct",   current["abs_min_sonar_gas_pct"]),
        "is_active":                1,
    }

    try:
        # Transacción: desactivar anterior + insertar nuevo
        with DB.get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Desactivar registro anterior
                cur.execute(
                    "UPDATE tb_config_pozo SET is_active = 0 WHERE well_id = %s AND is_active = 1",
                    (well_id,)
                )
                # 2. Insertar nueva versión
                cols   = ", ".join(merged.keys())
                placeholders = ", ".join(["%s"] * len(merged))
                cur.execute(
                    f"INSERT INTO tb_config_pozo ({cols}) VALUES ({placeholders})",
                    tuple(merged.values())
                )
                nuevo_id = cur.lastrowid
            conn.commit()
    except Exception as exc:
        logger.error("Error actualizando tb_config_pozo: %s", exc)
        return jsonify({"ok": False, "error": "Error al guardar configuración.",
                        "detail": str(exc)}), 503

    logger.info("Configuracion pozo '%s' actualizada -> nuevo id=%d. Campos: %s",
                well_id, nuevo_id, list(updated.keys()))

    return jsonify({
        "ok":               True,
        "nuevo_id":         nuevo_id,
        "well_id":          well_id,
        "campos_actualizados": list(updated.keys()),
        "configuracion":    merged,
    }), 200
