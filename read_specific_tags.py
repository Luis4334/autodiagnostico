import time
from pylogix import PLC

def read_specific_tags():
    ip_address = "172.17.32.220"
    
    tags_to_read = [
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
        "Program:MainProgram.WC_SW"
    ]

    print(f"Conectando al PLC en {ip_address}...")
    
    with PLC() as comm:
        comm.IPAddress = ip_address
        try:
            results = comm.Read(tags_to_read)
            
            print("\n--- Valores de las Variables ---")
            for i, result in enumerate(results):
                tag_name = tags_to_read[i]
                if result.Status == 'Success':
                    print(f"{tag_name:<50} : {result.Value}")
                else:
                    print(f"{tag_name:<50} : Error de lectura ({result.Status})")
                    
        except Exception as e:
            print(f"\nError de comunicación: {e}")
            print("No se pudo alcanzar el PLC. Verifica la conexión de red.")

if __name__ == "__main__":
    read_specific_tags()
