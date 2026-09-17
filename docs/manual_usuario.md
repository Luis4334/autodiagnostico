# Manual de Usuario: Sistema de Autodiagnóstico MPFM

Este manual está diseñado para operadores e ingenieros que monitorean la salud del pozo y los instrumentos (MPFM) en tiempo real usando el panel web.

## 1. Visión General del Dashboard

Al ingresar al sistema, lo primero que notarás es el **Health Score (Índice de Salud)** en el panel izquierdo. 
Este porcentaje resume el estado general del equipo:
- **100% - 90% (SANO)**: Operación normal, las variables son coherentes.
- **89% - 70% (ADVERTENCIA)**: Algún sensor presenta ruido, datos congelados o pequeños desvíos de la configuración.
- **< 70% (CRÍTICO)**: Incoherencias graves, límites operacionales superados o falla de hardware.

A la derecha del índice, encontrarás **tres medidores (gauges)** que desglosan la salud en tres capas:
1. **Calidad de Señal (Capa 1)**: Evalúa si los sensores están vivos (sin congelamiento) y sin ruido excesivo o picos repentinos.
2. **Coherencia Hidráulica (Capa 2)**: Verifica que la física dentro de la tubería tenga sentido (Ej. presiones diferenciales vs viscosidad del fluido).
3. **Comportamiento del Pozo (Capa 3)**: Evalúa si el pozo opera dentro de los límites esperados (Corte de agua, Balance de masa, Relación Gas-Petróleo).

---

## 2. Histórico de Alarmas (Tabla)

En la sección inferior se listan las **últimas alarmas generadas**. 
- **Capa**: Te indica de dónde proviene el problema (Señal, Hidráulica o Pozo).
- **Descripción**: Muestra un mensaje detallado explicando **por qué** se disparó la alarma (ej. "Presión total excesiva 12 psi > 10 psi").

> [!TIP]
> **Gestión del Historial:** Si las alarmas son muy antiguas y ya fueron atendidas, puedes presionar el botón **"Purgar 30 días"** para limpiar la base de datos de eventos obsoletos.

---

## 3. Exportación de Reportes

El sistema permite extraer los datos históricos para un análisis profundo o para reportes de ingeniería. Dispones de dos formatos principales:

### CSV de Alarmas
Exporta únicamente el registro de los eventos anormales (alarmas) en un formato ligero.
1. Haz clic en el botón **CSV Alarmas**.
2. Selecciona un rango de fechas (**Inicio y Fin**) o marca la casilla para descargar todo el histórico.
3. Se generará un archivo `.csv` ideal para importar a otros sistemas de gestión o analizar rápidamente fallas frecuentes.

### Excel Completo
Exporta tanto las alarmas como el **registro de telemetría** (las variables físicas segundo a segundo).
1. Haz clic en **Excel Completo**.
2. Define tu rango de fechas de interés.
3. Se descargará un archivo `.xlsx` con **dos pestañas**: una con los eventos (alarmas) y otra con el crudo de variables (`P_line_psig`, `Q_liquid_bpd`, `WC`, etc.) correspondiente a ese periodo.

> [!NOTE]
> La descarga por defecto carga las últimas 24 horas. Ampliar mucho el rango de fechas en el Excel tomará más tiempo en generar el archivo debido al volumen de datos que el PLC lee cada 2 segundos.
