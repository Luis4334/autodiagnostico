"""
================================================================================
 BACKEND DE AUTODIAGNÓSTICO CONTINUO - MEDIDOR MULTIFÁSICO VOX-X4
 Módulo  : backend_autodiagnostico.py
 Autor   : Senior Automation Engineer
 Versión : 1.0.0
 Fecha   : 2026-09-14
================================================================================
 Arquitectura:
   • SimPLCReader  → Lee todos los tags via pylogix (Allen-Bradley, IP 172.17.32.220)
   • MPFMSelfVerification → Motor de autodiagnóstico en 3 capas
   • BackendAutodiagnostico → Orquestador con hilo daemon (while True / 2 s)
   • PyMySQL → Persistencia en db_autodiagnostico (XAMPP)

 Capas de diagnóstico:
   CAPA 1 – Salud estadística de sensores (señal congelada, outliers Z-score)
   CAPA 2 – Coherencia hidráulica del medidor (DP, nivel, válvulas, SONAR)
   CAPA 3 – Coherencia con el comportamiento del pozo (balance, RGP, presión)

 Integridad del audit trail:
   Cada fila de tb_historico_alarmas se firma con SHA-256(well_id +
   timestamp + health_score + anomalias_json) antes del INSERT.
================================================================================
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import socket
import sys
import threading
import time
import datetime
from typing import Any, Dict, List, Optional, Tuple

import pymysql
import pymysql.cursors

# --------------------------------------------------------------------------- #
# Importar clases propias del proyecto                                         #
# --------------------------------------------------------------------------- #
# selfmeter.py  →  MPFMSelfVerification, WellOperatingEnvelope, DiagnosticResult
from selfmeter import MPFMSelfVerification, WellOperatingEnvelope, DiagnosticResult

# plc_tag_reader.py → SimPLCReader
from plc_tag_reader import SimPLCReader


# =========================================================================== #
#  CONFIGURACIÓN GLOBAL                                                         #
# =========================================================================== #

# --- PLC -------------------------------------------------------------------
PLC_IP_ADDRESS: str = "172.17.32.220"

# --- Base de datos (XAMPP / MySQL) -----------------------------------------
DB_CONFIG: Dict[str, Any] = {
    "host":    "127.0.0.1",
    "port":    3306,
    "user":    "root",
    "passwd":  "",
    "db":      "db_autodiagnostico",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": 10,
    "autocommit": False,            # transacciones explícitas
}

# --- Pozo activo -----------------------------------------------------------
WELL_ID: str = "FUL-01"

# --- Ciclo de adquisición --------------------------------------------------
ACQUISITION_INTERVAL_S: float = 2.0    # segundos entre lecturas del PLC

# --- Ventana estadística ---------------------------------------------------
WINDOW_SIZE_SAMPLES: int = 30

# --- Versión de la aplicación ----------------------------------------------
APP_VERSION: str = "1.0.0"

# --- Umbral de Health Score para disparar alarma --------------------------
HEALTH_ALARM_THRESHOLD: float = 100.0  # cualquier caída < 100 % genera registro

# =========================================================================== #
#  LOGGING                                                                      #
# =========================================================================== #
logging.Formatter.converter = time.localtime   # hora local, no UTC
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-8s] [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler("backend_autodiagnostico.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("BackendAutodiagnostico")


# =========================================================================== #
#  MAPEADOR DE TAGS PLC → DICCIONARIO add_telemetry                             #
# =========================================================================== #

def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convierte un valor de pylogix a float; devuelve default si falla."""
    if value is None or (isinstance(value, str) and value.startswith("Error")):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Convierte un valor de pylogix a bool."""
    if value is None or (isinstance(value, str) and value.startswith("Error")):
        return default
    try:
        return bool(value)
    except (TypeError, ValueError):
        return default


def map_plc_tags_to_telemetry(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transforma el diccionario crudo de pylogix al formato que espera
    MPFMSelfVerification.add_telemetry() y el INSERT en tb_telemetria.

    Convención de nombres internos (compatibles con selfmeter.py):
      P_line_psig          → presión de línea representativa
      T_proc_F             → temperatura de proceso en °F
      DP_wedge_inH2O       → DP del medidor cuña
      DP_laminar_inH2O     → DP del medidor laminar
      WaterCut_pct         → corte de agua
      Viscosity_cP         → viscosidad dinámica
      Separator_Level_pct  → nivel del separador
      LCV_Position_pct     → posición de válvula de control de nivel
      PCV_Position_pct     → posición de válvula de control de presión
      Sonar_EntrainedGas_pct → GVF por SONAR
      Skid_Total_DP_psi    → DP total del skid (PDT-01 + PDT-03 ~ convertido)
      Q_liquid_bpd         → caudal total de líquido
      Q_oil_bpd            → caudal de crudo
      Q_gas_mmscfd         → caudal de gas en MMSCFD
    """
    # Conversión de unidades: 1 psi = 27.6799 inH2O (a 4 °C)
    PDT01_PSI   = _safe_float(raw.get("PDT_01"),    0.0)
    PDT03_PSI   = _safe_float(raw.get("PDT_03"),    0.0)
    SKID_DP_PSI = PDT01_PSI + PDT03_PSI              # DP total del skid en psi

    # Q_gas_STD viene en unidades PLC (verificar con instrumentista si es MSCFD / MMSCFD)
    # Por defecto asumimos MSCFD → convertir a MMSCFD diviendo entre 1000
    Q_GAS_STD_MSCFD = _safe_float(raw.get("Q_gas_STD"), 0.0)
    Q_GAS_MMSCFD    = Q_GAS_STD_MSCFD / 1000.0

    return {
        # ── Identificación temporal ──────────────────────────────────────
        "timestamp":               datetime.datetime.now().isoformat(),

        # ── Variables para selfmeter.py (nombres canónicos) ──────────────
        "P_line_psig":             _safe_float(raw.get("PRESION_ENTRADA"), 0.0),
        "T_proc_F":                _safe_float(raw.get("T_Oil_F"),         0.0),
        "DP_wedge_inH2O":          _safe_float(raw.get("DP_W"),            0.0),
        "DP_laminar_inH2O":        _safe_float(raw.get("DP_L"),            0.0),
        "WaterCut_pct":            _safe_float(raw.get("WC"),              0.0),
        "Viscosity_cP":            _safe_float(raw.get("miu_Oil"),         0.0),
        "Separator_Level_pct":     _safe_float(raw.get("LIT_001"),         50.0),
        "LCV_Position_pct":        _safe_float(raw.get("Program:PID.LEVEL_PID.CV"), 50.0),
        "PCV_Position_pct":        _safe_float(raw.get("Program:PID.PRESS_PID.CV"), 50.0),
        "Sonar_EntrainedGas_pct":  _safe_float(raw.get("GVoidF"),          0.0),
        "Skid_Total_DP_psi":       SKID_DP_PSI,
        "Q_liquid_bpd":            _safe_float(raw.get("Q_Liquido"),       0.0),
        "Q_oil_bpd":               _safe_float(raw.get("Q_Crudo"),         0.0),
        "Q_gas_mmscfd":            Q_GAS_MMSCFD,
        "Q_diluent_bpd":           _safe_float(raw.get("caudal_diluente_BM"), 0.0),

        # ── Variables extendidas (para tb_telemetria) ────────────────────
        "p_gas_psig":              _safe_float(raw.get("P_Gas"),           None),
        "p_oil_psig":              _safe_float(raw.get("P_Oil"),           None),
        "presion_entrada_psig":    _safe_float(raw.get("PRESION_ENTRADA"), None),
        "pdt_01_psi":              PDT01_PSI,
        "pdt_03_psi":              PDT03_PSI,
        "press_pid_cv_pct":        _safe_float(raw.get("Program:PID.PRESS_PID.CV"), None),
        "t_gas_c":                 _safe_float(raw.get("T_GAS"),           None),
        "t_oil_c":                 _safe_float(raw.get("T_Oil_C"),         None),
        "t_oil_f":                 _safe_float(raw.get("T_Oil_F"),         None),
        "coriolis_temp_c":         _safe_float(raw.get("CORIOLIS_TEMPERATURE"), None),
        "dp_w_inh2o":              _safe_float(raw.get("DP_W"),            None),
        "dp_l_inh2o":              _safe_float(raw.get("DP_L"),            None),
        "dp_simeflum_inh2o":       _safe_float(raw.get("DP_Simeflum"),     None),
        "lit_001_pct":             _safe_float(raw.get("LIT_001"),         None),
        "level_pid_cv_pct":        _safe_float(raw.get("Program:PID.LEVEL_PID.CV"), None),
        "transmisor_baja":         _safe_float(raw.get("transmisor_baja"), None),
        "wc_pct":                  _safe_float(raw.get("WC"),              None),
        "wc_sw":                   _safe_bool(raw.get("Program:MainProgram.WC_SW")),
        "v_oil_medida_cp":         _safe_float(raw.get("v_oil_medida"),    None),
        "miu_oil_cp":              _safe_float(raw.get("miu_Oil"),         None),
        "gvoidf_pct":              _safe_float(raw.get("GVoidF"),          None),
        "gvf_sw":                  _safe_bool(raw.get("Program:MainProgram.GVF_SW")),
        "q_liquido_bpd":           _safe_float(raw.get("Q_Liquido"),       None),
        "q_crudo_bpd":             _safe_float(raw.get("Q_Crudo"),         None),
        "q_w_bpd":                 _safe_float(raw.get("Q_W"),             None),
        "q_gat_bpd":               _safe_float(raw.get("Q_gat"),           None),
        "q_gas_std":               _safe_float(raw.get("Q_gas_STD"),       None),
        "q_gas_t_sc":              _safe_float(raw.get("Program:Caudal.Q_gas_T_sc"), None),
        "qb_liquido_estimado_bpd": _safe_float(raw.get("Qb_Liquido_Estimado"),    None),
        "q_crudo_estimado_bpd":    _safe_float(raw.get("Q_Crudo_Estimado"),       None),
        "qb_diluente_estimado_bpd":_safe_float(raw.get("Qb_Diluente_Estimado"),   None),
        "q_w_estimado_bpd":        _safe_float(raw.get("Q_W_Estimado"),           None),
        "q_gat_estimado_bpd":      _safe_float(raw.get("Q_gat_Estimado"),         None),
        "coriolis_density_kgm3":   _safe_float(raw.get("CORIOLIS_DENSITY"),        None),
        "coriolis_vol_flow_m3h":   _safe_float(raw.get("CORIOLIS_VOL_FLOW_RATE"),  None),
        "caudal_diluente_bm":      _safe_float(raw.get("caudal_diluente_BM"),      None),
        "numero_prueba":           raw.get("Numero_Prueba"),
        "tipo_equipo":             raw.get("TIPO_EQUIPO"),
        "prueba_estatus":          _safe_bool(raw.get("Program:Prueba.ESTATUS")),
        "sw_dil_medido_calc":      _safe_bool(raw.get("SW_DIL_MEDIDO_CALC")),
        "vi_sw":                   _safe_bool(raw.get("VI_SW")),
        "deshabilita_pid":         _safe_bool(raw.get("DESHABILITA_PID")),
        "apertura_valvula_pct":    _safe_float(raw.get("Program:MainProgram.Apertura_Valvula"), None),
        "abrir_valvula_man":       _safe_bool(raw.get("Program:MainProgram.Abrir_Valvula_Man")),
        "abrir_valvula_auto":      _safe_bool(raw.get("Program:MainProgram.Abrir_Valvula_Auto")),
        "status_error_caudal":     _safe_bool(raw.get("STATUS_ERROR_CAUDAL_NETO_DILUENTE")),
        "man_pc_pct":              _safe_float(raw.get("Program:PID.MAN_PC"), None),
        "man_lc_pct":              _safe_float(raw.get("Program:PID.MAN_LC"), None),
    }


