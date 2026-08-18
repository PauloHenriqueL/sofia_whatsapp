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
    # DRY RUN: não chama a Meta, só loga o que teria sido enviado.
    #
    # O `.env` de desenvolvimento carrega o token REAL do número da Allos. Sem
    # esta trava, `uvicorn app.main:app` no laptop de qualquer pessoa manda
    # mensagem de verdade pra paciente de verdade, e o alerta cai no celular da
    # Thainá — sem nenhum aviso de que aquilo não era um teste.
    #
    # `None` = decide pelo ambiente: liga sozinho fora de `production`. Seguro
    # por omissão; quem precisa enviar de verdade em dev seta `false` de propósito.
    whatsapp_dry_run: bool | None = None

    @property
    def envio_whatsapp_bloqueado(self) -> bool:
        if self.whatsapp_dry_run is not None:
            return self.whatsapp_dry_run
        return self.environment.strip().lower() != "production"

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
    # Esforço de raciocínio dos modelos novos (gpt-5.x): "none" | "low" | "medium" |
    # "high" | "xhigh" | "max". Vazio = não envia o parâmetro (usa o padrão do
    # modelo, que no 5.6 é "medium").
    #
    # `none` na CONVERSA de propósito: o turno da Sofia é seguir roteiro e escolher
    # tool, não raciocinar. Com o padrão "medium" cada turno ganharia segundos de
    # latência — em cima do debounce que já existe — e os tokens de raciocínio são
    # cobrados como saída. Na EXTRAÇÃO da pesquisa (conversa -> JSON) vale o
    # contrário: ninguém está esperando a resposta e errar ali contamina relatório.
    #
    # Mora aqui e não no /painel/config porque quem mede isso (com o `laboratorio/`)
    # é quem mexe no deploy, não a Thainá — e "esforço de raciocínio" numa tela
    # operacional é um botão sem significado pra quem opera.
    openai_reasoning_effort: str = "none"
    openai_reasoning_effort_extracao: str = "low"

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
    preco_neuro: int = 1000
    parcelas_max: int = 5
    # Desconto máximo que a Sofia pode oferecer sozinha na mensalidade da terapia,
    # em %. Vale só pra terapia: na neuroavaliação quem negocia é a Amanda, na
    # reunião. 0 desliga o desconto e toda objeção de preço vai pra Thainá.
    desconto_maximo_pct: int = 10
    followup_horas: int = 20  # retorno automático de lead parado (Frente 2)

    # Database
    database_url: str

    # Hamilton (auth JWT: username/password -> token Bearer)
    hamilton_api_url: str
    hamilton_api_key: str = ""
    hamilton_username: str = ""
    hamilton_password: str = ""
    # Conexão SQL direta com a branch de TESTE do banco do Hamilton. Nenhum
    # código da app usa isto (a Sofia fala com o Hamilton por REST) — existe só
    # pra inspecionar/alterar schema e dados de teste durante o desenvolvimento.
    # Declarado aqui porque Settings rejeita chaves desconhecidas no .env.
    database_hamilton_teste: str = ""

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
    # Chaves do MODO DE TESTE do Stripe. Fora de `production`, são ELAS que a app
    # usa (ver `stripe_key` abaixo). NÃO setar no Render.
    test_stripe_secret_key: str = ""
    test_stripe_publishable_key: str = ""

    @property
    def stripe_modo_teste(self) -> bool:
        """Fora de produção, o Stripe é o de teste. Sem exceção e sem escotilha."""
        return self.environment.strip().lower() != "production"

    @property
    def stripe_key(self) -> str:
        """A chave que o código DEVE usar. **Nunca leia `stripe_secret_key` direto.**

        🔴 Esta propriedade existe porque o `.env` de desenvolvimento carrega uma
        chave `sk_live_`, e o Stripe não tem um "dry run" como a Meta: toda
        chamada cria coisa de verdade. Sem isto, rodar a app no laptop — ou um
        teste mal mockado — cria Payment Link, preço e assinatura na conta real
        da Allos. Já aconteceu **duas vezes**: um `pytest` criou quatro Payment
        Links, e a validação da cobrança criou mais um em 17/08.

        Fora de produção devolve a chave de TESTE. Se ela estiver vazia, devolve
        **vazio** — e o Stripe fica desligado, que é um estado que a app já sabe
        viver (a tela de Pagamentos mostra aviso, a cobrança marca `erro_link` e
        oferece só o Pix). **Nunca cai pra chave live**: era esse o buraco.

        Não há escotilha de propósito. A do WhatsApp existe porque há um caso
        legítimo (mandar mensagem pro próprio número num teste); aqui não há —
        pra ver dado real existe o dashboard do Stripe. Escotilha acaba ligada e
        esquecida.
        """
        return self.test_stripe_secret_key if self.stripe_modo_teste else self.stripe_secret_key

    # URL pública da Sofia — monta as páginas de retorno do checkout
    # (/pagamento-sucesso e /pagamento-cancelado). Sem barra no final.
    base_url: str = "https://sofia-whatsapp.onrender.com"
    # Prefixo do link curto de pagamento. Vazio = os links saem apontando pra
    # própria Sofia (`{base_url}/l`), que funciona igual — só não tem o domínio
    # da Allos. Assim o deploy daqui não fica preso ao deploy do site.
    # Em produção: https://allos.org.br/p
    link_curto_base: str = ""

    # Autentique (assinatura eletrônica do contrato terapêutico — Demanda E).
    #
    # ⚠️ TEMPORÁRIO NESTE REPO. Quem fala com a Autentique é o HAMILTON (é lá que
    # o contrato é gerado e o assinado é guardado); esta chave sai daqui quando a
    # Demanda E subir. Está declarada porque o `.env` já a tem, e o pydantic-settings
    # recusa chave desconhecida em arquivo de env (`extra_forbidden`) — sem isto
    # `Settings()` explode no import e NADA sobe, nem os testes.
    api_authentic: str = ""

    # Painel
    painel_user: str = "thaina"
    painel_password: str

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
