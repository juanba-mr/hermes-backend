from datetime import datetime
from typing import Optional
from sqlalchemy import Column, String, Integer, Date, Numeric, ForeignKey, DateTime, Index, Boolean, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship
import uuid

Base = declarative_base()

class Cliente(Base):
    __tablename__ = 'clientes'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dni = Column(String(20), unique=True, nullable=False, index=True)
    nombre_completo = Column(String(150), nullable=False)
    telefono = Column(String(50), nullable=True)
    email = Column(String(100), nullable=True)
    perfil_extra = Column(JSONB, default=dict) 
    
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    polizas = relationship("Poliza", back_populates="cliente", cascade="all, delete-orphan")


class Compania(Base):
    __tablename__ = 'companias'

    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(String(100), unique=True, nullable=False)
    telefono_asistencia = Column(String(50), nullable=True)
    
    is_active = Column(Boolean, default=True)
    polizas = relationship("Poliza", back_populates="compania")


class Poliza(Base):
    __tablename__ = 'polizas'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    numero_poliza = Column(String(100), unique=True, nullable=False, index=True)
    
    cliente_id = Column(UUID(as_uuid=True), ForeignKey('clientes.id'), nullable=False)
    compania_id = Column(Integer, ForeignKey('companias.id'), nullable=False)
    
    fecha_inicio = Column(Date, nullable=True)
    fecha_fin_vigencia = Column(Date, nullable=False, index=True)
    
    premio_cotizado = Column(Numeric(19, 4), nullable=True)
    saldo_adeudado = Column(Numeric(19, 4), nullable=True)
    estado_vigencia = Column(String(30), default='VIGENTE') 
    
    datos_especificos = Column(JSONB, default=dict)
    
    periodo_facturacion = Column(String(50), nullable=True, default="S/D")
    forma_pago = Column(String(50), nullable=True, default="S/D")
    pdf_url = Column(String, nullable=True)
    
    is_enabled = Column(Boolean, default=True, nullable=False) 
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    cliente = relationship("Cliente", back_populates="polizas")
    compania = relationship("Compania", back_populates="polizas")
    bien_asegurado = relationship("BienAsegurado", uselist=False, back_populates="poliza")

    __table_args__ = (
        Index('ix_poliza_active_lookup', 'numero_poliza', 'is_enabled'),
    )


class BienAsegurado(Base):
    __tablename__ = 'bienes_asegurados'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    poliza_id = Column(UUID(as_uuid=True), ForeignKey('polizas.id'), nullable=False, unique=True)
    
    tipo = Column(String(50), nullable=False) 
    descripcion_modelo = Column(String(200), nullable=True)
    patente = Column(String(20), nullable=True, index=True)
    detalles = Column(JSONB, default=dict)

    poliza = relationship("Poliza", back_populates="bien_asegurado")
    
class UsuarioAdmin(Base):
    __tablename__ = 'usuarios_admin'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Su "DNI" interno, ej: 99000001
    dni_acceso = Column(String(20), unique=True, nullable=False, index=True) 
    nombre_completo = Column(String(150), nullable=False)
    
    # Acá está la magia: 'SUPERADMIN' (Franci) o 'EMPLEADO' (Resto del equipo)
    rol = Column(String(50), default='EMPLEADO', nullable=False) 
    
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
class SuscripcionPush(Base):
    __tablename__ = 'suscripciones_push'

    id = Column(Integer, primary_key=True, index=True)
    cliente_dni = Column(String(20), index=True) # A quién pertenece este celular
    datos_navegador = Column(Text, nullable=False) # El JSON gigante que nos da Chrome/Safari
    created_at = Column(DateTime, default=datetime.utcnow)