# =========================================================================== #
#  HELPERS DE BASE DE DATOS                                                     #
# =========================================================================== #

def _get_db_connection() -> pymysql.connections.Connection:
    """Crea y retorna una conexión PyMySQL con reintentos."""
    max_retries = 5
    delay_s     = 3
    for attempt in range(1, max_retries + 1):
        try:
            conn = pymysql.connect(**DB_CONFIG)
            return conn
        except pymysql.err.OperationalError as exc:
            logger.warning(
                "DB: Intento %d/%d fallido → %s. Reintentando en %ds…",
                attempt, max_retries, exc, delay_s
            )
            if attempt == max_retries:
                raise
            time.sleep(delay_s)


def _compute_row_hash(well_id: str, ts: str, score: float, anomalias: str) -> str:
    """
    SHA-256 del audit trail. Se firma ANTES del INSERT para detectar
    cualquier manipulación posterior de la fila en la base de datos.
    Formato: SHA-256( well_id | "|" | ts | "|" | score | "|" | anomalias )
    """
    payload = f"{well_id}|{ts}|{score:.3f}|{anomalias}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _classify_alarm(
    diag: DiagnosticResult,
) -> Tuple[str, str, str]:
    """
    Determina severidad, capa dominante y código de alarma a partir
    del DiagnosticResult. Retorna (severidad, capa, codigo_alarma).
    """
    score = diag.overall_health_score
    alerts = diag.anomalies_detected

    # ── Severidad ─────────────────────────────────────────────────────────
    if diag.status == "CRITICAL":
        severidad = "CRITICAL"
    elif diag.status == "WARNING":
        severidad = "WARNING"
    else:
        severidad = "INFO"   # score < 100 pero sin anomalías explícitas

    if score < 50.0:
        severidad = "FATAL"

    # ── Capa dominante ────────────────────────────────────────────────────
    # Prioridad: CAPA_1 > CAPA_2 > CAPA_3 (mayor riesgo metrológico)
    has_capa1 = any(k in ["frozen", "outlier"] for k in
                    [k.split("_")[-1] for k in diag.statistical_health.keys()
                     if not diag.statistical_health[k]])
    has_capa2 = any(not v for v in diag.hydraulic_coherence.values())
    has_capa3 = any(not v for v in diag.well_behavior_coherence.values())

    if has_capa1:
        capa = "CAPA_1"
        codigo = "ALM-SENSOR-001"
    elif has_capa2:
        capa = "CAPA_2"
        codigo = "ALM-HYDRO-001"
    elif has_capa3:
        capa = "CAPA_3"
        codigo = "ALM-WELL-001"
    else:
        capa = "CAPA_3"
        codigo = "ALM-SCORE-001"

    return severidad, capa, codigo


