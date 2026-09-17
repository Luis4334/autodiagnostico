"""
===============================================================================
SISTEMA DE AUTODIAGNÓSTICO Y SELF-VERIFICATION PARA MEDIDOR MULTIFÁSICO VOX-X4
INDUSTRIAL VOX ANALYZER - MÓDULO DE PRUEBA DE POZOS
===============================================================================
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import datetime
import logging
import json

# Configuración de Logging / Audit Trail
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("mpfm_self_verification_audit.log"),
        logging.StreamHandler()
    ]
)

@dataclass
class WellOperatingEnvelope:
    """Envolvente operativa y propiedades base del pozo aforado."""
    well_id: str
    api_gravity: float = 26.9                # °API
    gas_specific_gravity: float = 0.70      # Aire = 1.0
    min_liquid_rate_bpd: float = 50.0       # BPD
    max_liquid_rate_bpd: float = 5000.0     # BPD
    min_gas_rate_mmscfd: float = 0.1        # MMSCFD
    max_gas_rate_mmscfd: float = 15.0       # MMSCFD
    expected_gor_scf_stb: float = 800.0     # RGP esperada
    gor_tolerance_pct: float = 40.0         # Tolerancia en variabilidad RGP (%)
    max_dp_psi: float = 10.0                # Caída máx permitida en skid (psi)
    viscosity_transition_cp: float = 20.0   # Umbral transición Cuña / Laminar (cP)
    outlier_zscore_threshold: float = 3.5   # Umbral Z-score para outlier
    spike_roc_factor: float = 3.0           # Factor k: |Δval| > k*ref_std → spike
    wc_operational_max_pct: float = 97.0    # WC máximo operacional del pozo (%)
    wc_max_delta_per_cycle_pct: float = 15.0  # Cambio máx de WC permitido por ciclo (%)
    dp_min_threshold: float = 0.1           # DP mínimo metrológico (inH2O)
    min_sonar_gas_pct: float = 0.0          # Límite mínimo de GVF
    max_sonar_gas_pct: float = 30.0         # Límite máximo de GVF
    # Umbrales mínimos de variación absoluta para Spike (ROC)
    abs_min_wc_pct: float = 5.0
    abs_min_p_line_psig: float = 15.0
    abs_min_t_proc_f: float = 5.0
    abs_min_dp_wedge_inh2o: float = 20.0
    abs_min_dp_laminar_inh2o: float = 10.0
    abs_min_viscosity_cp: float = 5.0
    abs_min_level_pct: float = 10.0
    abs_min_sonar_gas_pct: float = 5.0


@dataclass
class DiagnosticResult:
    """Estructura de salida del autodiagnóstico."""
    timestamp: str
    overall_health_score: float             # 0.0 a 100.0 %
    status: str                             # 'HEALTHY', 'WARNING', 'CRITICAL'
    statistical_health: Dict[str, bool]
    hydraulic_coherence: Dict[str, bool]
    well_behavior_coherence: Dict[str, bool]
    anomalies_detected: List[str]
    audit_record: Dict[str, any]


class MPFMSelfVerification:
    def __init__(self, envelope: WellOperatingEnvelope, window_size: int = 30):
        """
        :param envelope: Envolvente y características del pozo.
        :param window_size: Tamaño de la ventana móvil de análisis estadístico (muestras).
        """
        self.env = envelope
        self.window_size = window_size
        self.buffer = []
        
    def add_telemetry(self, sample: Dict[str, float]):
        """Agrega una muestra de telemetría proveniente del PLC."""
        sample['timestamp'] = sample.get('timestamp', datetime.datetime.utcnow().isoformat())
        self.buffer.append(sample)
        if len(self.buffer) > self.window_size * 2:
            self.buffer.pop(0)

    # -------------------------------------------------------------------------
    # 1. VERIFICACIÓN ESTADÍSTICA DE SENSORES (Sensor Health)
    # -------------------------------------------------------------------------
    def _verify_sensor_statistics(self, df: pd.DataFrame) -> Tuple[Dict[str, bool], List[str]]:
        """
        Capa 1 — Verificación estadística de sensores:
          1.1  Señal Congelada (Flatline / Frozen Tag)
          1.2  Outlier por Z-Score  (umbral configurable vía WellOperatingEnvelope)
          1.3  Spike por Rate-of-Change  (pico aislado = salto brusco entre muestras
               consecutivas que luego regresa a la línea base)

        Evaluación activa desde la 3ra muestra (no espera window_size completo).
        """
        health = {}
        alerts = []
        critical_tags = [
            'P_line_psig', 'T_proc_F', 'DP_wedge_inH2O', 'DP_laminar_inH2O',
            'WaterCut_pct', 'Viscosity_cP', 'Separator_Level_pct', 'Sonar_EntrainedGas_pct'
        ]
        z_thresh = self.env.outlier_zscore_threshold
        roc_k    = self.env.spike_roc_factor
        # Mínimo absoluto de muestras para análisis: 3 (arranque rápido)
        MIN_SAMPLES = 3

        for tag in critical_tags:
            if tag not in df.columns:
                continue

            # Tomar las últimas window_size muestras, pero evaluar desde MIN_SAMPLES
            series = df[tag].dropna().iloc[-self.window_size:]
            if len(series) < MIN_SAMPLES:
                continue

            std_dev  = series.std()
            mean_val = series.mean()
            last_val = series.iloc[-1]
            prev_val = series.iloc[-2]

            # ── 1.1 Señal Congelada (Flatline) ───────────────────────────────
            # std < 1e-4 con valor real: todas las muestras son idénticas
            if std_dev < 1e-4 and abs(mean_val) > 1e-6:
                health[f"{tag}_frozen"] = False
                alerts.append(
                    f"FALLA CRÍTICA: Sensor '{tag}' congelado — "
                    f"Flatline detectado (std={std_dev:.2e}, valor={last_val:.4f})"
                )
            else:
                health[f"{tag}_frozen"] = True

            # Referencia: todos los puntos EXCEPTO el último
            ref_series = series.iloc[:-1]
            ref_mean   = ref_series.mean()
            ref_std    = ref_series.std() if len(ref_series) > 1 else 0.0

            # ── 1.2 Outlier Z-Score ──────────────────────────────────────────
            # Usa N-1 puntos como referencia para que un spike no enmascare
            # su propio z-score inflando la media
            if ref_std > 1e-4:
                z_score = abs((last_val - ref_mean) / ref_std)
                if z_score > z_thresh:
                    health[f"{tag}_outlier"] = False
                    alerts.append(
                        f"ALERTA: Outlier detectado en '{tag}' "
                        f"(Z-score={z_score:.2f} > {z_thresh}, "
                        f"val={last_val:.3f}, media_ref={ref_mean:.3f})"
                    )
                else:
                    health[f"{tag}_outlier"] = True
            else:
                health[f"{tag}_outlier"] = True

            # ── 1.3 Spike por Rate-of-Change ─────────────────────────────────
            # |Δ| entre última y penúltima muestra vs. dispersión histórica.
            # Lógica simplificada: si |Δ| > roc_k × ref_std  O  > roc_k × abs_min,
            # donde abs_min evita falsos negativos cuando la señal era completamente estable.
            roc = abs(last_val - prev_val)
            # Umbral adaptativo: el mayor entre roc_k*ref_std y un mínimo absoluto por tag
            abs_min_thresholds = {
                'WaterCut_pct':          self.env.abs_min_wc_pct,
                'P_line_psig':          self.env.abs_min_p_line_psig,
                'T_proc_F':              self.env.abs_min_t_proc_f,
                'DP_wedge_inH2O':       self.env.abs_min_dp_wedge_inh2o,
                'DP_laminar_inH2O':     self.env.abs_min_dp_laminar_inh2o,
                'Viscosity_cP':          self.env.abs_min_viscosity_cp,
                'Separator_Level_pct':  self.env.abs_min_level_pct,
                'Sonar_EntrainedGas_pct': self.env.abs_min_sonar_gas_pct,
            }
            abs_min = abs_min_thresholds.get(tag, 5.0)
            roc_threshold = max(roc_k * ref_std, abs_min) if ref_std > 1e-6 else abs_min

            if roc > roc_threshold:
                health[f"{tag}_spike"] = False
                alerts.append(
                    f"ALERTA CAMBIO BRUSCO: '{tag}' "
                    f"(Delta={roc:.3f} > umbral={roc_threshold:.3f}; "
                    f"val={last_val:.3f} <- anterior={prev_val:.3f})"
                )
            else:
                health[f"{tag}_spike"] = True

        return health, alerts

    # -------------------------------------------------------------------------
    # 2. COHERENCIA HIDRÁULICA DEL MEDIDOR MULTIFÁSICO
    # -------------------------------------------------------------------------
    def _verify_hydraulic_coherence(self, current: Dict[str, float]) -> Tuple[Dict[str, bool], List[str]]:
        coherence = {}
        alerts = []
        
        visc = current.get('Viscosity_cP', 10.0)
        dp_wedge = current.get('DP_wedge_inH2O', 0.0)
        dp_laminar = current.get('DP_laminar_inH2O', 0.0)
        q_liquid_bpd = current.get('Q_liquid_bpd', 0.0)
        level = current.get('Separator_Level_pct', 50.0)
        lcv_pos = current.get('LCV_Position_pct', 50.0)
        pcv_pos = current.get('PCV_Position_pct', 50.0)
        sonar_gas = current.get('Sonar_EntrainedGas_pct', 0.0)
        dp_total = current.get('Skid_Total_DP_psi', 2.0)

        # 2.1 Caída de Presión Máxima admisible en el Skid (≤ 10 psi)
        if dp_total > self.env.max_dp_psi:
            coherence['dp_total_ok'] = False
            alerts.append(f"ALERTA HIDRÁULICA: Caída de presión total excesiva ({dp_total:.2f} psi > {self.env.max_dp_psi} psi)")
        else:
            coherence['dp_total_ok'] = True

        # 2.2 Validación Cruzada: Medidor Cuña vs. Laminar según Viscosidad
        if visc > self.env.viscosity_transition_cp:
            # Régimen Viscoso / Laminar: Hagen-Poiseuille domina (DP_laminar debe ser representativo)
            if dp_laminar <= self.env.dp_min_threshold and q_liquid_bpd > self.env.min_liquid_rate_bpd:
                coherence['meter_selection_valid'] = False
                alerts.append(f"INCOHERENCIA METROLÓGICA: Alta viscosidad ({visc:.1f} cP) sin respuesta en medidor laminar")
            else:
                coherence['meter_selection_valid'] = True
        else:
            # Régimen Turbulento / Baja Viscosidad: Ecuación de Bernoulli / Cuña
            if dp_wedge <= self.env.dp_min_threshold and q_liquid_bpd > self.env.min_liquid_rate_bpd:
                coherence['meter_selection_valid'] = False
                alerts.append(f"INCOHERENCIA METROLÓGICA: Baja viscosidad ({visc:.1f} cP) sin respuesta en medidor Cuña (Wedge)")
            else:
                coherence['meter_selection_valid'] = True

        # 2.3 Coherencia de Control de Nivel vs. Válvula Líquida (LCV)
        # Si el nivel está muy alto y la válvula está cerrada (o viceversa), existe falla de control o bloqueo
        if level > 85.0 and lcv_pos < 10.0:
            coherence['level_control_coherence'] = False
            alerts.append(f"FALLA DE PROCESO: Nivel en separador crítico ({level:.1f}%) con LCV cerrada ({lcv_pos:.1f}%)")
        elif level < 15.0 and lcv_pos > 90.0:
            coherence['level_control_coherence'] = False
            alerts.append(f"FALLA DE PROCESO: Nivel en separador muy bajo ({level:.1f}%) con LCV abierta ({lcv_pos:.1f}%)")
        else:
            coherence['level_control_coherence'] = True

        # 2.4 Límite del Sonar de Gas Arrastrado
        if sonar_gas < self.env.min_sonar_gas_pct or sonar_gas > self.env.max_sonar_gas_pct:
            coherence['sonar_gas_ok'] = False
            alerts.append(f"ALERTA INSTRUMENTO: Fracción de gas arrastrado (SONAR) fuera de rango físico ({sonar_gas:.2f}%)")
        else:
            coherence['sonar_gas_ok'] = True

        return coherence, alerts

    # -------------------------------------------------------------------------
    # 3. COHERENCIA CON EL COMPORTAMIENTO DEL POZO (Well Consistency)
    # -------------------------------------------------------------------------
    def _verify_well_behavior(self, current: Dict[str, float]) -> Tuple[Dict[str, bool], List[str]]:
        coherence = {}
        alerts = []

        q_oil        = current.get('Q_oil_bpd', 0.0)
        q_liquid     = current.get('Q_liquid_bpd', 0.0)
        q_gas_mmscfd = current.get('Q_gas_mmscfd', 0.0)
        q_diluent    = current.get('Q_diluent_bpd', 0.0)
        wc           = current.get('WaterCut_pct', 0.0)
        p_line       = current.get('P_line_psig', 240.0)

        # 3.1 Rango de Corte de Agua Físico (0 - 100%)
        if wc < 0.0 or wc > 100.0:
            coherence['water_cut_range_valid'] = False
            alerts.append(f"FALLA ANALIZADOR: Corte de agua fuera de rango físico ({wc:.2f}%)")
        else:
            coherence['water_cut_range_valid'] = True

        # 3.2 Límite Operacional de Corte de Agua (umbral de pozo configurable)
        # Un WC dentro del rango físico puede ser igualmente anormal para este pozo
        wc_max_op = self.env.wc_operational_max_pct
        if coherence['water_cut_range_valid'] and wc > wc_max_op:
            coherence['water_cut_operational'] = False
            alerts.append(
                f"ALARMA WC: Corte de agua ({wc:.1f}%) supera límite operacional del pozo "
                f"({wc_max_op:.1f}%) — posible inundación de pozo o falla de analizador"
            )
        else:
            coherence['water_cut_operational'] = True

        # 3.3 Tasa de cambio brusca de WC entre ciclos consecutivos
        if len(self.buffer) >= 2:
            prev_sample = self.buffer[-2]
            wc_prev = prev_sample.get('WaterCut_pct', wc)
            wc_delta = abs(wc - wc_prev)
            if wc_delta > self.env.wc_max_delta_per_cycle_pct:
                coherence['water_cut_rate_of_change'] = False
                alerts.append(
                    f"ALERTA WC BRUSCO: Cambio subito de corte de agua "
                    f"(Delta={wc_delta:.1f}% en un ciclo; actual={wc:.1f}%, anterior={wc_prev:.1f}%) "
                    f"-- umbral configurado: {self.env.wc_max_delta_per_cycle_pct:.1f}%"
                )
            else:
                coherence['water_cut_rate_of_change'] = True
        else:
            coherence['water_cut_rate_of_change'] = True

        # 3.4 Balance de Tasa de Crudo / Agua / Líquido Total
        expected_oil = (q_liquid * (1.0 - (wc / 100.0))) - q_diluent
        if q_liquid > self.env.min_liquid_rate_bpd:
            diff_oil_pct = abs(expected_oil - q_oil) / q_liquid * 100.0
            if diff_oil_pct > 5.0:  # Tolerancia máxima balance de fases líquidas
                coherence['liquid_mass_balance'] = False
                alerts.append(
                    f"DISCREPANCIA BALANCE: Q_oil ({q_oil:.1f} BPD) difiere del cálculo "
                    f"por WC ({expected_oil:.1f} BPD) en {diff_oil_pct:.1f}%"
                )
            else:
                coherence['liquid_mass_balance'] = True
        else:
            coherence['liquid_mass_balance'] = True

        # 3.5 Coherencia RGP / GOR vs. Histórico del Pozo
        if q_oil > 10.0:
            gor_calculated = (q_gas_mmscfd * 1e6) / q_oil
            gor_dev_pct = abs(gor_calculated - self.env.expected_gor_scf_stb) / self.env.expected_gor_scf_stb * 100.0
            if gor_dev_pct > self.env.gor_tolerance_pct:
                coherence['gor_stability'] = False
                alerts.append(
                    f"ANOMALÍA DE POZO: RGP calculada ({gor_calculated:.1f} scf/STB) "
                    f"desfasada de la esperada ({self.env.expected_gor_scf_stb:.1f} scf/STB) "
                    f"en {gor_dev_pct:.1f}%"
                )
            else:
                coherence['gor_stability'] = True
        else:
            coherence['gor_stability'] = True

        # 3.6 Presión de Flujo en Línea vs Rango Operacional
        if p_line < 30.0 or p_line > 350.0:
            coherence['p_line_safe'] = False
            alerts.append(
                f"ALERTA PRESIÓN: Presión de línea ({p_line:.1f} psig) "
                f"fuera de ventana de aforo del pozo"
            )
        else:
            coherence['p_line_safe'] = True

        return coherence, alerts

    # -------------------------------------------------------------------------
    # EJECUTOR PRINCIPAL DE AUTODIAGNÓSTICO
    # -------------------------------------------------------------------------
    def evaluate(self) -> DiagnosticResult:
        if not self.buffer:
            raise ValueError("Buffer de datos vacío.")

        df = pd.DataFrame(self.buffer)
        current = self.buffer[-1]
        
        stat_health, stat_alerts = self._verify_sensor_statistics(df)
        hyd_health, hyd_alerts = self._verify_hydraulic_coherence(current)
        well_health, well_alerts = self._verify_well_behavior(current)
        
        all_alerts = stat_alerts + hyd_alerts + well_alerts
        
        # Ponderación de Salud Global (Health Score)
        total_checks = len(stat_health) + len(hyd_health) + len(well_health)
        passed_checks = sum(stat_health.values()) + sum(hyd_health.values()) + sum(well_health.values())
        
        health_score = (passed_checks / total_checks * 100.0) if total_checks > 0 else 100.0
        
        if health_score >= 90.0 and len(all_alerts) == 0:
            status = 'HEALTHY'
        elif health_score >= 70.0:
            status = 'WARNING'
        else:
            status = 'CRITICAL'

        # Registro Audit Trail Inviolable
        audit_record = {
            "timestamp": current.get('timestamp'),
            "well_id": self.env.well_id,
            "health_score": round(health_score, 2),
            "status": status,
            "q_liq_bpd": current.get('Q_liquid_bpd'),
            "q_gas_mmscfd": current.get('Q_gas_mmscfd'),
            "wc_pct": current.get('WaterCut_pct'),
            "active_alerts_count": len(all_alerts)
        }

        if status != 'HEALTHY':
            logging.warning(f"Diagnóstico Pozo {self.env.well_id} -> {status} (Score: {health_score:.1f}%). Alertas: {all_alerts}")
        else:
            logging.info(f"Diagnóstico Pozo {self.env.well_id} -> Salud OK ({health_score:.1f}%).")

        return DiagnosticResult(
            timestamp=current.get('timestamp'),
            overall_health_score=round(health_score, 2),
            status=status,
            statistical_health=stat_health,
            hydraulic_coherence=hyd_health,
            well_behavior_coherence=well_health,
            anomalies_detected=all_alerts,
            audit_record=audit_record
        )


# =============================================================================
# PRUEBA Y SIMULACIÓN DE CASO DE CAMPO
# =============================================================================
if __name__ == "__main__":
    print("--- INICIANDO SIMULACIÓN DE SELF-VERIFICATION EN MPFM VOX-X4 ---")
    
    # 1. Definir envolvente del Pozo
    pozo_furrial = WellOperatingEnvelope(
        well_id="FUL-01",
        api_gravity=26.9,
        expected_gor_scf_stb=850.0,
        gor_tolerance_pct=30.0,
        viscosity_transition_cp=15.0
    )
    
    verifier = MPFMSelfVerification(envelope=pozo_furrial, window_size=20)
    
    # 2. Generar serie sintética con transición de estado normal a anomalía
    np.random.seed(42)
    base_time = datetime.datetime.now()
    
    for i in range(25):
        t_stamp = (base_time + datetime.timedelta(seconds=i*10)).isoformat()
        
        # Inyectar una falla inducida en la muestra 22 (Sensor de corte de agua congelado y salto en GOR)
        is_fault = (i >= 22)
        
        sample_data = {
            'timestamp': t_stamp,
            'P_line_psig': float(np.random.normal(240.0, 2.0)),
            'T_proc_F': float(np.random.normal(135.0, 1.0)),
            'DP_wedge_inH2O': float(np.random.normal(120.0, 3.0)),
            'DP_laminar_inH2O': float(np.random.normal(5.0, 0.5)),
            'WaterCut_pct': 65.0 if is_fault else float(np.random.normal(65.0, 0.8)),
            'Viscosity_cP': float(np.random.normal(11.6, 0.3)),
            'Separator_Level_pct': 92.0 if is_fault else float(np.random.normal(50.0, 4.0)),
            'LCV_Position_pct': 5.0 if is_fault else float(np.random.normal(52.0, 3.0)),
            'PCV_Position_pct': float(np.random.normal(48.0, 2.0)),
            'Sonar_EntrainedGas_pct': float(np.random.normal(4.2, 0.4)),
            'Skid_Total_DP_psi': float(np.random.normal(2.5, 0.2)),
            'Q_liquid_bpd': float(np.random.normal(3200.0, 50.0)),
            'Q_oil_bpd': float(np.random.normal(1120.0, 20.0)),
            'Q_gas_mmscfd': 2.8 if is_fault else float(np.random.normal(0.95, 0.05)),
        }
        
        verifier.add_telemetry(sample_data)
        
        if i >= 20:
            diag = verifier.evaluate()
            print(f"\n[Iteración {i+1}] Estado: {diag.status} | Score: {diag.overall_health_score}%")
            if diag.anomalies_detected:
                print(" -> Fallas Detectadas:")
                for anomaly in diag.anomalies_detected:
                    print(f"    * {anomaly}")