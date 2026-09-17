import time
from pylogix import PLC

class SimPLCReader:
    def __init__(self, ip_address="172.17.32.220"):
        self.ip_address = ip_address
        self.comm = PLC()
        self.comm.IPAddress = self.ip_address
        
        # Cleaned up list of unique tags to read
        # Removing duplicates (like Numero_Prueba, SW_DIL_MEDIDO_CALC, Program:PID.PRESS_PID.CV, caudal_diluente_BM)
        self.tags_to_read = [
            "Program:PID.PRESS_PID.CV",
            "T_GAS",
            "Program:MainProgram.ALARM_FT_03.AHH",
            "Program:MainProgram.ALARM_FT_03.AH",
            "Program:MainProgram.ALARM_FT_03.AL",
            "PDT_01",
            "Program:MainProgram.ALARM_PT_01.AHH",
            "Program:MainProgram.ALARM_PT_01.AH",
            "Program:MainProgram.ALARM_PT_01.AL",
            "Q_gas_STD",
            "P_Gas",
            "PDT_03",
            "Program:MainProgram.ALARM_DP_01.AHH",
            "Program:MainProgram.ALARM_DP_01.AH",
            "Program:MainProgram.ALARM_DP_01.AL",
            "DP_Simeflum",
            "P_Oil",
            "Program:MainProgram.ALARM_LIT.AHH",
            "Program:MainProgram.ALARM_LIT.AH",
            "Program:MainProgram.ALARM_LIT.AL",
            "Program:MainProgram.ALARM_LIT.ALL",
            "LIT_001",
            "Program:PID.LEVEL_PID.CV",
            "transmisor_baja",
            "Program:PID.MAN_PC",
            "Program:MainProgram.ALARM_FT_02.AHH",
            "Program:MainProgram.ALARM_FT_02.AH",
            "Program:MainProgram.ALARM_FT_02.AL",
            "Program:PID.MAN_LC",
            "Numero_Prueba",
            "Program:MainProgram.ALARM_FT_01.AHH",
            "Program:MainProgram.ALARM_FT_01.AH",
            "Program:MainProgram.ALARM_FT_01.AL",
            "Program:MainProgram.ALARM_WC.AHH",
            "Program:MainProgram.WC_SW",
            "Program:MainProgram.ALARM_VIT.AHH",
            "Program:MainProgram.ALARM_VIT.AH",
            "Program:MainProgram.ALARM_VIT.AL",
            "Program:MainProgram.ALARM_VIT.ALL",
            "Program:MainProgram.ALARM_TIT_01.AHH",
            "Program:MainProgram.ALARM_TIT_01.AH",
            "Program:MainProgram.ALARM_TIT_01.AL",
            "TIPO_EQUIPO",
            "Program:MainProgram.ALARM_GVF.AHH",
            "Program:MainProgram.GVF_SW",
            "DP_W",
            "DP_L",
            "WC",
            "v_oil_medida",
            "T_Oil_C",
            "GVoidF",
            "miu_Oil",
            "T_Oil_F",
            "Qb_Liquido_Estimado",
            "Q_Crudo_Estimado",
            "Qb_Diluente_Estimado",
            "Q_W_Estimado",
            "Q_gat_Estimado",
            "Program:Caudal.Q_gas_T_sc",
            "caudal_diluente_BM",
            "SW_DIL_MEDIDO_CALC",
            "Q_Liquido",
            "Q_Crudo",
            "Q_W",
            "Q_gat",
            "Program:Prueba.ESTATUS",
            "DESHABILITA_PID",
            "Program:MainProgram.Apertura_Valvula",
            "Program:MainProgram.Abrir_Valvula_Man",
            "Program:MainProgram.Abrir_Valvula_Auto",
            "STATUS_ERROR_CAUDAL_NETO_DILUENTE",
            "VI_SW",
            "CORIOLIS_DENSITY",
            "CORIOLIS_TEMPERATURE",
            "CORIOLIS_VOL_FLOW_RATE",
            "PRESION_ENTRADA",
            "Program:Caudal.wedge",
            
        ]

    def read_all_tags(self):
        """
        Reads all configured tags from the PLC.
        pylogix allows passing a list of tags for more efficient multi-reading.
        """
        print(f"Connecting to PLC at {self.ip_address}...")
        try:
            # Pass the entire list to Read()
            # It returns a list of Response objects corresponding to the tags
            results = self.comm.Read(self.tags_to_read)
            
            readings = {}
            for i, result in enumerate(results):
                tag_name = self.tags_to_read[i]
                if result.Status == 'Success':
                    readings[tag_name] = result.Value
                else:
                    readings[tag_name] = f"Error: {result.Status}"
                    
            return readings
            
        except Exception as e:
            print(f"Communication error: {e}")
            return None
            
    def close(self):
        self.comm.Close()

if __name__ == "__main__":
    # Initialize reader with your IP
    reader = SimPLCReader("172.17.32.220")
    
    print("Initiating tag read sequence...\n")
    data = reader.read_all_tags()
    
    if data:
        print("--- Tag Values ---")
        # Print results neatly formatted
        for tag, value in data.items():
            print(f"{tag:<55} : {value}")
            
    reader.close()
    print("\nConnection closed.")