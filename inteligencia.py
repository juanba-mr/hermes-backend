import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

# Configuramos el cliente con la nueva librería
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def generar_mensaje_renovacion(nombre_cliente, vehiculo, dias_restantes, compania):
    """
    Toma los datos fríos de Neon y le pide a Gemini que redacte un mensaje persuasivo.
    """
    prompt = f"""
    Actúa como un productor de seguros empático, profesional y de confianza de la agencia 'Hermes Seguros'.
    Tu objetivo es redactar una notificación corta (máximo 250 caracteres) para enviarle al celular de un cliente.
    El mensaje debe avisarle que su póliza está por vencer, transmitirle tranquilidad y motivarlo sutilmente a contactarnos para renovar.

    DATOS DEL CLIENTE:
    - Nombre (o Apellido): {nombre_cliente}
    - Vehículo asegurado: {vehiculo}
    - Compañía actual: {compania}
    - Vence en: {dias_restantes} días

    REGLAS DE ESTILO:
    - Tono: Amigable, argentino (usá 'vos', 'tu póliza'), directo.
    - No suenes alarmista ni uses lenguaje legal aburrido.
    - Usá máximo un (1) emoji relacionado a autos o tranquilidad.
    - Mantenelo MUY breve porque es una notificación push de celular.
    """
    
    try:
        # Usamos el alias para tener siempre la última versión de Flash
        respuesta = client.models.generate_content(
            model='gemini-flash-latest',
            contents=prompt
        )
        return respuesta.text.strip()
    except Exception as e:
        print(f"Error con Gemini: {e}")
        return f"Hola {nombre_cliente.split(' ')[0]}, tu seguro del {vehiculo} vence en {dias_restantes} días. ¡Contactanos para renovarlo!"