# =========================================================================== #
#  INSERT – tb_telemetria                                                        #
# =========================================================================== #

SQL_INSERT_TELEMETRIA = """
INSERT INTO tb_telemetria (
    well_id, plc_ip, timestamp_normal, health_score_pct, status_label,
    p_gas_psig, p_oil_psig, presion_entrada_psig, pdt_01_psi, pdt_03_psi,
    press_pid_cv_pct,
    t_gas_c, t_oil_c, t_oil_f, coriolis_temp_c,
    dp_w_inh2o, dp_l_inh2o, dp_simeflum_inh2o,
    lit_001_pct, level_pid_cv_pct, transmisor_baja,
    wc_pct, wc_sw, v_oil_medida_cp, miu_oil_cp,
    gvoidf_pct, gvf_sw,
    q_liquido_bpd, q_crudo_bpd, q_w_bpd, q_gat_bpd, q_gas_std, q_gas_t_sc,
    qb_liquido_estimado_bpd, q_crudo_estimado_bpd, qb_diluente_estimado_bpd,
    q_w_estimado_bpd, q_gat_estimado_bpd,
    coriolis_density_kgm3, coriolis_vol_flow_m3h, caudal_diluente_bm,
    numero_prueba, tipo_equipo, prueba_estatus, sw_dil_medido_calc,
    vi_sw, deshabilita_pid, apertura_valvula_pct,
    abrir_valvula_man, abrir_valvula_auto, status_error_caudal,
    man_pc_pct, man_lc_pct
) VALUES (
    %(well_id)s, %(plc_ip)s, %(timestamp_normal)s, %(health_score_pct)s, %(status_label)s,
    %(p_gas_psig)s, %(p_oil_psig)s, %(presion_entrada_psig)s, %(pdt_01_psi)s, %(pdt_03_psi)s,
    %(press_pid_cv_pct)s,
    %(t_gas_c)s, %(t_oil_c)s, %(t_oil_f)s, %(coriolis_temp_c)s,
    %(dp_w_inh2o)s, %(dp_l_inh2o)s, %(dp_simeflum_inh2o)s,
    %(lit_001_pct)s, %(level_pid_cv_pct)s, %(transmisor_baja)s,
    %(wc_pct)s, %(wc_sw)s, %(v_oil_medida_cp)s, %(miu_oil_cp)s,
    %(gvoidf_pct)s, %(gvf_sw)s,
    %(q_liquido_bpd)s, %(q_crudo_bpd)s, %(q_w_bpd)s, %(q_gat_bpd)s,
    %(q_gas_std)s, %(q_gas_t_sc)s,
    %(qb_liquido_estimado_bpd)s, %(q_crudo_estimado_bpd)s, %(qb_diluente_estimado_bpd)s,
    %(q_w_estimado_bpd)s, %(q_gat_estimado_bpd)s,
    %(coriolis_density_kgm3)s, %(coriolis_vol_flow_m3h)s, %(caudal_diluente_bm)s,
    %(numero_prueba)s, %(tipo_equipo)s, %(prueba_estatus)s, %(sw_dil_medido_calc)s,
    %(vi_sw)s, %(deshabilita_pid)s, %(apertura_valvula_pct)s,
    %(abrir_valvula_man)s, %(abrir_valvula_auto)s, %(status_error_caudal)s,
    %(man_pc_pct)s, %(man_lc_pct)s
)
"""


