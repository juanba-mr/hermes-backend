from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, joinedload
from datetime import date, timedelta
from inteligencia import generar_mensaje_renovacion
from notificaciones import enviar_alerta_push
import json
from pywebpush import webpush, WebPushException
from auth import crear_token_acceso, verificar_token

# Importamos tus modelos (asegúrate de que el archivo se llame models.py)
from models import Cliente, Poliza, Compania, BienAsegurado, UsuarioAdmin, SuscripcionPush

# 1. Configuración de Base de Datos
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# Neon requiere sslmode=require para conexiones seguras
engine = create_engine(DATABASE_URL, connect_args={"sslmode": "require"})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

app = FastAPI(title="Backend Hermes Seguros")

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
        # Usamos joinedload para traer las pólizas y compañías de un solo viaje
        cliente = db.query(Cliente).filter(Cliente.dni == dni).first()
        
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
                    "asistencia_telefono": cia.telefono_asistencia if cia else "0800-XXX-XXXX"
                })

        return {
            "dni": cliente.dni,
            "nombre": cliente.nombre_completo,
            "polizas": polizas_data
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
            "cobertura": poliza.datos_especificos.get('cobertura_completa', "Consultar con productor")
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
        clientes = db.query(Cliente).all()
        result = []
        for c in clientes:
            # Calculamos cuántas pólizas tiene cada uno
            cant_polizas = len([p for p in c.polizas if p.is_enabled])
            result.append({
                "id": str(c.id),
                "dni": c.dni,
                "nombre": c.nombre_completo,
                "telefono": c.telefono or "Sin teléfono",
                "cant_polizas": cant_polizas,
                "estado": "Activo" if c.is_active else "Inactivo"
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
        
        enviado = enviar_alerta_push(
            suscripcion_json=suscripcion_real, # <--- USAMOS LA REAL
            titulo="🔔 Hermes Asesores",
            cuerpo_mensaje=mensaje_magico
        )
        
        return {"success": enviado, "mensaje_generado": mensaje_magico}
    finally:
        db.close()
# Para ejecutar: uvicorn main:app --reload