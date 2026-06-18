"""Configuração da aplicação via environment variables"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings da aplicação - carrega do .env"""

    # App
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 5000
    log_level: str = "INFO"
    secret_key: str = "dev-secret-change-in-prod"

    # WhatsApp
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str
    whatsapp_app_secret: str
    thaina_whatsapp_number: str
    alert_template_name: str = "alerta_thaina"

    # OpenAI
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    # Database
    database_url: str

    # Hamilton (auth JWT: username/password -> token Bearer)
    hamilton_api_url: str
    hamilton_api_key: str = ""
    hamilton_username: str = ""
    hamilton_password: str = ""

    # Painel
    painel_user: str = "thaina"
    painel_password: str

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
