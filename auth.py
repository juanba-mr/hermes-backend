import os
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from dotenv import load_dotenv

load_dotenv()

# Configuración secreta
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "una_clave_super_secreta_12345") # Cambiala en tu .env
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 # El token dura una semana

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def crear_token_acceso(data: dict):
    """Genera el pase digital (JWT)"""
    a_copiar = data.copy()
    expira = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    a_copiar.update({"exp": expira})
    return jwt.encode(a_copiar, SECRET_KEY, algorithm=ALGORITHM)

def verificar_token(token: str):
    """Valida si el pase digital es auténtico y no venció"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload # Retorna los datos del usuario (DNI, rol, etc)
    except JWTError:
        return None