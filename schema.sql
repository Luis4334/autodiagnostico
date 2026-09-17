-- =============================================================================
--  SISTEMA DE AUTODIAGNÓSTICO CONTINUO - MEDIDOR MULTIFÁSICO VOX-X4
--  Script DDL para MySQL / XAMPP
--  Autor  : Senior Automation Engineer
--  Versión: 1.0.0
--  Fecha  : 2026-09-14
-- =============================================================================
--  Tablas creadas:
--    1. tb_config_pozo        → Parámetros de pozo, gravedad API, RGP y umbrales
--    2. tb_telemetria         → Variables en tiempo real del PLC (ciclo 2 s)
--    3. tb_historico_alarmas  → Registro de auditoría inviolable (append-only)
-- =============================================================================

CREATE DATABASE IF NOT EXISTS db_autodiagnostico
    CHARACTER SET utf8mb4
    COLLATE       utf8mb4_unicode_ci;

USE db_autodiagnostico;

-- ---------------------------------------------------------------------------
-- 0. Usuario de aplicación (mínimo privilegio)
-- ---------------------------------------------------------------------------
-- Ejecutar como root solo la primera vez:
CREATE USER IF NOT EXISTS 'mpfm_app'@'localhost' IDENTIFIED BY 'VoxX4_S3cure!';
GRANT SELECT, INSERT, UPDATE ON db_autodiagnostico.* TO 'mpfm_app'@'localhost';
-- REVOKE DELETE es innecesario: DELETE nunca fue concedido en el GRANT anterior.
-- MySQL error #1147 si se intenta revocar un privilegio no otorgado.
FLUSH PRIVILEGES;

-- =============================================================================
-- TABLA 1: tb_config_pozo
--   Almacena el perfil operativo del pozo y los umbrales de autodiagnóstico.
--   Una fila activa por pozo (is_active = 1). Se versionan por updated_at.
-- =============================================================================
CREATE TABLE IF NOT EXISTS tb_config_pozo (
    id                          INT UNSIGNED    NOT NULL AUTO_INCREMENT,

    -- Identificación
    well_id                     VARCHAR(32)     NOT NULL COMMENT 'Código único del pozo (ej. FUL-01)',
    description                 VARCHAR(255)             COMMENT 'Descripción del pozo / campo',

    -- Propiedades PVT
    api_gravity                 DECIMAL(6,3)    NOT NULL DEFAULT 26.900  COMMENT 'Gravedad API del crudo (°API)',
    gas_specific_gravity        DECIMAL(6,4)    NOT NULL DEFAULT 0.7000  COMMENT 'Gravedad específica del gas (aire=1)',
    expected_gor_scf_stb        DECIMAL(10,2)   NOT NULL DEFAULT 800.00  COMMENT 'RGP esperada de diseño (scf/STB)',
    gor_tolerance_pct           DECIMAL(5,2)    NOT NULL DEFAULT 40.00   COMMENT 'Tolerancia de desviación de RGP (%)',

    -- Caudales operativos
    min_liquid_rate_bpd         DECIMAL(10,2)   NOT NULL DEFAULT 50.00   COMMENT 'Tasa mínima de líquido (BPD)',
    max_liquid_rate_bpd         DECIMAL(10,2)   NOT NULL DEFAULT 5000.00 COMMENT 'Tasa máxima de líquido (BPD)',
    min_gas_rate_mmscfd         DECIMAL(8,4)    NOT NULL DEFAULT 0.1000  COMMENT 'Tasa mínima de gas (MMSCFD)',
    max_gas_rate_mmscfd         DECIMAL(8,4)    NOT NULL DEFAULT 15.0000 COMMENT 'Tasa máxima de gas (MMSCFD)',

    -- Umbrales hidráulicos y metrológicos
    max_dp_psi                  DECIMAL(7,3)    NOT NULL DEFAULT 10.000  COMMENT 'Caída de presión máxima en skid (psi)',
    viscosity_transition_cp     DECIMAL(7,3)    NOT NULL DEFAULT 20.000  COMMENT 'Viscosidad de transición Cuña/Laminar (cP)',
    max_sonar_gas_pct           DECIMAL(5,2)    NOT NULL DEFAULT 30.00   COMMENT 'Límite superior de GVF por SONAR (%)',

    -- Umbrales de presión de línea
    p_line_min_psig             DECIMAL(8,2)    NOT NULL DEFAULT 30.00   COMMENT 'Presión mínima de aforo (psig)',
    p_line_max_psig             DECIMAL(8,2)    NOT NULL DEFAULT 350.00  COMMENT 'Presión máxima de aforo (psig)',

    -- Parámetros adicionales configurables
    dp_min_threshold            DECIMAL(7,3)    NOT NULL DEFAULT 0.100   COMMENT 'Diferencial de presión mínimo (inH2O)',
    min_sonar_gas_pct           DECIMAL(5,2)    NOT NULL DEFAULT 0.00    COMMENT 'Límite inferior de GVF SONAR (%)',
    wc_operational_max_pct      DECIMAL(5,2)    NOT NULL DEFAULT 97.00   COMMENT 'WC máximo operacional (%)',
    wc_max_delta_per_cycle_pct  DECIMAL(5,2)    NOT NULL DEFAULT 15.00   COMMENT 'Cambio máx WC por ciclo (%)',

    -- Parámetros estadísticos (ventana móvil)
    window_size_samples         SMALLINT        NOT NULL DEFAULT 30       COMMENT 'Muestras en ventana de análisis estadístico',
    frozen_tag_std_threshold    DOUBLE          NOT NULL DEFAULT 1e-4     COMMENT 'Std mínima para considerar señal viva',
    outlier_zscore_threshold    DOUBLE          NOT NULL DEFAULT 3.5      COMMENT 'Z-score máximo antes de outlier',

    -- Estado del registro
    is_active                   TINYINT(1)      NOT NULL DEFAULT 1        COMMENT '1 = configuración activa del pozo',
    created_at                  TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    KEY idx_well_id (well_id)
) ENGINE=InnoDB
  COMMENT='Perfil operativo y umbrales de autodiagnóstico por pozo.';

