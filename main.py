from fastapi import FastAPI, HTTPException, Depends, Security, File, UploadFile
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

# Importamos tus modelos (asegúrate de que el archivo se llame models.py)
from models import Cliente, Poliza, Compania, BienAsegurado, UsuarioAdmin, SuscripcionPush, Sucursal, Mensaje

# 1. Configuración de Base de Datos
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

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

# 2. Configuración de CORS
# Esto permite que tu frontend en el puerto 5173 pueda hablar con este servidor
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
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
                "nombre": admin.nombre_completo
            })
            return {
                "success": True,
                "token": token, # <--- ¡Acá va el pase!
                "tipo_usuario": "admin",
                "usuario": {
                    "id": str(admin.id),
                    "nombre": admin.nombre_completo,
                    "dni": admin.dni_acceso,
                    "rol": admin.rol
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

        clave_ia = os.getenv("GEMINI_API_KEY_PDF")
        if not clave_ia:
            raise HTTPException(status_code=500, detail="Falta la API Key en el archivo .env")
            
        client = genai.Client(api_key=clave_ia)
        
        # PROMPT (Tu versión mejorada)
        prompt = f"""
        Eres un asistente experto y analítico en seguros de Argentina.
        Tu tarea es leer la póliza adjunta, deducir la información oculta siguiendo ESTRICTAMENTE las reglas de negocio, y devolver los datos en formato JSON.

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

        # 2. Llamada a Gemini
        response = client.models.generate_content(
            model='gemini-flash-latest',
            contents=prompt
        )
        
        # 3. Tu extracción vieja y confiable
        texto_json = response.text.strip()
        if texto_json.startswith("```json"):
            texto_json = texto_json[7:-3] 
        elif texto_json.startswith("```"):
            texto_json = texto_json[3:-3]
            
        datos_poliza = json.loads(texto_json)
        
        # =========================================================
        # 4. SUBIDA A SUPABASE CON MANEJO DE ERRORES SEGURO
        # =========================================================
        datos_poliza["pdf_url"] = None 
        
        try:
            import requests
            
            # Limpiamos el nombre para que la URL no se rompa
            nombre_archivo = f"{datos_poliza.get('compania', 'SD')}_{datos_poliza.get('poliza', 'SD')}.pdf"
            nombre_archivo = nombre_archivo.replace(" ", "_").replace("/", "-")
            
            SUPABASE_URL = os.getenv("SUPABASE_URL")
            SUPABASE_KEY = os.getenv("SUPABASE_KEY")
            
            # EL FIX ESTÁ ACÁ: El path correcto de la API es /storage/v1/object/nombre_del_bucket/nombre_del_archivo
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

@app.post("/api/save-poliza")
async def save_poliza(datos: dict):
    db = SessionLocal()
    try:
        fecha_desde = datetime.strptime(datos['vigencia_desde'], "%d/%m/%Y").date()
        fecha_hasta = datetime.strptime(datos['vigencia_hasta'], "%d/%m/%Y").date()

        cliente = db.query(Cliente).filter(Cliente.dni == datos['dni']).first()
        if not cliente:
            cliente = Cliente(
                nombre_completo=datos['nombre'],
                dni=datos['dni'],
                telefono="", 
                is_active=True
                # sucursal_id podría definirse por defecto acá si se requiere más adelante
            )
            db.add(cliente)
            db.flush()

        compania_id = None
        if datos.get('compania'):
            cia = db.query(Compania).filter(Compania.nombre == datos['compania']).first()
            if not cia:
                cia = Compania(nombre=datos['compania'], is_active=True)
                db.add(cia)
                db.flush()
            compania_id = cia.id

        # ACA AGREGAMOS LOS TRES CAMPOS NUEVOS
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
            pdf_url=datos.get('pdf_url', None)  # <--- GUARDADO EN NEON DB
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

        db.commit()
        return {"status": "success", "message": "Póliza y PDF vinculados correctamente"}
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