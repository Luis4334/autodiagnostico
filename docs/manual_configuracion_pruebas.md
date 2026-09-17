# Manual de Configuración y Escenarios de Prueba

Este documento está orientado a los ingenieros de control y administradores del sistema. Detalla cómo configurar los umbrales operativos y cómo forzar escenarios de falla para validar la lógica del autodiagnóstico.

---

## 1. Guía de Configuración de Umbrales

En la interfaz web, existe una pestaña de **"Configuración & Umbrales"**. Todo cambio realizado aquí se aplica **en tiempo real** sin necesidad de reiniciar el sistema.

### Umbrales Operativos de Pozo
- **Gravedad API y RGP Esperada**: Parámetros de diseño del crudo. Se usan para el cálculo de RGP (Relación Gas-Petróleo) teórica.
- **Tolerancia RGP (%)**: Qué tanto puede desviarse la RGP real medida respecto a la esperada antes de lanzar alarma.
- **WC Operacional Máx (%)**: El límite máximo permitido de Corte de Agua. (Si se configura en 50%, un WC de 60% generará alarma).
- **Salto Máx WC/Ciclo (%)**: Tolerancia al salto brusco. Evita que un pico irreal de WC se tome como dato válido.

### Umbrales de Hidráulica e Instrumentación
- **ΔP Máx Skid (psi)**: Caída de presión máxima tolerada a lo largo del múltiple (PDT_01 + PDT_03).
- **Viscosidad Transición (cP)**: Límite donde el sistema cambia de esperar lecturas del medidor Cuña (baja viscosidad) al Laminar (alta viscosidad).
- **DP Mínimo Metrológico (inH2O)**: Límite por debajo del cual se asume que un medidor no está "viendo" flujo.
- **GVF Sonar Mínimo/Máximo (%)**: Límites físicos aceptables para la fracción de gas arrastrado leída por el medidor SONAR.

### Parámetros Estadísticos (Capa 1)
- **Z-Score Umbral Outlier**: Controla qué tan estricto es el filtro de valores atípicos. (Mayor a 3.5 = menos alarmas falsas, Menor a 2.0 = muy sensible).
- **Spike ROC (Rate of Change)**: Límite de salto absoluto para cada variable. Si la variable salta en un segundo más de este valor, se dispara alarma de *Cambio Brusco*.

---

## 2. Escenarios de Prueba (Testing)

A continuación se presentan 5 escenarios prácticos para que puedas ver en acción el sistema y verificar que todas las capas responden correctamente.

### Escenario 1: Alarma de Corte de Agua (Límite Operacional)
**Objetivo**: Validar que el sistema responde ante un alto corte de agua.
1. Ve a la pestaña **Configuración**.
2. Cambia el campo **WC Operacional Máx (%)** a un valor muy bajo, por ejemplo, `20%`.
3. Haz clic en **Aplicar Configuración**.
4. En tu simulador/PLC, asegúrate de que el Tag de `WC` esté en un valor mayor a 20 (ej. `45%`).
5. **Resultado esperado**: En 2 segundos, el *Health Score* caerá, la "Capa 3 (Comportamiento de Pozo)" se marcará en rojo, y verás una alarma: *"ALARMA WC: Corte de agua (45%) supera límite operacional (20%)"*.

### Escenario 2: Alarma de Cambio Brusco (Spike) en Presión
**Objetivo**: Probar la detección instantánea de ruido o falla de transmisor.
1. Ve a la pestaña **Configuración**.
2. Revisa el valor de **Spike ROC P. Línea (psig)** (por defecto `15.0`).
3. En el PLC, modifica bruscamente el tag `PRESION_ENTRADA`. Si está en `240`, cámbialo súbitamente a `200`.
4. **Resultado esperado**: Como el salto (40 psi) es mayor a 15, la "Capa 1 (Calidad de Señal)" fallará temporalmente indicando un *Cambio Brusco*.

### Escenario 3: Incoherencia Metrológica (Medidor Laminar/Cuña)
**Objetivo**: Probar la lógica de validación cruzada entre viscosidad y medidores de presión diferencial.
1. Asegúrate de tener flujo de líquido (`Q_Liquido > 50`).
2. En la Configuración, revisa la **Viscosidad Transición** (ej. `20 cP`) y el **DP Mínimo Metrológico** (ej. `0.1 inH2O`).
3. En el PLC, ajusta la viscosidad (`miu_Oil`) a un valor muy alto (ej. `50 cP`). Según la física, deberíamos usar el medidor *Laminar*.
4. Forza el tag del medidor laminar (`DP_L`) a `0.0` (indicando que no marca nada).
5. **Resultado esperado**: "Capa 2 (Coherencia Hidráulica)" fallará con la alerta: *"INCOHERENCIA METROLÓGICA: Alta viscosidad (50 cP) sin respuesta en medidor laminar"*.

### Escenario 4: Falla de Balance de Masas (Inyección de Diluente)
**Objetivo**: Validar la resta del diluente en el cálculo matemático.
1. En el PLC, ajusta los siguientes valores:
   - `Q_Liquido` = `1000` BPD
   - `WC` = `10%`
   - `caudal_diluente_BM` = `200` BPD
2. Según la matemática: `Crudo Esperado = (1000 * (1 - 0.10)) - 200 = (1000 * 0.90) - 200 = 900 - 200 = 700 BPD`.
3. Ahora ajusta el crudo reportado en el PLC (`Q_Crudo`) a `500` BPD (un valor totalmente irreal frente a los 700 calculados).
4. **Resultado esperado**: "Capa 3" fallará con *"DISCREPANCIA BALANCE: Q_oil (500 BPD) difiere del cálculo... en X%"*.

### Escenario 5: Sensor Congelado (Flatline)
**Objetivo**: Validar que el sistema note cuando un sensor se daña y deja de variar.
1. Ve al PLC y congela completamente los tags de presión diferencial (e.g. `DP_W` y `DP_L`). Ponles un valor fijo (e.g. `15.0`) y asegúrate de que **no oscilen ni un decimal** durante más de 60 segundos (dependiendo de tu ventana de muestras de 30 ciclos x 2s = 60s).
2. **Resultado esperado**: Al cabo del tiempo de la ventana, la desviación estándar será matemáticamente cero y la "Capa 1" detonará: *"FALLA CRÍTICA: Sensor congelado — Flatline detectado"*.