-- Registro inicial para el pozo de prueba
INSERT INTO tb_config_pozo (
    well_id, description,
    api_gravity, gas_specific_gravity,
    expected_gor_scf_stb, gor_tolerance_pct,
    min_liquid_rate_bpd, max_liquid_rate_bpd,
    min_gas_rate_mmscfd, max_gas_rate_mmscfd,
    max_dp_psi, viscosity_transition_cp,
    dp_min_threshold, min_sonar_gas_pct,
    wc_operational_max_pct, wc_max_delta_per_cycle_pct
) VALUES (
    'FUL-01', 'Pozo Furrial 01 - Bloque Norte',
    26.900, 0.7000,
    850.00, 30.00,
    50.00, 5000.00,
    0.1000, 15.0000,
    10.000, 15.000,
    0.100, 0.00,
    97.00, 15.00
);


-- =============================================================================
-- TABLA 2: tb_telemetria
--   Recibe todas las variables del PLC cada 2 segundos.
--   Particionada por mes (RANGE sobre UNIX_TIMESTAMP) para rendimiento.
-- =============================================================================
CREATE TABLE IF NOT EXISTS tb_telemetria (
    id                          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    -- Contexto
    well_id                     VARCHAR(32)     NOT NULL                  COMMENT 'FK → tb_config_pozo.well_id',
    plc_ip                      VARCHAR(45)     NOT NULL DEFAULT '172.17.32.220',
    timestamp_utc               DATETIME(3)     NOT NULL DEFAULT (UTC_TIMESTAMP(3)) COMMENT 'Marca de tiempo UTC con ms',
    health_score_pct            DECIMAL(6,3)    NOT NULL DEFAULT 100.000  COMMENT 'Score global del ciclo (%)',
    status_label                VARCHAR(16)     NOT NULL DEFAULT 'HEALTHY' COMMENT 'HEALTHY / WARNING / CRITICAL',

    -- Variables de Presion
    p_gas_psig                  DECIMAL(10,4)            COMMENT 'Presión de gas (tag: P_Gas) [psig]',
    p_oil_psig                  DECIMAL(10,4)            COMMENT 'Presión de aceite (tag: P_Oil) [psig]',
    presion_entrada_psig        DECIMAL(10,4)            COMMENT 'Presión de entrada (tag: PRESION_ENTRADA) [psig]',
    pdt_01_psi                  DECIMAL(10,4)            COMMENT 'Transmisor diferencial PDT-01 [psi]',
    pdt_03_psi                  DECIMAL(10,4)            COMMENT 'Transmisor diferencial PDT-03 [psi]',
    press_pid_cv_pct            DECIMAL(7,3)             COMMENT 'CV de PID de presión [%]',

    -- Variables de Temperatura
    t_gas_c                     DECIMAL(8,4)             COMMENT 'Temperatura del gas (tag: T_GAS) [C]',
    t_oil_c                     DECIMAL(8,4)             COMMENT 'Temperatura del aceite (tag: T_Oil_C) [C]',
    t_oil_f                     DECIMAL(8,4)             COMMENT 'Temperatura del aceite (tag: T_Oil_F) [F]',
    coriolis_temp_c             DECIMAL(8,4)             COMMENT 'Temperatura Coriolis [C]',

    -- Caidas Diferenciales de Presion
    dp_w_inh2o                  DECIMAL(10,4)            COMMENT 'DP medidor cuña / Wedge (tag: DP_W) [inH2O]',
    dp_l_inh2o                  DECIMAL(10,4)            COMMENT 'DP medidor laminar (tag: DP_L) [inH2O]',
    dp_simeflum_inh2o           DECIMAL(10,4)            COMMENT 'DP Simeflum (tag: DP_Simeflum) [inH2O]',

    -- Nivel y Control
    lit_001_pct                 DECIMAL(7,3)             COMMENT 'Nivel separador LIT-001 (tag: LIT_001) [%]',
    level_pid_cv_pct            DECIMAL(7,3)             COMMENT 'CV de PID de nivel [%]',
    transmisor_baja             DECIMAL(10,4)            COMMENT 'Transmisor rango bajo (tag: transmisor_baja)',

    -- Corte de Agua y Viscosidad
    wc_pct                      DECIMAL(7,4)             COMMENT 'Corte de agua medido (tag: WC) [%]',
    wc_sw                       TINYINT(1)               COMMENT 'Switch WC activado',
    v_oil_medida_cp             DECIMAL(10,4)            COMMENT 'Viscosidad medida del aceite (tag: v_oil_medida) [cP]',
    miu_oil_cp                  DECIMAL(10,4)            COMMENT 'Viscosidad calculada mu (tag: miu_Oil) [cP]',

    -- Fraccion de Gas Vacio (GVF / SONAR)
    gvoidf_pct                  DECIMAL(7,4)             COMMENT 'Gas Void Fraction SONAR (tag: GVoidF) [%]',
    gvf_sw                      TINYINT(1)               COMMENT 'Switch GVF activado',

    -- Caudales Medidos (PLC)
    q_liquido_bpd               DECIMAL(12,4)            COMMENT 'Caudal total liquido medido (tag: Q_Liquido) [BPD]',
    q_crudo_bpd                 DECIMAL(12,4)            COMMENT 'Caudal de crudo medido (tag: Q_Crudo) [BPD]',
    q_w_bpd                     DECIMAL(12,4)            COMMENT 'Caudal de agua medido (tag: Q_W) [BPD]',
    q_gat_bpd                   DECIMAL(12,4)            COMMENT 'Caudal de gas asociado total (tag: Q_gat) [BPD]',
    q_gas_std                   DECIMAL(12,4)            COMMENT 'Caudal de gas en condiciones std (tag: Q_gas_STD)',
    q_gas_t_sc                  DECIMAL(12,4)            COMMENT 'Caudal de gas a T y Psc (tag: Program:Caudal.Q_gas_T_sc)',

    -- Caudales Estimados
    qb_liquido_estimado_bpd     DECIMAL(12,4)            COMMENT 'Caudal liquido estimado [BPD]',
    q_crudo_estimado_bpd        DECIMAL(12,4)            COMMENT 'Caudal crudo estimado [BPD]',
    qb_diluente_estimado_bpd    DECIMAL(12,4)            COMMENT 'Caudal diluente estimado [BPD]',
    q_w_estimado_bpd            DECIMAL(12,4)            COMMENT 'Caudal agua estimado [BPD]',
    q_gat_estimado_bpd          DECIMAL(12,4)            COMMENT 'Caudal gas asociado estimado [BPD]',

    -- Coriolis
    coriolis_density_kgm3       DECIMAL(10,4)            COMMENT 'Densidad Coriolis [kg/m3]',
    coriolis_vol_flow_m3h       DECIMAL(12,4)            COMMENT 'Caudal volumetrico Coriolis [m3/h]',
    caudal_diluente_bm          DECIMAL(12,4)            COMMENT 'Caudal de diluente medido (tag: caudal_diluente_BM)',

    -- Metadata de Estado del PLC
    numero_prueba               INT UNSIGNED             COMMENT 'Numero de prueba de pozo (tag: Numero_Prueba)',
    tipo_equipo                 VARCHAR(64)              COMMENT 'Tipo de equipo PLC (tag: TIPO_EQUIPO)',
    prueba_estatus              TINYINT(1)               COMMENT 'Estatus de prueba (tag: Program:Prueba.ESTATUS)',
    sw_dil_medido_calc          TINYINT(1)               COMMENT 'Switch diluente medido/calculado',
    vi_sw                       TINYINT(1)               COMMENT 'Switch de viscosimetro en linea (tag: VI_SW)',
    deshabilita_pid             TINYINT(1)               COMMENT 'Flag inhibicion de PIDs',
    apertura_valvula_pct        DECIMAL(7,3)             COMMENT 'Apertura de valvula [%]',
    abrir_valvula_man           TINYINT(1)               COMMENT 'Comando apertura manual valvula',
    abrir_valvula_auto          TINYINT(1)               COMMENT 'Comando apertura automatica valvula',
    status_error_caudal         TINYINT(1)               COMMENT 'Error de caudal neto diluente',
    man_pc_pct                  DECIMAL(7,3)             COMMENT 'Salida manual PID presion [%]',
    man_lc_pct                  DECIMAL(7,3)             COMMENT 'Salida manual PID nivel [%]',

    PRIMARY KEY (id),
    KEY idx_well_ts    (well_id, timestamp_utc),
    KEY idx_ts         (timestamp_utc),
    KEY idx_health     (health_score_pct),
    KEY idx_status     (status_label)
) ENGINE=InnoDB
  ROW_FORMAT=COMPRESSED
  COMMENT='Telemetria en tiempo real del PLC. Ciclo de 2 segundos. NO MODIFICAR MANUALMENTE.';


