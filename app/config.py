"""Configuração da aplicação via environment variables"""

from pydantic import field_validator
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
    # Presença humana: marca mensagens como lidas (tique azul), mostra "digitando…"
    # e espaça as bolhas no tempo. Ligado na produção (env); desligado por padrão
    # pra não atrasar testes/dev nem fazer chamadas de rede fora de hora.
    simular_digitacao: bool = False
    # Janela de agrupamento (debounce) por conversa: a Sofia espera N segundos de
    # silêncio antes de responder, pra juntar rajadas de mensagens numa resposta
    # só. Configurável no Render sem mexer no código (sugestão 4 a 8). Crise não
    # espera essa janela.
    debounce_segundos: float = 5.0
    # Transcrever áudio do paciente (Whisper) e responder em texto, em vez de
    # escalar. Desligado por padrão (custo); editável no painel (Configurações).
    transcrever_audio: bool = False

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
    # Modelo de transcrição de áudio (STT). whisper-1 é robusto e barato;
    # gpt-4o-mini-transcribe é uma alternativa mais nova.
    openai_audio_model: str = "whisper-1"
    # Temperature da geração. Opcional: deixe vazio (ou "none"/"default") pra NÃO
    # enviar o parâmetro e usar o padrão do modelo — alguns modelos novos (de
    # raciocínio) só aceitam o padrão e rejeitam um valor custom. Se um modelo
    # rejeitar o valor configurado, o llm_client reenvia sem temperature sozinho.
    openai_temperature: float | None = 0.7

    @field_validator("openai_temperature", mode="before")
    @classmethod
    def _temperature_opcional(cls, v):
        if isinstance(v, str) and v.strip().lower() in (
            "",
            "none",
            "default",
            "padrao",
            "padrão",
            "off",
        ):
            return None
        return v

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

    # Stripe (links de pagamento gerados no painel). Vazio = feature desligada,
    # a tela de Pagamentos mostra um aviso em vez de quebrar.
    stripe_secret_key: str = ""
    # Publishable key (pk_...). O checkout hospedado do Stripe NÃO usa; fica
    # aqui só pra config ficar completa/documentada junto com as outras.
    stripe_publishable_key: str = ""
    # Preço mensal já cadastrado no Stripe (price_...). Quando definido e a
    # mensalidade pedida bate com o valor dele, a assinatura reusa esse preço
    # (relatórios unificados com o site da Allos) em vez de criar um novo.
    stripe_preco_mensal_id: str = ""
    # URL pública da Sofia — monta as páginas de retorno do checkout
    # (/pagamento-sucesso e /pagamento-cancelado). Sem barra no final.
    base_url: str = "https://sofia-whatsapp.onrender.com"

    # Painel
    painel_user: str = "thaina"
    painel_password: str

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