def _insert_telemetria(
    conn: pymysql.connections.Connection,
    mapped: Dict[str, Any],
    diag: DiagnosticResult,
) -> int:
    """
    Inserta una fila en tb_telemetria.
    Retorna el lastrowid para vincularlo en la alarma.
    """
    row = {**mapped}
    row["well_id"]          = WELL_ID
    row["plc_ip"]           = PLC_IP_ADDRESS
    row["timestamp_normal"]  = mapped["timestamp"]
    row["health_score_pct"] = diag.overall_health_score
    row["status_label"]     = diag.status

    with conn.cursor() as cur:
        cur.execute(SQL_INSERT_TELEMETRIA, row)
        last_id = cur.lastrowid   # capturar ANTES del commit
    conn.commit()
    return last_id


# =========================================================================== #
#  INSERT – tb_historico_alarmas                                                 #
# =========================================================================== #

SQL_INSERT_ALARMA = """
INSERT INTO tb_historico_alarmas (
    well_id, telemetria_id, timestamp_evento_utc,
    severidad, capa_diagnostico, codigo_alarma,
    health_score_pct, status_label,
    total_checks, failed_checks,
    anomalias_json, stat_health_json, hyd_health_json,
    well_health_json, audit_record_json,
    p_line_snapshot_psig, t_proc_snapshot_f,
    q_liquid_snapshot_bpd, q_gas_snapshot_mmscfd, wc_snapshot_pct,
    app_version, host_registrador, row_hash_sha256
) VALUES (
    %(well_id)s, %(telemetria_id)s, %(timestamp_evento_utc)s,
    %(severidad)s, %(capa_diagnostico)s, %(codigo_alarma)s,
    %(health_score_pct)s, %(status_label)s,
    %(total_checks)s, %(failed_checks)s,
    %(anomalias_json)s, %(stat_health_json)s, %(hyd_health_json)s,
    %(well_health_json)s, %(audit_record_json)s,
    %(p_line_snapshot_psig)s, %(t_proc_snapshot_f)s,
    %(q_liquid_snapshot_bpd)s, %(q_gas_snapshot_mmscfd)s, %(wc_snapshot_pct)s,
    %(app_version)s, %(host_registrador)s, %(row_hash_sha256)s
)
"""