-- =============================================================================
-- TABLA 3: tb_historico_alarmas
--   Registro de auditoria inviolable (append-only).
--   RESTRICCION: Ningun usuario de aplicacion tiene permiso de UPDATE/DELETE.
--   Cada fila representa un evento de diagnostico con salud degradada o anomalia.
-- =============================================================================
CREATE TABLE IF NOT EXISTS tb_historico_alarmas (
    id                          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    -- Contexto de la alarma
    well_id                     VARCHAR(32)     NOT NULL                  COMMENT 'Pozo origen de la alarma',
    telemetria_id               BIGINT UNSIGNED          DEFAULT NULL     COMMENT 'FK → tb_telemetria.id del ciclo que origino la alarma',
    timestamp_evento_utc        DATETIME(3)     NOT NULL DEFAULT (UTC_TIMESTAMP(3)) COMMENT 'Momento exacto de deteccion (UTC)',

    -- Clasificacion
    severidad                   ENUM('INFO','WARNING','CRITICAL','FATAL') NOT NULL DEFAULT 'WARNING'
                                                                          COMMENT 'Nivel de severidad segun Health Score y capa de falla',
    capa_diagnostico            ENUM('CAPA_1','CAPA_2','CAPA_3','SISTEMA') NOT NULL
                                                                          COMMENT 'CAPA_1=Sensores | CAPA_2=Hidraulica | CAPA_3=Comportamiento de Pozo | SISTEMA=Errores de aplicacion',
    codigo_alarma               VARCHAR(32)     NOT NULL DEFAULT 'ALM-000' COMMENT 'Codigo unico legible por sistema (ej. ALM-FROZEN-001)',

    -- Snapshot del estado del medidor en el momento del evento
    health_score_pct            DECIMAL(6,3)    NOT NULL                  COMMENT 'Health Score en el instante del evento (%)',
    status_label                VARCHAR(16)     NOT NULL                  COMMENT 'HEALTHY / WARNING / CRITICAL en el momento del evento',
    total_checks                SMALLINT        NOT NULL DEFAULT 0         COMMENT 'Total de verificaciones realizadas en el ciclo',
    failed_checks               SMALLINT        NOT NULL DEFAULT 0         COMMENT 'Verificaciones fallidas en el ciclo',

    -- Payload de anomalias en JSON (esquema versionado)
    anomalias_json              JSON            NOT NULL                  COMMENT 'Array JSON con textos de cada anomalia detectada en el ciclo',
    stat_health_json            JSON                     DEFAULT NULL     COMMENT 'Snapshot del diccionario stat_health (Capa 1)',
    hyd_health_json             JSON                     DEFAULT NULL     COMMENT 'Snapshot del diccionario hyd_health (Capa 2)',
    well_health_json            JSON                     DEFAULT NULL     COMMENT 'Snapshot del diccionario well_health (Capa 3)',
    audit_record_json           JSON                     DEFAULT NULL     COMMENT 'Audit record completo del DiagnosticResult',

    -- Snapshot de variables criticas del PLC en el momento del evento
    p_line_snapshot_psig        DECIMAL(10,4)            DEFAULT NULL,
    t_proc_snapshot_f           DECIMAL(8,4)             DEFAULT NULL,
    q_liquid_snapshot_bpd       DECIMAL(12,4)            DEFAULT NULL,
    q_gas_snapshot_mmscfd       DECIMAL(12,6)            DEFAULT NULL,
    wc_snapshot_pct             DECIMAL(7,4)             DEFAULT NULL,

    -- Trazabilidad del sistema (no editable)
    app_version                 VARCHAR(32)     NOT NULL DEFAULT '1.0.0'  COMMENT 'Version del backend de autodiagnostico',
    host_registrador            VARCHAR(128)             DEFAULT NULL     COMMENT 'Hostname del servidor que registro la alarma',

    -- INTEGRIDAD: Fila firmada con hash SHA-256 de los datos del evento.
    -- Se calcula en Python antes del INSERT para detectar manipulaciones posteriores.
    row_hash_sha256             CHAR(64)        NOT NULL                  COMMENT 'SHA-256(well_id|timestamp_evento_utc|health_score_pct|anomalias_json)',

    PRIMARY KEY (id),
    KEY idx_well_ts_ev  (well_id, timestamp_evento_utc),
    KEY idx_severidad   (severidad),
    KEY idx_capa        (capa_diagnostico),
    KEY idx_health_ev   (health_score_pct),
    KEY idx_telemetria  (telemetria_id),

    CONSTRAINT fk_alarma_telemetria
        FOREIGN KEY (telemetria_id)
        REFERENCES tb_telemetria (id)
        ON DELETE SET NULL
        ON UPDATE CASCADE
) ENGINE=InnoDB
  COMMENT='Registro de auditoria inviolable. PROHIBIDO UPDATE/DELETE. Append-only enforced por permisos de DB.';


