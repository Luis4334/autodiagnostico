"""
================================================================================
 api/exportacion.py — Blueprint: Exportación de datos (CSV / Excel)
 Sistema de Autodiagnóstico MPFM VOX-X4
================================================================================
 GET /api/exportacion/alarmas.csv          → CSV de tb_historico_alarmas
 GET /api/exportacion/telemetria.csv       → CSV de tb_telemetria
 GET /api/exportacion/reporte.xlsx         → Excel multi-hoja (Alarmas + Telemetría)

 Todos los endpoints responden con cabeceras Content-Disposition correctas para
 que tanto el navegador como pywebview descarguen el archivo directamente.
================================================================================
"""
from __future__ import annotations

import csv
import io
import json
import logging
import datetime
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request, Response

import db as DB
from config import DEFAULT_WELL_ID

logger = logging.getLogger("api.exportacion")
bp = Blueprint("exportacion", __name__)


# ---------------------------------------------------------------------------
# SQL base (sin ORDER/LIMIT — se construyen dinámicamente)
# ---------------------------------------------------------------------------
_SQL_ALARMAS_BASE_EXPORT = """
SELECT
    a.id, a.well_id, a.telemetria_id,
    a.timestamp_evento_utc,
    a.severidad, a.capa_diagnostico, a.codigo_alarma,
    a.health_score_pct, a.status_label,
    a.total_checks, a.failed_checks,
    a.anomalias_json,
    a.p_line_snapshot_psig, a.t_proc_snapshot_f,
    a.q_liquid_snapshot_bpd, a.q_gas_snapshot_mmscfd,
    a.wc_snapshot_pct, a.app_version
FROM tb_historico_alarmas a
WHERE a.well_id = %s
"""

_SQL_ALARMAS = _SQL_ALARMAS_BASE_EXPORT + " ORDER BY a.id DESC LIMIT %s"

_SQL_TELEMETRIA_BASE_EXPORT = """
SELECT
    t.id, t.timestamp_normal AS timestamp_normal, t.well_id, t.plc_ip,
    t.health_score_pct, t.status_label,
    t.p_gas_psig, t.p_oil_psig, t.presion_entrada_psig,
    t.pdt_01_psi, t.pdt_03_psi,
    t.t_gas_c, t.t_oil_c, t.t_oil_f,
    t.dp_w_inh2o, t.dp_l_inh2o, t.dp_simeflum_inh2o,
    t.wc_pct, t.v_oil_medida_cp, t.miu_oil_cp,
    t.gvoidf_pct, t.lit_001_pct, t.level_pid_cv_pct,
    t.q_liquido_bpd, t.q_crudo_bpd, t.q_w_bpd, t.q_gas_std,
    t.coriolis_density_kgm3, t.coriolis_vol_flow_m3h
FROM tb_telemetria t
WHERE t.well_id = %s
"""

_SQL_TELEMETRIA = _SQL_TELEMETRIA_BASE_EXPORT + " ORDER BY t.id DESC LIMIT %s"


def _build_range_queries(
    well_id: str,
    desde: str,
    hasta: str,
    limit: int,
) -> tuple:
    """
    Construye SQL y parámetros con filtro de rango datetime opcional.
    desde/hasta: cadena 'YYYY-MM-DDTHH:MM' o '' para sin filtro.
    Retorna (sql_alarmas, params_alarmas, sql_tele, params_tele).
    """
    dt_fmt_in  = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"]

    def _parse(s: str, end: bool = False):
        for fmt in dt_fmt_in:
            try:
                dt = datetime.datetime.strptime(s, fmt)
                if end and fmt == "%Y-%m-%d":
                    dt = dt.replace(hour=23, minute=59, second=59)
                return dt
            except ValueError:
                continue
        return None

    dt_desde = _parse(desde)        if desde else None
    dt_hasta = _parse(hasta, True)  if hasta else None

    # ── Alarmas ────────────────────────────────────────────────
    sql_a  = _SQL_ALARMAS_BASE_EXPORT
    par_a: list = [well_id]
    if dt_desde:
        sql_a += " AND a.timestamp_evento_utc >= %s"
        par_a.append(dt_desde)
    if dt_hasta:
        sql_a += " AND a.timestamp_evento_utc <= %s"
        par_a.append(dt_hasta)
    sql_a += " ORDER BY a.timestamp_evento_utc ASC LIMIT %s"
    par_a.append(limit)

    # ── Telemetría ────────────────────────────────────────────
    sql_t  = _SQL_TELEMETRIA_BASE_EXPORT
    par_t: list = [well_id]
    if dt_desde:
        sql_t += " AND t.timestamp_normal >= %s"
        par_t.append(dt_desde)
    if dt_hasta:
        sql_t += " AND t.timestamp_normal <= %s"
        par_t.append(dt_hasta)
    sql_t += " ORDER BY t.timestamp_normal ASC LIMIT %s"
    par_t.append(limit)

    return sql_a, tuple(par_a), sql_t, tuple(par_t)

