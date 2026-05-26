# 🛡️ Hermes Seguros - Backend API & IA Engine

![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
![PostgreSQL](https://img.shields.io/badge/postgresql-4169e1?style=for-the-badge&logo=postgresql&logoColor=white)
![Supabase](https://img.shields.io/badge/Supabase-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white)
![Google Gemini](https://img.shields.io/badge/Google%20Gemini-8E75B2?style=for-the-badge&logo=google%20gemini&logoColor=white)

Este repositorio contiene el motor principal (Backend) de la plataforma Hermes Seguros. Está construido sobre **FastAPI**, ofreciendo endpoints de altísimo rendimiento, y cuenta con un sistema de arquitectura híbrida en la nube y procesamiento de lenguaje natural (NLP) para la automatización de la ingesta de datos.

![Arquitectura](link-a-una-imagen-de-arquitectura-o-consola-aca)
> *Log de procesamiento de Ingesta Masiva.*

## ✨ Características Principales

- **🧠 Motor de Ingesta con IA (Google Gemini):** Análisis profundo de documentos PDF (Pólizas) en memoria RAM. Extrae metadatos y deduce complejas reglas de negocio (Período de Facturación, Forma de Pago) a través de modelos de lenguaje grande.
- **☁️ Almacenamiento Híbrido:** Puenteo directo (API Rest Bypass) hacia **Supabase Storage** para la persistencia segura de los documentos originales en la nube.
- **🗄️ Base de Datos Relacional:** Gestión robusta de relaciones (Clientes, Pólizas, Bienes Asegurados, Compañías) usando SQLAlchemy y PostgreSQL alojado en **Neon DB**.
- **🔔 Sistema de Notificaciones Inteligentes:** Generación de mensajes persuasivos automáticos (IA) para renovaciones y disparo de alertas nativas mediante Web Push API.
- **🔐 Seguridad y Autenticación:** Manejo de sesiones basadas en JSON Web Tokens (JWT) y Middlewares de protección de rutas por roles (Cliente/Admin).

## 🛠️ Stack Tecnológico

- **Framework:** FastAPI (Python 3.10+)
- **ORM & Database:** SQLAlchemy, Pycopg2 (PostgreSQL)
- **Inteligencia Artificial:** Google GenAI SDK (`gemini-flash-latest`)
- **Cloud Storage:** Supabase REST API
- **Push Notifications:** PyWebPush
- **Procesamiento de Archivos:** PyPDF2
