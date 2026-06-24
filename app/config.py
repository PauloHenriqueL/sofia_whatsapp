"""Configuração da aplicação via environment variables"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings da aplicação - carrega do .env"""

    # App
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 5000
    log_level: str = "INFO"
    log_json: bool = False
    secret_key: str = "dev-secret-change-in-prod"
    # Token do endpoint de tarefas (cron externo dispara os follow-ups).
    # Vazio = endpoint desligado (responde 403). Defina no Render pra ativar.
    tasks_token: str = ""

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

    # Valores de negócio (mudáveis no Render, sem mexer no código). São injetados
    # no que a Sofia fala via llm_client.carregar_system_prompt().
    preco_terapia_mensal: int = 200
    preco_neuro: int = 1200
    parcelas_max: int = 5
    followup_horas: int = 20  # retorno automático de lead parado (Frente 2)

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