_ALARMAS_HEADERS = [
    "ID", "Well ID", "Telemetría ID", "Timestamp",
    "Severidad", "Capa", "Código Alarma",
    "Health Score (%)", "Status", "Checks Totales", "Checks Fallidos",
    "Anomalías", "P Línea (psig)", "T Proceso (°F)",
    "Q Líquido (BPD)", "Q Gas (MMSCFD)", "WC (%)", "App Version",
]

_TELEMETRIA_HEADERS = [
    "ID", "Timestamp", "Well ID", "PLC IP",
    "Health Score (%)", "Status",
    "P Gas (psig)", "P Aceite (psig)", "P Entrada (psig)",
    "PDT-01 (psi)", "PDT-03 (psi)",
    "T Gas (°C)", "T Aceite (°C)", "T Aceite (°F)",
    "DP Cuña (inH2O)", "DP Laminar (inH2O)", "DP Simeflum (inH2O)",
    "WC (%)", "Viscosidad Medida (cP)", "Viscosidad Calc (cP)",
    "GVF SONAR (%)", "Nivel Sep (%)", "LCV Pos (%)",
    "Q Líquido (BPD)", "Q Crudo (BPD)", "Q Agua (BPD)", "Q Gas (Mscfd)",
    "Densidad Coriolis (kg/m³)", "Caudal Coriolis (m³/h)",
]


def _alarma_row(r: Dict[str, Any]) -> List:
    raw = r.get("anomalias_json", "")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            anomalias = " | ".join(parsed) if isinstance(parsed, list) else raw
        except Exception:
            anomalias = raw
    elif isinstance(raw, list):
        anomalias = " | ".join(str(x) for x in raw)
    else:
        anomalias = str(raw)

    return [
        r.get("id"), r.get("well_id"), r.get("telemetria_id"),
        r.get("timestamp_evento_utc"), r.get("severidad"),
        r.get("capa_diagnostico"), r.get("codigo_alarma"),
        r.get("health_score_pct"), r.get("status_label"),
        r.get("total_checks"), r.get("failed_checks"),
        anomalias,
        r.get("p_line_snapshot_psig"), r.get("t_proc_snapshot_f"),
        r.get("q_liquid_snapshot_bpd"), r.get("q_gas_snapshot_mmscfd"),
        r.get("wc_snapshot_pct"), r.get("app_version"),
    ]


def _telemetria_row(r: Dict[str, Any]) -> List:
    return [
        r.get("id"), r.get("timestamp_normal"), r.get("well_id"), r.get("plc_ip"),
        r.get("health_score_pct"), r.get("status_label"),
        r.get("p_gas_psig"), r.get("p_oil_psig"), r.get("presion_entrada_psig"),
        r.get("pdt_01_psi"), r.get("pdt_03_psi"),
        r.get("t_gas_c"), r.get("t_oil_c"), r.get("t_oil_f"),
        r.get("dp_w_inh2o"), r.get("dp_l_inh2o"), r.get("dp_simeflum_inh2o"),
        r.get("wc_pct"), r.get("v_oil_medida_cp"), r.get("miu_oil_cp"),
        r.get("gvoidf_pct"), r.get("lit_001_pct"), r.get("level_pid_cv_pct"),
        r.get("q_liquido_bpd"), r.get("q_crudo_bpd"), r.get("q_w_bpd"), r.get("q_gas_std"),
        r.get("coriolis_density_kgm3"), r.get("coriolis_vol_flow_m3h"),
    ]


