import pandas as pd
import re
import os
from datetime import datetime

def clean_amount(val):
    if pd.isna(val) or val == "": return 0.0
    val_str = str(val).split('\n')[0]
    val_str = re.sub(r'[^\d,]', '', val_str).replace(',', '.')
    try:
        return float(val_str)
    except:
        return 0.0

def parse_fecha(fecha_str):
    try:
        clean_date = str(fecha_str).strip().split('\n')[-1]
        return datetime.strptime(clean_date, '%d/%m/%Y').date()
    except:
        return None

def extraer_datos_vehiculo(texto):
    if pd.isna(texto) or not isinstance(texto, str):
        return "Ver Póliza", None

    t = texto.replace('\n', ' ').strip()
    
    patrones = [
        r'\b([A-Z]{2}\s?\d{3}\s?[A-Z]{2})\b', # Auto Mercosur
        r'\b([A-Z]{1}\s?\d{3}\s?[A-Z]{3})\b', # Moto Mercosur
        r'\b([A-Z]{3}\s?\d{3})\b',            # Auto Clásica
        r'\b(\d{3}\s?[A-Z]{3})\b'             # Moto Clásica
    ]
    
    patente = None
    
    for patron in patrones:
        matches = list(re.finditer(patron, t, re.IGNORECASE))
        if matches:
            ultimo_match = matches[-1]
            patente = ultimo_match.group(1).replace(' ', '').upper()
            t = t[:ultimo_match.start()] + t[ultimo_match.end():]
            break

    modelo = re.sub(r'[-\s/.]+$', '', t).strip()

    return modelo if modelo else "Ver Póliza", patente


# --- FUNCIÓN PRINCIPAL ---
# ¡Ahora requiere que le pases la compañía que viene del Frontend!
def procesar_archivo_seguros(ruta, compania_seleccionada):
    if not os.path.exists(ruta):
        raise FileNotFoundError(f"Archivo no encontrado: {ruta}")

    ext = os.path.splitext(ruta)[1].lower()
    encoding = 'latin-1'
    
    # 1. Normalizamos el texto (ej: "Antártida Seguros" -> "ANTARTIDA")
    compania_limpia = str(compania_seleccionada).upper().replace('Á', 'A').replace(' SEGUROS', '').strip()

    try:
        df_final = []

        # 2. Leemos la estructura correcta según lo que se seleccionó
        if compania_limpia == 'ANTARTIDA':
            # --- LÓGICA ANTÁRTIDA ---
            if ext == '.csv':
                df = pd.read_csv(ruta, sep=';', encoding=encoding, skiprows=1, on_bad_lines='skip')
            else:
                df = pd.read_excel(ruta, engine='xlrd' if ext == '.xls' else None, skiprows=1)
            
            for _, row in df.iterrows():
                poliza_num = str(row.iloc[1]).strip()
                df_final.append({
                    'compania_nombre': 'ANTARTIDA', 
                    'dni': poliza_num, 
                    'nombre_completo': str(row.iloc[4]).strip(),
                    'telefono': None,
                    'email': None,
                    'numero_poliza': poliza_num,
                    'fecha_fin_vigencia': parse_fecha(row.iloc[2]),
                    'saldo_adeudado': 0.0,
                    'estado_vigencia': 'VIGENTE',
                    'tipo': "Automotores" if str(row.iloc[0]) == '4' else "Otros",
                    'descripcion_modelo': "Ver Póliza",
                    'patente': None,
                    'detalles_bien': {'rama': str(row.iloc[0])}
                })
            
        elif compania_limpia == 'RUS':
            # --- LÓGICA RUS ---
            if ext == '.csv':
                df = pd.read_csv(ruta, sep=';', encoding=encoding, skiprows=5, on_bad_lines='skip')
            else:
                df = pd.read_excel(ruta, engine='xlrd' if ext == '.xls' else None, skiprows=5)
            
            for _, row in df.iterrows():
                pol_aseg = str(row.iloc[1]).split('\n')
                if len(pol_aseg) < 2: continue 
                
                dni_comm = str(row.iloc[2]).split('\n')
                dni_limpio = re.sub(r'\D', '', dni_comm[0])
                telefonos = " / ".join(dni_comm[1:]).strip() if len(dni_comm) > 1 else None
                
                cobertura_raw = str(row.iloc[8]).split('\n')[0]
                tipo_seguro = cobertura_raw.split('-')[0].strip()
                
                vigencia_raw = str(row.iloc[3]).split('-')
                fecha_fin = parse_fecha(vigencia_raw[-1]) if len(vigencia_raw) > 0 else None
                
                vehiculo_crudo = str(row.iloc[6])
                modelo_limpio, patente_encontrada = extraer_datos_vehiculo(vehiculo_crudo)

                df_final.append({
                    'compania_nombre': 'RUS',
                    'dni': dni_limpio,
                    'nombre_completo': pol_aseg[1].strip(),
                    'telefono': telefonos,
                    'email': None,
                    'numero_poliza': pol_aseg[0].strip(),
                    'fecha_fin_vigencia': fecha_fin,
                    'saldo_adeudado': clean_amount(row.iloc[10]),
                    'estado_vigencia': 'VIGENTE',
                    'tipo': tipo_seguro, 
                    'descripcion_modelo': modelo_limpio,
                    'patente': patente_encontrada,
                    'detalles_bien': {'cobertura_original': str(row.iloc[8])}
                })
        else:
            print(f"No hay lógica de lectura (parser) implementada para la compañía: {compania_limpia}")

        return pd.DataFrame(df_final)

    except Exception as e:
        print(f"Error crítico en parser: {e}")
        return pd.DataFrame()