def _insert_alarma(
    conn: pymysql.connections.Connection,
    diag: DiagnosticResult,
    mapped: Dict[str, Any],
    telemetria_id: int,
    severidad: str,
    capa: str,
    codigo: str,
) -> None:
    """Inserta una fila en tb_historico_alarmas con firma SHA-256."""

    ts_evento      = mapped["timestamp"]
    anomalias_str  = json.dumps(diag.anomalies_detected, ensure_ascii=False)
    row_hash       = _compute_row_hash(
        WELL_ID, ts_evento, diag.overall_health_score, anomalias_str
    )

    # Calcula checks totales / fallidos desde los diccionarios de estado
    all_checks  = {**diag.statistical_health, **diag.hydraulic_coherence, **diag.well_behavior_coherence}
    total_chk   = len(all_checks)
    failed_chk  = sum(1 for v in all_checks.values() if not v)

    row = {
        "well_id":               WELL_ID,
        "telemetria_id":         telemetria_id,
        "timestamp_evento_utc":  ts_evento,
        "severidad":             severidad,
        "capa_diagnostico":      capa,
        "codigo_alarma":         codigo,
        "health_score_pct":      diag.overall_health_score,
        "status_label":          diag.status,
        "total_checks":          total_chk,
        "failed_checks":         failed_chk,
        "anomalias_json":        anomalias_str,
        "stat_health_json":      json.dumps(diag.statistical_health, ensure_ascii=False),
        "hyd_health_json":       json.dumps(diag.hydraulic_coherence, ensure_ascii=False),
        "well_health_json":      json.dumps(diag.well_behavior_coherence, ensure_ascii=False),
        "audit_record_json":     json.dumps(diag.audit_record, ensure_ascii=False, default=str),
        "p_line_snapshot_psig":  mapped.get("P_line_psig"),
        "t_proc_snapshot_f":     mapped.get("T_proc_F"),
        "q_liquid_snapshot_bpd": mapped.get("Q_liquid_bpd"),
        "q_gas_snapshot_mmscfd": mapped.get("Q_gas_mmscfd"),
        "wc_snapshot_pct":       mapped.get("WaterCut_pct"),
        "app_version":           APP_VERSION,
        "host_registrador":      socket.gethostname(),
        "row_hash_sha256":       row_hash,
    }

    with conn.cursor() as cur:
        cur.execute(SQL_INSERT_ALARMA, row)
    conn.commit()
    logger.warning(
        "ALARMA REGISTRADA | %s | %s | Score=%.1f%% | %d fallas | hash=%s…",
        severidad, capa, diag.overall_health_score, failed_chk, row_hash[:12]
    )


# =========================================================================== #
#  ORQUESTADOR PRINCIPAL                                                         #
# =========================================================================== #

