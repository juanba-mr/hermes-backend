from pywebpush import webpush, WebPushException
import json
import os
from urllib.parse import urlparse # Importante para sacar el 'aud'
from dotenv import load_dotenv

load_dotenv()

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_MAIL = os.getenv("VAPID_MAIL")

def enviar_alerta_push(suscripcion_json, titulo, cuerpo_mensaje):
    try:
        # Extraemos la dirección base (aud) del endpoint del navegador
        endpoint = suscripcion_json.get('endpoint')
        parsed_url = urlparse(endpoint)
        aud = f"{parsed_url.scheme}://{parsed_url.netloc}"

        webpush(
            subscription_info=suscripcion_json,
            data=json.dumps({
                "title": titulo,
                "body": cuerpo_mensaje,
                "icon": "/icon-192x192.png",
                "badge": "/icon-192x192.png",
                "url": "/dashboard"
            }),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={
                "sub": VAPID_MAIL,
                "aud": aud # Agregamos esto para que py_vapid no se queje
            }
        )
        return True
    except WebPushException as ex:
        print(f"Fallo el envío Push: {repr(ex)}")
        return False