# ---------------------------------------------------------------------------
# /api/exportacion/alarmas.csv
# ---------------------------------------------------------------------------
@bp.route("/alarmas.csv", methods=["GET"])
def export_alarmas_csv():
    """
    GET /api/exportacion/alarmas.csv?well_id=FUL-01&limit=1000

    Descarga el histórico de alarmas como CSV con cabeceras correctas.
    """
    well_id = request.args.get("well_id", DEFAULT_WELL_ID).strip().upper()
    limit   = min(int(request.args.get("limit", 10000)), 10000)
    desde   = request.args.get("desde", "")
    hasta   = request.args.get("hasta", "")

    try:
        if desde or hasta:
            sql_a, par_a, _, _ = _build_range_queries(well_id, desde, hasta, limit)
            rows: List[Dict[str, Any]] = DB.query_all(sql_a, par_a)
        else:
            rows: List[Dict[str, Any]] = DB.query_all(_SQL_ALARMAS, (well_id, limit))
    except Exception as exc:
        logger.error("Error exportando alarmas CSV: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 503

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(_ALARMAS_HEADERS)
    for r in rows:
        cw.writerow(_alarma_row(r))

    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"alarmas_{well_id}_{ts}.csv"

    return Response(
        "\ufeff" + si.getvalue(),           # BOM UTF-8 para Excel en Windows
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# /api/exportacion/telemetria.csv
# ---------------------------------------------------------------------------
@bp.route("/telemetria.csv", methods=["GET"])
def export_telemetria_csv():
    """
    GET /api/exportacion/telemetria.csv?well_id=FUL-01&limit=500
    """
    well_id = request.args.get("well_id", DEFAULT_WELL_ID).strip().upper()
    limit   = min(int(request.args.get("limit", 500)), 5000)

    try:
        rows: List[Dict[str, Any]] = DB.query_all(_SQL_TELEMETRIA, (well_id, limit))
    except Exception as exc:
        logger.error("Error exportando telemetría CSV: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 503

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(_TELEMETRIA_HEADERS)
    for r in rows:
        cw.writerow(_telemetria_row(r))

    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"telemetria_{well_id}_{ts}.csv"

    return Response(
        "\ufeff" + si.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# /api/exportacion/reporte.xlsx  (requiere openpyxl)
# ---------------------------------------------------------------------------
@bp.route("/reporte.xlsx", methods=["GET"])
def export_reporte_xlsx():
    """
    GET /api/exportacion/reporte.xlsx?well_id=FUL-01&limit=500

    Genera un Excel con dos hojas:
      • Alarmas    — últimas N alarmas de tb_historico_alarmas
      • Telemetría — últimas N muestras de tb_telemetria
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({
            "ok":    False,
            "error": "openpyxl no instalado. Ejecuta: pip install openpyxl",
        }), 501

    well_id = request.args.get("well_id", DEFAULT_WELL_ID).strip().upper()
    limit   = min(int(request.args.get("limit", 5000)), 10000)
    desde   = request.args.get("desde", "").strip()
    hasta   = request.args.get("hasta", "").strip()

    sql_a, par_a, sql_t, par_t = _build_range_queries(well_id, desde, hasta, limit)

    try:
        alarm_rows = DB.query_all(sql_a, par_a)
        tele_rows  = DB.query_all(sql_t, par_t)
    except Exception as exc:
        logger.error("Error generando Excel: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 503

    # ── Rótulo de rango para el título del Excel ─────────────────────
    if desde or hasta:
        rango_lbl = f"{desde or 'inicio'} → {hasta or 'ahora'}"
    else:
        rango_lbl = f"last {limit} records"

    wb = openpyxl.Workbook()

    # ── Estilos ──────────────────────────────────────────────────────────
    HEADER_FILL  = PatternFill("solid", fgColor="1E293B")   # slate-800
    HEADER_FONT  = Font(bold=True, color="38BDF8", name="Calibri", size=10)
    TITLE_FONT   = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
    CELL_ALIGN   = Alignment(horizontal="left", vertical="center", wrap_text=False)

    def _style_sheet(ws, headers: List[str]) -> None:
        # Título en fila 1
        ws.row_dimensions[1].height = 22
        title_cell = ws.cell(row=1, column=1,
            value=f"MPFM VOX-X4 | {well_id} | Rango: {rango_lbl} | Generado: {datetime.datetime.now():%Y-%m-%d %H:%M}")
        title_cell.font = TITLE_FONT
        title_cell.fill = PatternFill("solid", fgColor="0F172A")
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))

        # Cabeceras en fila 2
        for col, h in enumerate(headers, start=1):
            c = ws.cell(row=2, column=col, value=h)
            c.font  = HEADER_FONT
            c.fill  = HEADER_FILL
            c.alignment = CELL_ALIGN
        ws.freeze_panes = "A3"

    def _auto_width(ws, headers: List[str]) -> None:
        for col_idx, _ in enumerate(headers, start=1):
            letter = get_column_letter(col_idx)
            ws.column_dimensions[letter].width = 18

    # ── Hoja 1: Alarmas ───────────────────────────────────────────────────
    ws_alarms = wb.active
    ws_alarms.title = "Alarmas"
    _style_sheet(ws_alarms, _ALARMAS_HEADERS)
    for row_idx, r in enumerate(alarm_rows, start=3):
        for col_idx, val in enumerate(_alarma_row(r), start=1):
            cell = ws_alarms.cell(row=row_idx, column=col_idx, value=val)
            cell.alignment = CELL_ALIGN
            # Colorear por severidad
            sev = r.get("severidad", "")
            colors = {
                "FATAL":    "4B1C1C",
                "CRITICAL": "7F1D1D",
                "WARNING":  "78350F",
                "INFO":     "1E293B",
            }
            if sev in colors:
                cell.fill = PatternFill("solid", fgColor=colors[sev])
    _auto_width(ws_alarms, _ALARMAS_HEADERS)

    # ── Hoja 2: Telemetría ───────────────────────────────────────────────
    ws_tele = wb.create_sheet("Telemetría")
    _style_sheet(ws_tele, _TELEMETRIA_HEADERS)
    for row_idx, r in enumerate(tele_rows, start=3):
        for col_idx, val in enumerate(_telemetria_row(r), start=1):
            cell = ws_tele.cell(row=row_idx, column=col_idx, value=val)
            cell.alignment = CELL_ALIGN
    _auto_width(ws_tele, _TELEMETRIA_HEADERS)

    # ── Serializar ───────────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    sfx = f"{desde[:10]}_{hasta[:10]}" if (desde and hasta) else ts
    filename = f"reporte_MPFM_{well_id}_{sfx}.xlsx"

    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
