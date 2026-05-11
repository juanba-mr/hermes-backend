import os
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# Importamos el parser y los modelos
from parser import procesar_archivo_seguros
from models import Cliente, Compania, Poliza, BienAsegurado

# Configuramos la conexión a Neon
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL, connect_args={"sslmode": "require"})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def cargar_archivo(nombre_archivo):
    ruta = os.path.join('data', nombre_archivo)
    print(f"\n--- 🚀 INTENTANDO CARGAR: {ruta} ---")
    
    # 1. Llamamos a tu parser (El Traductor)
    df = procesar_archivo_seguros(ruta)
    
    if df.empty:
        print(f"⚠️ El archivo {nombre_archivo} no devolvió datos o no existe.")
        return

    print(f"✅ Parser OK. Filas detectadas: {len(df)}. Inyectando en Neon...")
    
    db = SessionLocal()
    
    try:
        for _, row in df.iterrows():
            # 1. COMPAÑÍA (Buscar o crear)
            cia = db.query(Compania).filter_by(nombre=row['compania_nombre']).first()
            if not cia:
                cia = Compania(nombre=row['compania_nombre'])
                db.add(cia)
                db.flush() # flush() guarda temporalmente y nos da el cia.id

            # 2. CLIENTE (Buscar por DNI o crear)
            cliente = db.query(Cliente).filter_by(dni=row['dni']).first()
            if not cliente:
                cliente = Cliente(
                    dni=row['dni'],
                    nombre_completo=row['nombre_completo'],
                    telefono=row['telefono']
                )
                db.add(cliente)
                db.flush() # Obtenemos el UUID del cliente

            # 3. PÓLIZA (Buscar por número o crear)
            poliza = db.query(Poliza).filter_by(numero_poliza=row['numero_poliza']).first()
            
            # Fallback de fecha por si el Excel viene roto (tu BD exige que no sea nula)
            fecha_fin = row['fecha_fin_vigencia']
            if pd.isna(fecha_fin) or not fecha_fin:
                fecha_fin = datetime.now().date()

            if not poliza:
                poliza = Poliza(
                    numero_poliza=row['numero_poliza'],
                    cliente_id=cliente.id,
                    compania_id=cia.id,
                    fecha_fin_vigencia=fecha_fin,
                    saldo_adeudado=row['saldo_adeudado'],
                    estado_vigencia=row['estado_vigencia']
                )
                db.add(poliza)
                db.flush() # Obtenemos el UUID de la póliza
            else:
                # Si la póliza ya existe, actualizamos el saldo por si cambió
                poliza.saldo_adeudado = row['saldo_adeudado']
                poliza.fecha_fin_vigencia = fecha_fin

            # 4. BIEN ASEGURADO (Buscar por poliza_id o crear)
            bien = db.query(BienAsegurado).filter_by(poliza_id=poliza.id).first()
            if not bien:
                bien = BienAsegurado(
                    poliza_id=poliza.id,
                    tipo=row['tipo'],
                    descripcion_modelo=row['descripcion_modelo'],
                    patente=row['patente'] if pd.notna(row['patente']) else None,
                    detalles=row['detalles_bien']
                )
                db.add(bien)

        # Si todo el bucle salió bien, hacemos el commit final
        db.commit()
        print(f"✅ ¡Datos de {nombre_archivo} guardados en Neon exitosamente!")

    except Exception as e:
        print(f"❌ Error al guardar en base de datos: {e}")
        db.rollback() # Si algo explota, deshacemos los cambios para no dejar datos a medias
    finally:
        db.close()

if __name__ == "__main__":
    # Automáticamente busca los archivos en la carpeta data/
    archivos = os.listdir('data')
    print(f"Archivos encontrados en data/: {archivos}")
    
    for arch in archivos:
        if arch.endswith('.csv') or arch.endswith('.xls'):
            cargar_archivo(arch)