-- =============================================================================
-- VISTAS DE EXPLOTACION
-- =============================================================================

-- Vista de las ultimas 100 lecturas de telemetria
CREATE OR REPLACE VIEW vw_telemetria_reciente AS
    SELECT
        id, well_id, timestamp_utc, health_score_pct, status_label,
        p_gas_psig, t_oil_f, wc_pct, gvoidf_pct,
        q_liquido_bpd, q_crudo_bpd, q_w_bpd, q_gas_std,
        dp_w_inh2o, dp_l_inh2o, lit_001_pct
    FROM tb_telemetria
    ORDER BY id DESC
    LIMIT 100;

-- Vista de alarmas activas (ultimas 24 h) con severidad CRITICAL o FATAL
CREATE OR REPLACE VIEW vw_alarmas_criticas AS
    SELECT
        id, well_id, timestamp_evento_utc, severidad, capa_diagnostico,
        codigo_alarma, health_score_pct, status_label,
        JSON_LENGTH(anomalias_json) AS num_anomalias,
        anomalias_json
    FROM tb_historico_alarmas
    WHERE timestamp_evento_utc >= UTC_TIMESTAMP() - INTERVAL 24 HOUR
      AND severidad IN ('CRITICAL', 'FATAL')
    ORDER BY timestamp_evento_utc DESC;

-- Vista de Health Score promedio por hora (ultimas 48 h)
CREATE OR REPLACE VIEW vw_health_score_horario AS
    SELECT
        well_id,
        DATE_FORMAT(timestamp_utc, '%Y-%m-%d %H:00:00') AS hora_utc,
        ROUND(AVG(health_score_pct), 2)  AS health_score_promedio,
        MIN(health_score_pct)             AS health_score_minimo,
        COUNT(*)                          AS num_muestras
    FROM tb_telemetria
    WHERE timestamp_utc >= UTC_TIMESTAMP() - INTERVAL 48 HOUR
    GROUP BY well_id, DATE_FORMAT(timestamp_utc, '%Y-%m-%d %H:00:00')
    ORDER BY hora_utc DESC;
