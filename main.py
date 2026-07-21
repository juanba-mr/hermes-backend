from fastapi import FastAPI, HTTPException, Depends, Security, File, UploadFile, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, joinedload, Session
from datetime import date, timedelta, datetime
from inteligencia import generar_mensaje_renovacion
from notificaciones import enviar_alerta_push
import json
from pywebpush import webpush, WebPushException
from auth import crear_token_acceso, verificar_token
import PyPDF2
from io import BytesIO
from google import genai
from supabase import create_client, Client
import logging
from typing import Optional
import pandas as pd
from parser import procesar_archivo_seguros


# Importamos tus modelos (asegúrate de que el archivo se llame models.py)
from models import Cliente, Poliza, Compania, BienAsegurado, UsuarioAdmin, SuscripcionPush, Sucursal, Mensaje

# 1. Configuración de Base de Datos
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Neon requiere sslmode=require para conexiones seguras
engine = create_engine(
    DATABASE_URL, 
    connect_args={"sslmode": "require"},
    pool_pre_ping=True,   # <--- Verifica si la conexión sigue viva antes de usarla
    pool_recycle=1800     # <--- Cierra y renueva las conexiones cada 30 minutos
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Inicialización del cliente de Supabase Storage
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

app = FastAPI(title="Backend Hermes Seguros")

# monitor uptime

@app.get("/")
@app.head("/")
def health_check():
    return {"status": "ok", "mensaje": "El backend de Hermes está despierto."}

# ARQUITECTURA DEFENSIVA: POOL DE MODELOS Y LLAVES

class GeminiRotator:
    def __init__(self):
        key1 = os.getenv("GEMINI_API_KEY_PDF")
        key2 = os.getenv("GEMINI_API_KEY_PDF_2")
        
        # Filtramos las keys que realmente existan en el .env
        self.api_keys = [k for k in [key1, key2] if k]
        if not self.api_keys:
            raise ValueError("Falta configurar GEMINI_API_KEY_PDF en el archivo .env")

        # Pool de 5 modelos funcionales validados
        self.models = [
            "gemini-3.5-flash",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest"
        ]
        
        # Punteros de estado en memoria
        self.current_key_idx = 0
        self.current_model_idx = 0

    def procesar_prompt(self, prompt: str) -> str:
        consecutive_failures = 0
        total_combinations = len(self.api_keys) * len(self.models)

        while consecutive_failures < total_combinations:
            current_key = self.api_keys[self.current_key_idx]
            current_model = self.models[self.current_model_idx]

            try:
                client = genai.Client(api_key=current_key)
                response = client.models.generate_content(
                    model=current_model,
                    contents=prompt
                )
                return response.text

            except Exception as e:
                logger.warning(f"⚠️ [APIError] Fallo en {current_model} (Key {self.current_key_idx + 1}). Error: {e}. Rotando modelo...")
                
                # Desplazamiento al siguiente modelo
                self.current_model_idx += 1
                consecutive_failures += 1

                # Si agotamos los 5 modelos de esta Key, saltamos a la siguiente Key
                if self.current_model_idx >= len(self.models):
                    self.current_model_idx = 0
                    self.current_key_idx += 1
                    logger.warning(f"🔄 Pool agotado para la Key actual. Conmutando a Key {self.current_key_idx + 1}...")

                    # Si llegamos al final de la lista de Keys, volvemos a empezar el índice 
                    # (aunque el bucle while cortará si se alcanzan fallos consecutivos máximos)
                    if self.current_key_idx >= len(self.api_keys):
                        self.current_key_idx = 0

        # Excepción crítica final si todo el pool de ambas keys fracasa
        raise Exception("CRÍTICO: Ambas API Keys agotaron sus 5 modelos de manera consecutiva. Sugerencia: Configurar facturación comercial o pausar ingesta.")

# Instanciamos el motor en memoria al arrancar el servidor
ia_motor = GeminiRotator()



# 2. Configuración de CORS
# Esto permite que tu frontend en el puerto 5173 pueda hablar con este servidor
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", 
        "https://app.hermesasesores.com.ar" # <-- Tu nuevo subdominio acá
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Esto le dice a FastAPI que busque el "Candadito" en la documentación (Swagger)
security = HTTPBearer()

def obtener_usuario_actual(res: HTTPAuthorizationCredentials = Security(security)):
    """
    Esta función es el guardaespaldas. 
    Si el token es válido, deja pasar. Si no, tira error 401.
    """
    token = res.credentials
    payload = verificar_token(token)
    
    if not payload:
        raise HTTPException(
            status_code=401, 
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload # Devuelve los datos del usuario (dni, rol, etc)


# 3. Modelos de Datos para Peticiones (Pydantic)
class LoginRequest(BaseModel):
    dni: str

# 4. ENDPOINT: Login de Usuarios
@app.post("/api/login")
def login_usuario(request: LoginRequest):
    db = SessionLocal()
    try:
        # 1. Buscamos si es Administrador
        admin = db.query(UsuarioAdmin).filter(
            UsuarioAdmin.dni_acceso == request.dni,
            UsuarioAdmin.is_active == True
        ).first()
        
        if admin:
            # Generamos el token para el Admin
            token = crear_token_acceso(data={
                "sub": str(admin.id), 
                "rol": admin.rol, 
                "nombre": admin.nombre_completo,
                "sucursal_id": str(admin.sucursal_id) if admin.sucursal_id else None # <--- ACÁ
            })
            return {
                "success": True,
                "token": token, # <--- ¡Acá va el pase!
                "tipo_usuario": "admin",
                "usuario": {
                    "id": str(admin.id),
                    "nombre": admin.nombre_completo,
                    "dni": admin.dni_acceso,
                    "rol": admin.rol,
                    "sucursal_id": str(admin.sucursal_id) if admin.sucursal_id else None
                }
            }

        # 2. Buscamos si es Cliente regular
        cliente = db.query(Cliente).filter(
            Cliente.dni == request.dni, 
            Cliente.is_active == True
        ).first()
        
        if cliente:
            # Generamos el token para el Cliente
            token = crear_token_acceso(data={
                "sub": str(cliente.dni), 
                "rol": "cliente", 
                "nombre": cliente.nombre_completo
            })
            return {
                "success": True,
                "token": token, # <--- El cliente también recibe su pase
                "tipo_usuario": "cliente",
                "usuario": {
                    "id": str(cliente.id),
                    "nombre": cliente.nombre_completo,
                    "dni": cliente.dni
                }
            }
            
        raise HTTPException(status_code=404, detail="DNI no encontrado o inactivo")
    finally:
        db.close()

# 5. ENDPOINT: Perfil del Cliente y Lista de Pólizas (Lo que usa el Dashboard)
@app.get("/api/clientes/{dni}")
def get_perfil_cliente(dni: str, usuario: dict = Depends(obtener_usuario_actual)):
    # Verificación de seguridad: solo el propio cliente o un admin pueden ver esto
    if usuario["rol"] == "cliente" and usuario["sub"] != dni:
        raise HTTPException(status_code=403, detail="No tienes permiso para ver datos ajenos")

    db = SessionLocal()
    try:
        # Usamos joinedload para traer la sucursal, las pólizas y compañías de un solo viaje
        cliente = db.query(Cliente).options(
            joinedload(Cliente.sucursal)
        ).filter(Cliente.dni == dni).first()
        
        if not cliente:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")

        polizas_data = []
        for p in cliente.polizas:
            if p.is_enabled:
                # Obtenemos el bien asegurado vinculado
                bien = p.bien_asegurado
                # Obtenemos la compañía
                cia = p.compania
                
                polizas_data.append({
                    "numero_poliza": p.numero_poliza,
                    "compania": cia.nombre if cia else "S/D",
                    "tipo_seguro": bien.tipo if bien else "General",
                    "vehiculo": bien.descripcion_modelo if bien else "Ver póliza",
                    "patente": bien.patente if bien else "",
                    "vigencia_hasta": str(p.fecha_fin_vigencia),
                    "estado": p.estado_vigencia.lower(), # 'vigente' o 'vencida'
                    # Si el saldo es 0 o negativo, está al día
                    "estado_pago": "al_dia" if (p.saldo_adeudado or 0) <= 0 else "vencido",
                    "asistencia_telefono": cia.telefono_asistencia if cia else "0800-XXX-XXXX",
                    "pdf_url": p.pdf_url
                })

        return {
            "dni": cliente.dni,
            "nombre": cliente.nombre_completo,
            "sucursal": {
                "id": str(cliente.sucursal.id) if cliente.sucursal else None,
                "nombre": cliente.sucursal.nombre if cliente.sucursal else "Central",
                "telefono_whatsapp": cliente.sucursal.telefono_whatsapp if cliente.sucursal else "5491100000000" # Fallback de seguridad
            },
            "polizas": polizas_data,
            "mensajes": [
            {
                "id": str(m.id),
                "titulo": m.titulo,
                "cuerpo": m.cuerpo,
                "fecha": m.fecha_creacion.isoformat(),
                "leido": m.leido
            } for m in sorted(cliente.mensajes, key=lambda x: x.fecha_creacion, reverse=True)
        ]
        }
    finally:
        db.close()

# 6. ENDPOINT: Detalle de una Póliza Específica (Lo que usa PolicyDetail.jsx)
@app.get("/api/clientes/{dni}/polizas/{numero_poliza}")
def get_detalle_poliza(dni: str, numero_poliza: str, usuario: dict = Depends(obtener_usuario_actual)):
    if usuario["rol"] == "cliente" and usuario["sub"] != dni:
        raise HTTPException(status_code=403, detail="Acceso denegado")

    db = SessionLocal()
    try:
        # Buscamos la póliza específica
        poliza = db.query(Poliza).filter(Poliza.numero_poliza == numero_poliza).first()
        
        if not poliza or poliza.cliente.dni != dni:
            raise HTTPException(status_code=404, detail="Póliza no encontrada")

        bien = poliza.bien_asegurado
        cia = poliza.compania

        return {
            "numero_poliza": poliza.numero_poliza,
            "compania": cia.nombre if cia else "S/D",
            "asistencia_telefono": cia.telefono_asistencia if cia else "0800-XXX-XXXX",
            "vigencia_desde": str(poliza.fecha_inicio) if poliza.fecha_inicio else "S/D",
            "vigencia_hasta": str(poliza.fecha_fin_vigencia),
            "estado": poliza.estado_vigencia.lower(),
            "estado_pago": "al_dia" if (poliza.saldo_adeudado or 0) <= 0 else "vencido",
            "monto_adeudado": float(poliza.saldo_adeudado or 0),
            "vehiculo": {
                "tipo": bien.tipo if bien else "Vehículo",
                "modelo": bien.descripcion_modelo if bien else "S/D",
                "patente": bien.patente if bien else "S/D",
                "detalles": bien.detalles if bien else {}
            },
            "cobertura": poliza.datos_especificos.get('cobertura_completa', "Consultar con productor"), 
            "pdf_url": poliza.pdf_url
        }
    finally:
        db.close()
        
# 7. ENDPOINT: Estadísticas para el Admin Dashboard
@app.get("/api/admin/stats")
def get_admin_stats(usuario: dict = Depends(obtener_usuario_actual)):
    if usuario["rol"].lower() not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Acceso exclusivo para administradores")

    db = SessionLocal()
    try:
        total_clientes = db.query(Cliente).filter(Cliente.is_active == True).count()
        total_a_cobrar = db.query(func.sum(Poliza.saldo_adeudado)).filter(Poliza.is_enabled == True).scalar() or 0
        polizas_vigentes = db.query(Poliza).filter(Poliza.estado_vigencia == 'VIGENTE').count()
        renovaciones_mes = db.query(Poliza).filter(Poliza.estado_vigencia == 'VIGENTE').count() 

        # NUEVO: Lógica para armar el gráfico de barras (cantidad de pólizas por compañía)
        distribucion = db.query(
            Compania.nombre, func.count(Poliza.id)
        ).join(Poliza.compania).filter(
            Poliza.estado_vigencia == 'VIGENTE'
        ).group_by(Compania.nombre).all()
        
        # Lo formateamos para que recharts (el gráfico de React) lo entienda
        chart_data = [{"name": c[0], "value": c[1]} for c in distribucion]

        return {
            "clientes_activos": total_clientes,
            "total_a_cobrar": float(total_a_cobrar),
            "polizas_vigentes": polizas_vigentes,
            "renovaciones_pendientes": renovaciones_mes,
            "distribucion_companias": chart_data # <-- Acá enviamos los datos del gráfico
        }
    finally:
        db.close()

# 8. ENDPOINT: Lista completa de clientes para el explorador
@app.get("/api/admin/clientes")
def get_admin_clientes(usuario: dict = Depends(obtener_usuario_actual)):
    if usuario["rol"].lower() not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Acceso exclusivo para administradores")
    
    db = SessionLocal()
    try:
        # Usamos joinedload para que traiga sucursal, pólizas y compañías súper rápido
        clientes = db.query(Cliente).options(
            joinedload(Cliente.sucursal),
            joinedload(Cliente.polizas).joinedload(Poliza.compania)
        ).all()
        result = []
        for c in clientes:
            polizas_activas = [p for p in c.polizas if p.is_enabled]
            cant_polizas = len(polizas_activas)
            
            # Recolectamos datos únicos de este cliente para los filtros
            companias = list(set([p.compania.nombre for p in polizas_activas if p.compania]))
            formas_pago = list(set([p.forma_pago for p in polizas_activas if getattr(p, 'forma_pago', None) and p.forma_pago != 'S/D']))
            estados = list(set([p.estado_vigencia for p in polizas_activas if p.estado_vigencia]))
            
            result.append({
                "id": str(c.id),
                "dni": c.dni,
                "nombre": c.nombre_completo,
                "telefono": c.telefono or "Sin teléfono",
                "sucursal_nombre": c.sucursal.nombre if c.sucursal else "Sin asignar",
                "cant_polizas": cant_polizas,
                "estado": "Activo" if c.is_active else "Inactivo",
                "companias": companias,
                "formas_pago": formas_pago,
                "estados_polizas": estados
            })
        return result
    finally:
        db.close()

class ClienteABM(BaseModel):
    nombre: str
    dni: str
    telefono: Optional[str] = None
    email: Optional[str] = None

# ENDPOINT: CREAR NUEVO CLIENTE (POST)
# ========================================================
@app.post("/api/admin/clientes")
def crear_cliente(datos: ClienteABM, usuario: dict = Depends(obtener_usuario_actual)):
    if usuario["rol"].lower() not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Acceso exclusivo para administradores")
    
    db = SessionLocal()
    try:
        # Verificamos que no exista un cliente con ese DNI
        existe = db.query(Cliente).filter(Cliente.dni == datos.dni).first()
        if existe:
            raise HTTPException(status_code=400, detail="Ya existe un cliente registrado con este DNI")

        # Creamos el nuevo cliente
        nuevo_cliente = Cliente(
            nombre_completo=datos.nombre,
            dni=datos.dni,
            telefono=datos.telefono or "",
            email=datos.email or "", # Descomentá esta línea si tenés la columna 'email' en tu base de datos Neon
            is_active=True,
            sucursal_id=usuario.get("sucursal_id")
        )
        db.add(nuevo_cliente)
        db.commit()
        return {"success": True, "message": "Cliente creado correctamente"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ========================================================
# ENDPOINT: EDITAR CLIENTE EXISTENTE (PUT)
# ========================================================
@app.put("/api/admin/clientes/{dni}")
def actualizar_cliente(dni: str, datos: ClienteABM, usuario: dict = Depends(obtener_usuario_actual)):
    if usuario["rol"].lower() not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Acceso exclusivo para administradores")
    
    db = SessionLocal()
    try:
        cliente = db.query(Cliente).filter(Cliente.dni == dni).first()
        if not cliente:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")

        # Actualizamos los datos
        cliente.nombre_completo = datos.nombre
        cliente.telefono = datos.telefono or ""
        cliente.email = datos.email or "" # Descomentá esta línea si tenés la columna 'email' en tu base de datos Neon
        
        db.commit()
        return {"success": True, "message": "Cliente actualizado correctamente"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ========================================================
# ENDPOINT: ELIMINAR CLIENTE (DELETE)
# ========================================================
@app.delete("/api/admin/clientes/{dni}")
def eliminar_cliente(dni: str, usuario: dict = Depends(obtener_usuario_actual)):
    if usuario["rol"].lower() not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Acceso exclusivo para administradores")
    
    db = SessionLocal()
    try:
        cliente = db.query(Cliente).filter(Cliente.dni == dni).first()
        if not cliente:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")

        # SOFT DELETE: En sistemas de seguros nunca borramos el registro físicamente
        # para no romper el historial de pólizas. Simplemente lo desactivamos.
        cliente.is_active = False
        db.commit()
        return {"success": True, "message": "Cliente eliminado correctamente"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# 9. ENDPOINT: Pólizas a renovar (Próximos 30 días)
@app.get("/api/admin/renovaciones")
def get_renovaciones(usuario: dict = Depends(obtener_usuario_actual)):
    if usuario["rol"].lower() not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Acceso exclusivo para administradores")
    
    db = SessionLocal()
    try:
        hoy = date.today()
        proximos_30 = hoy + timedelta(days=30)
        
        # Buscamos pólizas que venzan pronto
        polizas = db.query(Poliza).filter(
            Poliza.fecha_fin_vigencia >= hoy,
            Poliza.fecha_fin_vigencia <= proximos_30,
            Poliza.is_enabled == True
        ).order_by(Poliza.fecha_fin_vigencia.asc()).all()
        
        result = []
        for p in polizas:
            cliente = p.cliente
            bien = p.bien_asegurado
            result.append({
                "id": str(p.id),
                "numero_poliza": p.numero_poliza,
                "cliente_nombre": cliente.nombre_completo,
                "cliente_dni": cliente.dni,
                "cliente_tel": cliente.telefono,
                "compania": p.compania.nombre if p.compania else "S/D",
                "vehiculo": bien.descripcion_modelo if bien else "S/D",
                "patente": bien.patente if bien else "",
                "vence_el": str(p.fecha_fin_vigencia),
                "dias_restantes": (p.fecha_fin_vigencia - hoy).days
            })
        return result
    finally:
        db.close()


class PushSubRequest(BaseModel):
    dni: str
    suscripcion: dict


@app.post("/api/notificaciones/suscribir")
def guardar_suscripcion(req: PushSubRequest, usuario: dict = Depends(obtener_usuario_actual)):
    # Validar que el cliente se suscriba a su propio DNI
    if usuario["sub"] != req.dni:
         raise HTTPException(status_code=403, detail="Operación no permitida")

    db = SessionLocal()
    try:
        # Buscamos si este cliente ya tiene este celular registrado
        datos_str = json.dumps(req.suscripcion)
        
        existe = db.query(SuscripcionPush).filter(
            SuscripcionPush.cliente_dni == req.dni,
            SuscripcionPush.datos_navegador == datos_str
        ).first()
        
        if not existe:
            nueva_sub = SuscripcionPush(
                cliente_dni=req.dni,
                datos_navegador=datos_str
            )
            db.add(nueva_sub)
            db.commit()
            
        return {"success": True, "mensaje": "Suscripción guardada"}
    finally:
        db.close()
        
@app.get("/api/test-push/{dni}")
def probar_notificacion(dni: str):
    db = SessionLocal()
    try:
        # 1. Buscamos la suscripción del cliente en la base de datos
        suscripcion = db.query(SuscripcionPush).filter(SuscripcionPush.cliente_dni == dni).first()
        
        if not suscripcion:
            return {"error": "Este DNI no tiene las notificaciones activadas."}
            
        # 2. Convertimos el texto guardado de vuelta a un diccionario JSON
        sub_info = json.loads(suscripcion.datos_navegador)
        
        # 3. Disparamos la notificación Push
        try:
            webpush(
                subscription_info=sub_info,
                data=json.dumps({
                    "title": "¡Hermes Seguros!",
                    "body": "¡Funciona! Esta es una prueba de notificación push.",
                    "icon": "/icon-192x192.png",
                    "url": f"/dashboard?dni={dni}"
                }),
                vapid_private_key=os.getenv("VAPID_PRIVATE_KEY"),
                vapid_claims={"sub": "mailto:juanbamr244@gmail.com"}
            )
            return {"success": True, "mensaje": "Notificación enviada al navegador"}
        except WebPushException as ex:
            return {"error": "Falló el envío", "detalle": str(ex)}
            
    finally:
        db.close()
        
@app.post("/api/admin/disparar-alerta/{id_poliza}")
def disparar_alerta_inteligente(id_poliza: str, usuario: dict = Depends(obtener_usuario_actual)):
    if usuario["rol"].lower() not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Solo administradores pueden enviar alertas")

    db = SessionLocal()
    try:
        # 1. Buscamos la póliza y el cliente
        poliza = db.query(Poliza).filter(Poliza.id == id_poliza).first()
        if not poliza:
            raise HTTPException(status_code=404, detail="Póliza no encontrada")
            
        cliente = poliza.cliente
        bien = poliza.bien_asegurado
        dias_restantes = (poliza.fecha_fin_vigencia - date.today()).days
        
        # 2. Gemini redacta el mensaje
        mensaje_magico = generar_mensaje_renovacion(
            nombre_cliente=cliente.nombre_completo,
            vehiculo=bien.descripcion_modelo,
            dias_restantes=dias_restantes,
            compania=poliza.compania.nombre
        )
        
        # 3. BUSCAMOS LA SUSCRIPCIÓN REAL EN NEON
        suscripcion_db = db.query(SuscripcionPush).filter(SuscripcionPush.cliente_dni == cliente.dni).first()
        
        if not suscripcion_db:
            return {
                "success": False, 
                "mensaje": "El cliente no tiene alertas activas.", 
                "texto_generado": mensaje_magico 
            }
            
        # 4. Convertimos el JSON guardado en diccionario y mandamos
        suscripcion_real = json.loads(suscripcion_db.datos_navegador)
        
        nuevo_mensaje = Mensaje(
            cliente_id=cliente.id,
            titulo=f"Aviso de Renovación: {poliza.compania.nombre}",
            cuerpo=mensaje_magico
        )
        db.add(nuevo_mensaje)
        db.commit()        
        
        enviado = enviar_alerta_push(
            suscripcion_json=suscripcion_real, # <--- USAMOS LA REAL
            titulo="🔔 Hermes Asesores",
            cuerpo_mensaje=mensaje_magico
        )
        
        return {"success": enviado, "mensaje_generado": mensaje_magico}
    finally:
        db.close()
        
        
@app.post("/api/upload-poliza")
async def upload_poliza(file: UploadFile = File(...)):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="El archivo debe ser un PDF")

    try:
        # 1. Leer el archivo PDF en memoria
        contenido_pdf = await file.read()
        lector = PyPDF2.PdfReader(BytesIO(contenido_pdf))
        
        texto_extraido = ""
        for i in range(min(5, len(lector.pages))):
            texto_extraido += lector.pages[i].extract_text()
       
        # PROMPT (Tu versión mejorada)
        prompt = f"""
        Eres un asistente experto y analítico en seguros de Argentina.
        Tu tarea es leer la póliza adjunta, deducir la información oculta siguiendo ESTRICTAMENTE las reglas de negocio, y devolver los datos en formato JSON.
        
        REGLAS DE IDENTIFICACIÓN DE PERSONAS (¡MUY IMPORTANTE!):
        - El campo "nombre" DEBE ser el del ASEGURADO o TOMADOR (por ejemplo, "BRACAMONTE FACUNDO", "GUYET CRISTINA VIVIANA").
        - NO confundas al Asegurado con el PRODUCTOR, ASESOR, ORGANIZADOR o MATRÍCULA de seguros (por ejemplo, "MERCADO FRANCISCO ALFREDO" o el código "97792"). El nombre del productor o su matrícula NUNCA debe ser extraído como el nombre o DNI del asegurado.
        - El campo "dni" DEBE ser el DNI del ASEGURADO, el cual debe extraerse de su CUIT/CUIL de 11 dígitos que figure al lado o debajo de sus datos de asegurado.
        
        REGLA CRÍTICA PARA EL DNI (¡NO ALUCINAR!):
        - Extrae el número EXACTO que figura en el documento. NO inventes números.
        - REGLA DE EXTRACCIÓN DE DNI DESDE CUIT/CUIL:
          * Si el CUIT/CUIL tiene guiones (ej. 20-35123456-9), el DNI es exactamente el grupo central de 8 dígitos (35123456).
          * Si el CUIT/CUIL NO tiene guiones y es un bloque continuo de 11 dígitos (ej. 20351234569):
            - Los primeros 2 dígitos son el prefijo (ej. 20, 23, 27). ¡ELIMÍNALOS!
            - El último dígito es el verificador (ej. 9). ¡ELIMÍNALO!
            - El DNI son los 8 dígitos del medio. Por ejemplo, en "20351234569", eliminas el "20" del principio y el "9" del final, obteniendo "35123456" (8 dígitos).
            - NUNCA tomes los primeros 8 dígitos del CUIT (como "20351234") como si fueran el DNI. El DNI de un CUIT "20351234569" NUNCA empieza con el prefijo "20", "23" o "27".
        - El campo "dni" DEBE ser estrictamente una cadena de 7 u 8 números. No incluyas puntos ni letras.    
        - NO utilices la matrícula, código de productor o casillero (como "97792", "1989", etc.) como el DNI del asegurado.

        REGLAS DE COMPAÑÍA:
        - Si menciona "Río Uruguay", "RUS" o su CUIT, compania es "RUS".
        - Si menciona "Antártida" o su CUIT, compania es "ANTARTIDA".

        REGLAS DE FORMA DE PAGO:
        - Si el texto dice "PAGO MANUAL", "Ventanilla", "Rapipago", "Pago Fácil" o menciona pago en efectivo/billetera virtual, forma_pago es "Efectivo".
        - Si el texto dice "Débito Automático", "CBU", "Tarjeta", "Visa", "Mastercard", etc., forma_pago es "Tarjeta de Crédito / Débito".

        REGLAS AVANZADAS PARA EL PERÍODO DE FACTURACIÓN:
        Debes cruzar la información del tipo de vehículo, la compañía y si hay cuotas para deducir el período. Analiza el cronograma de pagos y las fechas de vigencia:

        1. REGLA PARA MOTOS (Cualquier compañía):
           - Las pólizas de motos NUNCA son mes a mes. SIEMPRE son en 1 solo pago.
           - Según la diferencia entre vigencia_desde y vigencia_hasta, periodo_facturacion DEBE ser: "Bimestral", "Cuatrimestral" o "Semestral".

        2. REGLAS PARA AUTOS EN ANTÁRTIDA:
           - Si la póliza dura 2 meses y se abona en 1 SOLO PAGO: periodo_facturacion = "Bimestral".
           - Si la póliza dura 2 meses pero se abona "MES A MES": periodo_facturacion = "Bimestral 2".

        3. REGLAS PARA AUTOS EN RUS:
           - Si la póliza dura 2 meses y se abona en 1 SOLO PAGO: periodo_facturacion = "Bimestral".
           - Si la póliza se abona "MES A MES" (cuotas mensuales): periodo_facturacion = "Mensual".

        ESTRUCTURA EXACTA REQUERIDA:
        {{
            "nombre": "Nombre completo del asegurado",
            "dni": "Número de DNI de 8 dígitos",
            "poliza": "Número exacto de la póliza",
            "compania": "RUS o ANTARTIDA",
            "tipo_seguro": "Automotor o Moto",
            "patente": "Patente del vehículo",
            "vehiculo": "Marca y modelo",
            "vigencia_desde": "DD/MM/AAAA",
            "vigencia_hasta": "DD/MM/AAAA",
            "periodo_facturacion": "Ej: Bimestral, Bimestral 2, Mensual",
            "forma_pago": "Efectivo o Tarjeta de Crédito / Débito"
        }}
        
        Texto de la póliza a analizar:
        {texto_extraido}
        """

        # 2. Llamada a Gemini usando nuestra Arquitectura Defensiva
        texto_crudo = ia_motor.procesar_prompt(prompt)
        
        # 3. Extracción limpia del JSON
        texto_json = texto_crudo.strip()
        if texto_json.startswith("```json"):
            texto_json = texto_json[7:-3] 
        elif texto_json.startswith("```"):
            texto_json = texto_json[3:-3]
            
        datos_poliza = json.loads(texto_json)
        
        datos_poliza["poliza"] = str(datos_poliza.get("poliza", "")).strip()
        
        # =========================================================
        # 4. SUBIDA DIRECTA A LA API REST (PUENTEANDO LA LIBRERÍA)
        # =========================================================
        datos_poliza["pdf_url"] = None 
        
        # CHECK DE DUPLICADOS Y RENOVACIONES EN LA BASE DE DATOS
        db = SessionLocal()
        try:
            poliza_db = db.query(Poliza).filter(Poliza.numero_poliza == datos_poliza["poliza"]).first()
            if poliza_db:
                # Comparamos las fechas
                try:
                    fecha_hasta_nueva = datetime.strptime(datos_poliza['vigencia_hasta'], "%d/%m/%Y").date()
                    if fecha_hasta_nueva > poliza_db.fecha_fin_vigencia:
                        datos_poliza["estado_db"] = "renovacion"
                        datos_poliza["mensaje_db"] = "✅ Renovación detectada: Se actualizarán las fechas."
                    else:
                        datos_poliza["estado_db"] = "duplicado"
                        datos_poliza["mensaje_db"] = "⚠️ Esta póliza ya existe con la misma vigencia (o una superior)."
                except Exception:
                    # Si falla el parseo de fecha, por las dudas marcamos como duplicado
                    datos_poliza["estado_db"] = "duplicado"
                    datos_poliza["mensaje_db"] = "⚠️ Póliza ya existente en el sistema."
            else:
                datos_poliza["estado_db"] = "nueva"
                datos_poliza["mensaje_db"] = "✨ Póliza nueva lista para guardar."
        finally:
            db.close()
        
        try:
            import requests  # <--- ACÁ ESTÁ DE VUELTA LA MAGIA
            
            # Limpiamos el nombre para que la URL no se rompa
            nombre_archivo = f"{datos_poliza.get('compania', 'SD')}_{datos_poliza.get('poliza', 'SD')}.pdf"
            nombre_archivo = nombre_archivo.replace(" ", "_").replace("/", "-")
            
            url_subida = f"{SUPABASE_URL}/storage/v1/object/polizas/{nombre_archivo}"
            
            # Cabeceras obligatorias imitando el comportamiento de la librería
            headers = {
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey": SUPABASE_KEY,
                "Content-Type": "application/pdf",
                "x-upsert": "true"
            }
            
            # Disparamos el PDF en formato binario directo a la nube
            respuesta = requests.post(url_subida, headers=headers, data=contenido_pdf)
            
            if respuesta.status_code == 200:
                # Si devuelve 200 OK, armamos el link público a mano
                url_publica = f"{SUPABASE_URL}/storage/v1/object/public/polizas/{nombre_archivo}"
                datos_poliza["pdf_url"] = url_publica
            else:
                # Si sigue fallando, nos dirá por qué
                print(f"⚠️ RECHAZO REAL DE SUPABASE: Código {respuesta.status_code} - {respuesta.text}")
                
        except Exception as e_req:
            print(f"⚠️ Error conectando con la API de Supabase: {str(e_req)}")

        return {"mensaje": "Póliza procesada", "datos": datos_poliza}

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="La IA no devolvió un JSON válido.")
    except Exception as e:
        print(f"🚨 ERROR CRÍTICO EN IA: {str(e)}")
        raise HTTPException(status_code=500, detail=f"La IA falló: {str(e)}")

@app.post("/api/admin/ingesta-masiva")
async def ingesta_masiva(
    file: UploadFile = File(...),
    compania: str = Form(...),
    usuario: dict = Depends(obtener_usuario_actual)
):
    import tempfile
    import shutil
    
    if usuario["rol"].lower() not in ["admin", "superadmin"]:
        raise HTTPException(status_code=403, detail="Acceso exclusivo para administradores")
        
    suffix = os.path.splitext(file.filename)[1].lower()
    if suffix not in ['.csv', '.xls', '.xlsx']:
        raise HTTPException(status_code=400, detail="El archivo debe ser un CSV, XLS o XLSX")

    db = SessionLocal()
    temp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
            temp_file_path = tmp.name

        df = procesar_archivo_seguros(temp_file_path, compania)
        
        if df.empty:
            raise HTTPException(status_code=400, detail="El archivo está vacío o no contiene registros válidos")

        procesados = 0
        admin_id = usuario.get("sub")
        admin_rol = usuario.get("rol")
        
        sucursal_admin_id = usuario.get("sucursal_id")
        if admin_rol.lower() in ["admin", "superadmin"]:
            admin_obj = db.query(UsuarioAdmin).filter(UsuarioAdmin.id == admin_id).first()
            if admin_obj:
                sucursal_admin_id = str(admin_obj.sucursal_id) if admin_obj.sucursal_id else None

        for _, row in df.iterrows():
            dni_limpio = str(row['dni']).strip()
            if not dni_limpio:
                continue

            # 1. COMPAÑÍA
            cia_nombre = row['compania_nombre']
            cia = db.query(Compania).filter(Compania.nombre == cia_nombre).first()
            if not cia:
                cia = Compania(nombre=cia_nombre, is_active=True)
                db.add(cia)
                db.flush()

            # 2. CLIENTE
            cliente = db.query(Cliente).filter(Cliente.dni == dni_limpio).first()
            if not cliente:
                cliente = Cliente(
                    nombre_completo=row['nombre_completo'],
                    dni=dni_limpio,
                    telefono=row['telefono'] or "",
                    email=row['email'] or "",
                    is_active=True,
                    sucursal_id=sucursal_admin_id
                )
                db.add(cliente)
                db.flush()
            else:
                if row['telefono'] and not cliente.telefono:
                    cliente.telefono = row['telefono']
                if row['email'] and not cliente.email:
                    cliente.email = row['email']
                if not cliente.sucursal_id and sucursal_admin_id:
                    cliente.sucursal_id = sucursal_admin_id
                db.flush()

            # 3. PÓLIZA (UPSERT)
            numero_poliza_limpio = str(row['numero_poliza']).strip()
            fecha_fin = row['fecha_fin_vigencia']
            if pd.isna(fecha_fin) or not fecha_fin:
                fecha_fin = datetime.now().date()

            poliza_existente = db.query(Poliza).filter(Poliza.numero_poliza == numero_poliza_limpio).first()
            if poliza_existente:
                poliza_existente.fecha_fin_vigencia = fecha_fin
                poliza_existente.saldo_adeudado = float(row['saldo_adeudado'] or 0)
                
                bien = db.query(BienAsegurado).filter(BienAsegurado.poliza_id == poliza_existente.id).first()
                if bien:
                    bien.patente = row['patente'] if pd.notna(row['patente']) else bien.patente
                    bien.descripcion_modelo = row['descripcion_modelo'] if row['descripcion_modelo'] else bien.descripcion_modelo
            else:
                nueva_poliza = Poliza(
                    cliente_id=cliente.id,
                    compania_id=cia.id,
                    numero_poliza=numero_poliza_limpio,
                    fecha_fin_vigencia=fecha_fin,
                    estado_vigencia="VIGENTE",
                    saldo_adeudado=float(row['saldo_adeudado'] or 0),
                    is_enabled=True,
                    periodo_facturacion="S/D",
                    forma_pago="S/D"
                )
                db.add(nueva_poliza)
                db.flush()

                nuevo_bien = BienAsegurado(
                    poliza_id=nueva_poliza.id,
                    tipo=row['tipo'] or 'Automotor',
                    descripcion_modelo=row['descripcion_modelo'] or 'Ver Póliza',
                    patente=row['patente'] if pd.notna(row['patente']) else None,
                    detalles=row['detalles_bien'] or {}
                )
                db.add(nuevo_bien)

            procesados += 1
            
        db.commit()
        return {"status": "success", "procesados": procesados}

    except Exception as e:
        db.rollback()
        logger.error(f"🚨 Error en ingesta masiva: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception:
                pass

@app.post("/api/save-poliza")
async def save_poliza(datos: dict, usuario: dict = Depends(obtener_usuario_actual)):
    db = SessionLocal()
    try:
        # 1. MANTENEMOS TU LÓGICA DE USUARIO Y SUCURSAL
        admin_id = usuario.get("sub")
        admin_rol = usuario.get("rol")
        
        sucursal_admin_id = usuario.get("sucursal_id")
        if admin_rol in ["ADMIN", "SUPERADMIN"]:
            admin_obj = db.query(UsuarioAdmin).filter(UsuarioAdmin.id == admin_id).first()
            if admin_obj:
                # Forzamos conversión a string limpio por si las moscas
                sucursal_admin_id = str(admin_obj.sucursal_id) if admin_obj.sucursal_id else None

        numero_poliza_limpio = str(datos.get('poliza', '')).strip()
        datos['poliza'] = numero_poliza_limpio
        
        # 2. PROCESAMIENTO DE FECHAS
        fecha_desde = datetime.strptime(datos['vigencia_desde'], "%d/%m/%Y").date()
        fecha_hasta = datetime.strptime(datos['vigencia_hasta'], "%d/%m/%Y").date()

        # 3. MANTENEMOS TU LÓGICA DE CLIENTE
        cliente = db.query(Cliente).filter(Cliente.dni == datos['dni']).first()
        
        if not cliente:
            cliente = Cliente(
                nombre_completo=datos['nombre'],
                dni=datos['dni'],
                telefono="", 
                is_active=True,
                sucursal_id=sucursal_admin_id 
            )
            db.add(cliente)
            db.flush()
        else:
            if not cliente.sucursal_id and sucursal_admin_id:
                cliente.sucursal_id = sucursal_admin_id
                db.flush()

        # 4. MANTENEMOS TU LÓGICA DE COMPAÑÍA
        compania_id = None
        if datos.get('compania'):
            cia = db.query(Compania).filter(Compania.nombre == datos['compania']).first()
            if not cia:
                cia = Compania(nombre=datos['compania'], is_active=True)
                db.add(cia)
                db.flush()
            compania_id = cia.id

        # 5. NUEVA LÓGICA DE UPSERT (ACTUALIZAR O INSERTAR PÓLIZA)
        poliza_existente = db.query(Poliza).filter(Poliza.numero_poliza == numero_poliza_limpio).first()
        
        if poliza_existente:
            # Es una RENOVACIÓN o actualización: Pisamos los datos viejos
            poliza_existente.fecha_inicio = fecha_desde
            poliza_existente.fecha_fin_vigencia = fecha_hasta
            poliza_existente.periodo_facturacion = datos.get('periodo_facturacion', poliza_existente.periodo_facturacion)
            poliza_existente.forma_pago = datos.get('forma_pago', poliza_existente.forma_pago)
            
            # Si subimos un PDF nuevo, actualizamos el link
            if datos.get('pdf_url'):
                poliza_existente.pdf_url = datos['pdf_url']

            # Actualizamos el vehículo por si cambió
            bien = db.query(BienAsegurado).filter(BienAsegurado.poliza_id == poliza_existente.id).first()
            if bien:
                bien.patente = datos.get('patente', bien.patente)
                bien.descripcion_modelo = datos.get('vehiculo', bien.descripcion_modelo)
        else:
            # Es una póliza NUEVA: Creamos todo desde cero
            nueva_poliza = Poliza(
                cliente_id=cliente.id,
                compania_id=compania_id,
                numero_poliza=datos['poliza'],
                fecha_inicio=fecha_desde,
                fecha_fin_vigencia=fecha_hasta,
                estado_vigencia="VIGENTE",
                saldo_adeudado=0,
                is_enabled=True,
                periodo_facturacion=datos.get('periodo_facturacion', 'S/D'),
                forma_pago=datos.get('forma_pago', 'S/D'),
                pdf_url=datos.get('pdf_url', None)  
            )
            db.add(nueva_poliza)
            db.flush()

            nuevo_bien = BienAsegurado(
                poliza_id=nueva_poliza.id,
                tipo=datos.get('tipo_seguro', 'Automotor'),
                descripcion_modelo=datos.get('vehiculo', ''),
                patente=datos.get('patente', ''),
                detalles={}
            )
            db.add(nuevo_bien)

        # 6. CIERRE DE LA TRANSACCIÓN
        db.commit()
        return {"status": "success", "message": "Póliza procesada correctamente"}

    except Exception as e:
        db.rollback()
        print(f"🚨 Error en save-poliza: {str(e)}") 
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.put("/api/mensajes/{id_mensaje}/leer")
def marcar_mensaje_leido(id_mensaje: str, usuario: dict = Depends(obtener_usuario_actual)):
    db = SessionLocal()
    try:
        # Buscamos el mensaje por su UUID
        mensaje = db.query(Mensaje).filter(Mensaje.id == id_mensaje).first()
        if not mensaje:
            raise HTTPException(status_code=404, detail="Mensaje no encontrado")
            
        # Validación estricta: si es cliente, comprobamos que el mensaje le pertenezca a su DNI
        if usuario["rol"] == "cliente" and str(mensaje.cliente.dni) != usuario["sub"]:
            raise HTTPException(status_code=403, detail="No tienes permiso para modificar este mensaje")

        # Si no está leído, lo actualizamos y disparamos el commit a Neon DB
        if not mensaje.leido:
            mensaje.leido = True
            db.commit()
            
        return {"success": True, "leido": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
# Para ejecutar: uvicorn main:app --reload