class BackendAutodiagnostico:
    """
    Orquestador del ciclo de adquisición → diagnóstico → persistencia.

    Corre en un hilo daemon de forma que no bloquea la aplicación principal
    ni impide el apagado limpio del proceso padre.
    """

    def __init__(self) -> None:
        # ── Motor de diagnóstico ─────────────────────────────────────────
        envelope = WellOperatingEnvelope(
            well_id                    = WELL_ID,
            api_gravity                = 26.9,
            gas_specific_gravity       = 0.70,
            expected_gor_scf_stb       = 850.0,
            gor_tolerance_pct          = 30.0,
            min_liquid_rate_bpd        = 50.0,
            max_liquid_rate_bpd        = 5000.0,
            min_gas_rate_mmscfd        = 0.1,
            max_gas_rate_mmscfd        = 15.0,
            max_dp_psi                 = 10.0,
            viscosity_transition_cp    = 20.0,
            outlier_zscore_threshold   = 3.5,
            spike_roc_factor           = 3.0,
            # Límites operacionales específicos del pozo FUL-01
            wc_operational_max_pct     = 97.0,   # WC > 97% dispara alarma operacional
            wc_max_delta_per_cycle_pct = 15.0,   # Salto > 15 pp en un ciclo de 2s = anómalo
        )
        self.verifier = MPFMSelfVerification(envelope=envelope, window_size=WINDOW_SIZE_SAMPLES)

        # ── Lector PLC ───────────────────────────────────────────────────
        self.reader = SimPLCReader(ip_address=PLC_IP_ADDRESS)

        # ── Control del hilo ─────────────────────────────────────────────
        self._stop_event = threading.Event()
        self._thread     = threading.Thread(
            target=self._acquisition_loop,
            name="AcquisitionDaemon",
            daemon=True,            # muere automáticamente al cerrar el proceso padre
        )

        # ── Estadísticas en memoria ──────────────────────────────────────
        self._cycle_count     = 0
        self._alarm_count     = 0
        self._last_health     = 100.0
        self._conn: Optional[pymysql.connections.Connection] = None

        # ── Estado de conexión PLC ─────────────────────────────────────
        self._plc_connected:   bool  = False  # True después de la primera lectura OK
        self._plc_last_ok_ts:  float = 0.0    # time.monotonic() de la última lectura exitosa
        self._plc_last_error:  str   = ""     # mensaje del último error de comunicación
        self._state_lock = threading.Lock()   # protege las vars de estado PLC

    # ----------------------------------------------------------------------- #
    def start(self) -> None:
        """Arranca el hilo daemon de adquisición."""
        logger.info("BackendAutodiagnostico v%s arrancando → PLC=%s / DB=%s",
                    APP_VERSION, PLC_IP_ADDRESS, DB_CONFIG["db"])
        self._conn = _get_db_connection()
        logger.info("Conexión a MySQL establecida (host=%s, db=%s).",
                    DB_CONFIG["host"], DB_CONFIG["db"])
        self._thread.start()
        logger.info("Hilo daemon '%s' iniciado (TID=%d).",
                    self._thread.name, self._thread.ident or -1)

    # ----------------------------------------------------------------------- #
    def stop(self, timeout_s: float = 10.0) -> None:
        """Señaliza la detención y espera al hilo."""
        logger.info("Solicitando parada del hilo de adquisición…")
        self._stop_event.set()
        self._thread.join(timeout=timeout_s)
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        logger.info("Backend detenido. Ciclos ejecutados: %d | Alarmas generadas: %d",
                    self._cycle_count, self._alarm_count)

    # ----------------------------------------------------------------------- #
    def _reconnect_db(self) -> None:
        """Reconecta a MySQL si la conexión se perdió."""
        try:
            if self._conn:
                self._conn.ping(reconnect=True)
        except Exception:
            logger.warning("Reconectando a MySQL…")
            try:
                self._conn = _get_db_connection()
            except Exception as exc:
                logger.error("No se pudo reconectar a MySQL: %s", exc)

    # ----------------------------------------------------------------------- #
    def _acquisition_loop(self) -> None:
        """
        Bucle principal de adquisición. Corre indefinidamente en el hilo daemon.

        Ciclo por iteración:
          1. Leer todos los tags del PLC via SimPLCReader.read_all_tags()
          2. Mapear tags al diccionario canónico de add_telemetry()
          3. Alimentar el buffer de MPFMSelfVerification
          4. Ejecutar evaluate() para obtener DiagnosticResult
          5. INSERT en tb_telemetria (siempre)
          6. Si score < HEALTH_ALARM_THRESHOLD o hay anomalías de Capa 1/2/3
             → INSERT en tb_historico_alarmas
          7. time.sleep(2)
        """
        logger.info("Bucle de adquisición iniciado. Intervalo: %.1fs.", ACQUISITION_INTERVAL_S)

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            self._cycle_count += 1

            try:
                # ── 1. Lectura PLC ───────────────────────────────────────
                raw_tags = self.reader.read_all_tags()

                if raw_tags is None:
                    err_msg = (
                        f"PLC inalcanzable ({PLC_IP_ADDRESS}) "
                        f"en ciclo {self._cycle_count}"
                    )
                    with self._state_lock:
                        self._plc_connected  = False
                        self._plc_last_error = err_msg
                    logger.error(
                        "[Ciclo %d] SimPLCReader retornó None. "
                        "PLC inalcanzable (%s). Reintentando en %.0fs.",
                        self._cycle_count, PLC_IP_ADDRESS, ACQUISITION_INTERVAL_S
                    )
                    self._register_system_alarm("PLC inalcanzable – SimPLCReader retornó None")
                    time.sleep(ACQUISITION_INTERVAL_S)
                    continue

                # Lectura exitosa → actualizar estado de conexión
                with self._state_lock:
                    self._plc_connected  = True
                    self._plc_last_ok_ts = time.monotonic()
                    self._plc_last_error = ""

                # ── 1.5 Sincronizar configuración de DB ──────────────────
                self._reconnect_db()
                if self._conn:
                    try:
                        import decimal
                        with self._conn.cursor(pymysql.cursors.DictCursor) as cur:
                            cur.execute(
                                "SELECT * FROM tb_config_pozo WHERE well_id = %s AND is_active = 1 ORDER BY id DESC LIMIT 1",
                                (WELL_ID,)
                            )
                            row = cur.fetchone()
                            if row:
                                valid_keys = self.verifier.env.__dataclass_fields__.keys()
                                for k, v in row.items():
                                    if k in valid_keys:
                                        setattr(self.verifier.env, k, float(v) if isinstance(v, decimal.Decimal) else v)
                    except Exception as exc:
                        logger.warning("[Ciclo %d] Error leyendo tb_config_pozo: %s", self._cycle_count, exc)

                # ── 2. Mapeo de tags ─────────────────────────────────────
                mapped = map_plc_tags_to_telemetry(raw_tags)

                # ── 3. Alimentar buffer de diagnóstico ───────────────────
                # add_telemetry() espera las claves canónicas (P_line_psig, etc.)
                self.verifier.add_telemetry(mapped)

                # ── 4. Evaluar diagnóstico ───────────────────────────────
                try:
                    diag: DiagnosticResult = self.verifier.evaluate()
                except ValueError:
                    # Buffer aún insuficiente (< window_size muestras)
                    logger.debug(
                        "[Ciclo %d] Buffer insuficiente para diagnóstico. "
                        "Acumulando muestras…", self._cycle_count
                    )
                    time.sleep(ACQUISITION_INTERVAL_S)
                    continue

                self._last_health = diag.overall_health_score

                # Resumen de diagnóstico detallado por check individual
                failed_stat  = [k for k, v in diag.statistical_health.items()    if not v]
                failed_hyd   = [k for k, v in diag.hydraulic_coherence.items()   if not v]
                failed_well  = [k for k, v in diag.well_behavior_coherence.items() if not v]
                all_failed   = failed_stat + failed_hyd + failed_well

                logger.info(
                    "[Ciclo %4d] %s | Health=%.1f%% | Anomalías=%d | Checks fallidos: %s",
                    self._cycle_count,
                    diag.status,
                    diag.overall_health_score,
                    len(diag.anomalies_detected),
                    all_failed if all_failed else "ninguno",
                )
                if diag.anomalies_detected:
                    for msg in diag.anomalies_detected:
                        logger.warning("  └ %s", msg)

                # ── 5. Persistir en tb_telemetria (siempre) ──────────────
                self._reconnect_db()
                telemetria_id = _insert_telemetria(self._conn, mapped, diag)

                # ── 6. Evaluar si se debe generar alarma ──────────────────
                #  Condición de disparo (OR):
                #    a) Health Score < HEALTH_ALARM_THRESHOLD  (default 100%)
                #    b) Hay al menos 1 check fallido en Capa 1 (stat_health)
                #    c) Hay al menos 1 check fallido en Capa 2 (hydraulic_coherence)
                #    d) Hay al menos 1 check fallido en Capa 3 (well_behavior_coherence)

                score_alarm  = diag.overall_health_score < HEALTH_ALARM_THRESHOLD
                capa1_alarm  = any(not v for v in diag.statistical_health.values())
                capa2_alarm  = any(not v for v in diag.hydraulic_coherence.values())
                capa3_alarm  = any(not v for v in diag.well_behavior_coherence.values())

                if score_alarm or capa1_alarm or capa2_alarm or capa3_alarm:
                    self._alarm_count += 1
                    severidad, capa, codigo = _classify_alarm(diag)
                    _insert_alarma(
                        self._conn, diag, mapped,
                        telemetria_id, severidad, capa, codigo
                    )

            except pymysql.err.IntegrityError as ik_exc:
                logger.error(
                    "[Ciclo %d] FK constraint al insertar alarma: %s. "
                    "El telemetria_id referenciado puede no existir.",
                    self._cycle_count, ik_exc
                )
                self._reconnect_db()

            except pymysql.err.OperationalError as db_exc:
                logger.error("[Ciclo %d] Error DB: %s", self._cycle_count, db_exc)
                self._reconnect_db()

            except Exception as exc:
                logger.exception("[Ciclo %d] Error inesperado: %s", self._cycle_count, exc)

            finally:
                # ── 7. Respetar el intervalo de 2 segundos ───────────────
                elapsed   = time.monotonic() - cycle_start
                sleep_for = max(0.0, ACQUISITION_INTERVAL_S - elapsed)
                if sleep_for > 0:
                    self._stop_event.wait(timeout=sleep_for)

        logger.info("Bucle de adquisición finalizado.")

    # ----------------------------------------------------------------------- #
    def _register_system_alarm(self, message: str) -> None:
        """
        Registra un error de sistema (PLC unreachable, DB error, etc.)
        en tb_historico_alarmas con capa SISTEMA.
        """
        ts = datetime.datetime.now().isoformat()
        anomalias_str = json.dumps([message], ensure_ascii=False)
        row_hash = _compute_row_hash(WELL_ID, ts, -1.0, anomalias_str)

        row = {
            "well_id":               WELL_ID,
            "telemetria_id":         None,
            "timestamp_evento_utc":  ts,
            "severidad":             "CRITICAL",
            "capa_diagnostico":      "SISTEMA",
            "codigo_alarma":         "ALM-SYS-001",
            "health_score_pct":      -1.0,
            "status_label":          "SISTEMA",
            "total_checks":          0,
            "failed_checks":         0,
            "anomalias_json":        anomalias_str,
            "stat_health_json":      None,
            "hyd_health_json":       None,
            "well_health_json":      None,
            "audit_record_json":     None,
            "p_line_snapshot_psig":  None,
            "t_proc_snapshot_f":     None,
            "q_liquid_snapshot_bpd": None,
            "q_gas_snapshot_mmscfd": None,
            "wc_snapshot_pct":       None,
            "app_version":           APP_VERSION,
            "host_registrador":      socket.gethostname(),
            "row_hash_sha256":       row_hash,
        }

        try:
            self._reconnect_db()
            if self._conn:
                with self._conn.cursor() as cur:
                    cur.execute(SQL_INSERT_ALARMA, row)
                self._conn.commit()
        except Exception as exc:
            logger.error("No se pudo registrar alarma de sistema: %s", exc)

    # ----------------------------------------------------------------------- #
    @property
    def is_alive(self) -> bool:
        """True si el hilo daemon está corriendo."""
        return self._thread.is_alive()

    @property
    def plc_status(self) -> Dict[str, Any]:
        """Estado de conexión con el PLC (thread-safe)."""
        with self._state_lock:
            connected  = self._plc_connected
            last_ok_ts = self._plc_last_ok_ts
            last_error = self._plc_last_error
        now = time.monotonic()
        last_ok_ago = round(now - last_ok_ts, 1) if last_ok_ts > 0 else None
        return {
            "plc_connected":    connected,
            "plc_ip":           PLC_IP_ADDRESS,
            "last_ok_ago_s":    last_ok_ago,
            "last_error":       last_error,
            "backend_alive":    self.is_alive,
        }

    @property
    def stats(self) -> Dict[str, Any]:
        """Estadísticas del backend para monitoreo externo."""
        return {
            "ciclos":       self._cycle_count,
            "alarmas":      self._alarm_count,
            "ultimo_score": self._last_health,
            "hilo_vivo":    self.is_alive,
        }


# =========================================================================== #
#  PUNTO DE ENTRADA / DEMOSTRACIÓN STANDALONE                                   #
# =========================================================================== #

if __name__ == "__main__":
    import signal

    backend = BackendAutodiagnostico()
    backend.start()

    def _graceful_shutdown(signum, frame):
        logger.info("Señal %d recibida. Iniciando parada limpia…", signum)
        backend.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    logger.info(
        "Backend corriendo. Presiona Ctrl+C para detener.\n"
        "PLC : %s\nDB  : %s@%s/%s",
        PLC_IP_ADDRESS,
        DB_CONFIG["user"], DB_CONFIG["host"], DB_CONFIG["db"]
    )

    # Mantener el proceso principal vivo mientras el daemon trabaja
    try:
        while backend.is_alive:
            time.sleep(30)
            s = backend.stats
            logger.info(
                "── STATUS ── Ciclos=%d | Alarmas=%d | ÚltimoScore=%.1f%%",
                s["ciclos"], s["alarmas"], s["ultimo_score"]
            )
    except KeyboardInterrupt:
        backend.stop()
