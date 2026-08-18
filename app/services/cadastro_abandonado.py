"""Resgata quem passou os dados e sumiu antes de confirmar.

O ritual de confirmação existe pra não gravar dado errado no Hamilton ("deixa eu
confirmar rapidinho… tá tudo certo assim?"), e ele funciona. O custo apareceu
medido no laboratório: em 2 de 3 conversas da persona `chega-com-a-dor` a pessoa
**saiu satisfeita exatamente no "tá tudo certo?"** — passou nome, nascimento e
horários, entendeu o próximo passo, e foi embora. Do lado de fora ela acha que
está encaminhada. Do lado de dentro não existe ficha nenhuma.

Depois de 24h de silêncio, este módulo relê a conversa, extrai o que ela contou e
**deixa a ficha pronta na tela Hoje** — com o botão "Cadastrar no Hamilton" que já
existia pro cadastro que falha. Quem clica é gente.

**Por que não cadastrar sozinho.** Foi o desenho inicial, e ele está errado por um
motivo que o código não consegue cobrir: dá pra saber que a pessoa *passou* os
dados, mas não dá pra saber que ela *quis* ser cadastrada. Quem disse nome e
nascimento e depois escreveu "vou pensar melhor" é indistinguível, aqui dentro, de
quem só perdeu o wi-fi. As guardas parecem cobrir isso e não cobrem: o `estado` da
conversa **nunca** é escrito como `qualificando`/`coletando_dados` (só
`cadastrado`, `cadastro_pendente` e `escalado` são), então filtrar por estado não
prova que a coleta chegou ao fim. Um humano lê o histórico em cinco segundos e
sabe. O ganho — ninguém se perde — é preservado inteiro; o risco de escrever no
prontuário sem ninguém olhar sai de cena.

**Por que extração por LLM e não uma tool de anotar incremental.** Uma tool nova
dependeria de o modelo *lembrar* de chamá-la — e é exatamente esse o defeito que
a gente passou o dia medindo (ele diz "vou registrar" e não registra). Consertar
esquecimento com mais uma coisa pra esquecer é andar em círculo. A extração roda
no cron, relendo a transcrição: não depende do modelo em runtime, e quando não
acha o essencial simplesmente não prepara nada.

**Não cria cron novo.** Roda pendurado no `POST /tasks/seguimentos`, que já
existe e já está configurado. Cron que alguém precisa lembrar de criar é feature
que nunca roda — este repositório tem um caso assim custando dinheiro até hoje.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Escalada, Mensagem
from app.services import conversation, llm_client
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# Silêncio a partir do qual a gente desiste de esperar a confirmação. 24h é
# depois de o follow-up (20h) já ter tido a chance dele de trazer a pessoa de
# volta: o resgate é o último recurso, não atropela a tentativa.
HORAS_DE_SILENCIO = 24

# Marcador de "já tentei extrair" — evita gastar uma chamada ao modelo por
# conversa em toda rodada, pra sempre. Mora no `dados_coletados`, e não numa
# coluna nova, pelo mesmo motivo que `pesquisa_respostas_gravadas`.
CHAVE_TENTATIVA = "cadastro_auto_tentado_em"

# Marcador de "tem ficha pronta esperando gente". É por ele que a tela Hoje acha
# essas conversas. Separado do de cima de propósito: tentativa que não achou nada
# não pode virar linha na fila.
CHAVE_PRONTO = "cadastro_auto_pronto_em"

# O que a coordenação precisa saber ao abrir essa ficha. Sem isto, um dado
# extraído de conversa fica indistinguível de um dado confirmado pela pessoa.
NOTA_OBSERVACAO = (
    "Dados extraídos da conversa: a pessoa passou as informações mas sumiu antes "
    "de confirmar. Confira nome e nascimento antes de cadastrar."
)

# Instrução de extração. Fica no código, e não em /painel/prompts como as outras:
# isto não é voz nem tom, é um contrato com o schema da tool `cadastrar_paciente`.
# Editar no painel quebraria o cadastro sem ninguém perceber.
INSTRUCAO_EXTRACAO = """Você lê a transcrição de um atendimento por WhatsApp e extrai os dados de cadastro.

Devolva SÓ um JSON, sem texto em volta, com as chaves que você encontrar:
- nome_completo: o nome que a PESSOA informou sobre quem vai ser atendido
- data_nascimento: no formato AAAA-MM-DD
- horarios_disponiveis: com as palavras dela
- motivo_busca: o que ela disse que a trouxe
- como_conheceu: como ela chegou até a Allos, se ela disse
- observacoes: qualquer pedido ou preferência relevante (ex.: quer atendimento
  presencial, quer terapeuta mulher, quer alguém com experiência em luto)

Regras:
- NÃO invente. Chave que você não encontrou na conversa, você omite.
- Se quem escreve é outra pessoa (mãe cadastrando filho), os dados são do PACIENTE.
- Se você não achar nome completo E data de nascimento, devolva {}."""

# Só estes campos são aceitos de volta. Allowlist, igual à da extração da
# pesquisa: o modelo pode inventar chave, e chave inventada não chega ao Hamilton.
CAMPOS_ACEITOS = {
    "nome_completo",
    "data_nascimento",
    "horarios_disponiveis",
    "motivo_busca",
    "como_conheceu",
    "observacoes",
}

ESTADOS_FINALIZADOS = ("cadastrado", "cadastro_pendente", "escalado")


def _sem_ficha(q):
    """Filtro comum: conversa que ainda não virou paciente e não saiu de cena."""
    return q.where(
        Conversa.paciente_hamilton_id.is_(None),
        Conversa.estado.not_in(ESTADOS_FINALIZADOS),
        Conversa.arquivada_em.is_(None),
    )


async def buscar_abandonadas(db: AsyncSession, agora: datetime) -> list[Conversa]:
    """Conversas que passaram dados e sumiram antes de confirmar.

    Critério: ainda no bot, sem cadastro no Hamilton, sem escalada aberta, não
    arquivada, sem tentativa anterior, e silenciosa há 24h ou mais.
    """
    limite = agora - timedelta(hours=HORAS_DE_SILENCIO)

    ultima_recebida = (
        select(
            Mensagem.conversa_id.label("conversa_id"),
            func.max(Mensagem.criada_em).label("ult"),
        )
        .where(Mensagem.direcao == "recebida")
        .group_by(Mensagem.conversa_id)
        .subquery()
    )
    escalada_aberta = select(Escalada.conversa_id).where(Escalada.resolvida_em.is_(None)).subquery()
    q = _sem_ficha(select(Conversa)).join(
        ultima_recebida, ultima_recebida.c.conversa_id == Conversa.id
    )
    q = q.where(
        Conversa.modo == "bot",
        Conversa.id.not_in(select(escalada_aberta.c.conversa_id)),
        ultima_recebida.c.ult <= limite,
    )
    candidatas = list((await db.execute(q)).scalars().all())
    # O marcador vive no JSON, então filtra em Python (portável SQLite/Postgres).
    return [c for c in candidatas if not (c.dados_coletados or {}).get(CHAVE_TENTATIVA)]


async def aguardando_confirmacao(db: AsyncSession) -> list[Conversa]:
    """Fichas prontas esperando alguém clicar em "Cadastrar no Hamilton".

    Usada pela tela Hoje. Sai da lista sozinha quando o cadastro acontece (aí a
    conversa ganha `paciente_hamilton_id`) — não há estado a limpar.
    """
    candidatas = (await db.execute(_sem_ficha(select(Conversa)))).scalars().all()
    return [c for c in candidatas if (c.dados_coletados or {}).get(CHAVE_PRONTO)]


def _normalizar(bruto: str | None) -> dict:
    """JSON do modelo -> dados de cadastro, com allowlist.

    Devolve `{}` sempre que faltar o essencial: sem nome completo e nascimento o
    Hamilton recusaria de qualquer jeito, e meia ficha na fila faz alguém abrir,
    perder tempo e fechar.
    """
    if not bruto:
        return {}
    texto = bruto.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        logger.warning("Extração de cadastro devolveu JSON inválido.")
        return {}
    if not isinstance(dados, dict):
        return {}
    limpo = {
        k: v for k, v in dados.items() if k in CAMPOS_ACEITOS and isinstance(v, str) and v.strip()
    }
    if not limpo.get("nome_completo") or not limpo.get("data_nascimento"):
        return {}
    return limpo


async def extrair(db: AsyncSession, conversa: Conversa) -> dict:
    """Relê a conversa e devolve os dados de cadastro que ela contém."""
    historico = await conversation.carregar_historico(db, conversa)
    transcricao = "\n".join(
        f"{'Pessoa' if m.get('role') == 'user' else 'Sofia'}: {m.get('content')}"
        for m in historico
        if m.get("role") in ("user", "assistant") and m.get("content")
    )
    if not transcricao.strip():
        return {}
    try:
        resposta = await llm_client.get_llm_client().gerar_resposta(
            [{"role": "user", "content": transcricao}],
            system_prompt=INSTRUCAO_EXTRACAO,
        )
    except llm_client.LLMError:
        logger.warning("LLM falhou ao extrair cadastro da conversa %s", conversa.id)
        return {}
    return _normalizar(resposta.texto)


async def preparar(db: AsyncSession, conversa: Conversa, agora: datetime) -> bool:
    """Extrai e deixa a ficha pronta pra confirmação. **Não cadastra.**

    Devolve True se sobrou ficha pra alguém revisar.
    """
    dados = await extrair(db, conversa)
    # Marca a tentativa mesmo quando não dá em nada: sem isto o cron gastaria uma
    # chamada ao modelo por conversa abandonada, em toda rodada, pra sempre.
    marcado = {**(conversa.dados_coletados or {}), CHAVE_TENTATIVA: agora.isoformat()}
    if not dados:
        conversa.dados_coletados = marcado
        logger.info("Resgate de cadastro: nada extraível na conversa %s", conversa.id)
        return False

    observacoes = " | ".join(filter(None, [dados.get("observacoes"), NOTA_OBSERVACAO]))
    conversa.dados_coletados = {
        **marcado,
        **dados,
        "observacoes": observacoes,
        CHAVE_PRONTO: agora.isoformat(),
    }
    logger.info(
        "Resgate de cadastro: ficha pronta pra confirmação na conversa %s (%s)",
        conversa.id,
        mascarar_telefone(conversa.numero_whatsapp),
    )
    return True


async def rodar_resgates(db: AsyncSession, agora: datetime | None = None) -> dict:
    """Uma rodada. Chamada junto do follow-up, no mesmo cron."""
    agora = agora or datetime.now(timezone.utc)
    abandonadas = await buscar_abandonadas(db, agora)
    prontas = 0
    for conversa in abandonadas:
        try:
            if await preparar(db, conversa, agora):
                prontas += 1
        except Exception:  # noqa: BLE001 - uma conversa ruim não para a rodada
            logger.exception("Falha no resgate de cadastro da conversa %s", conversa.id)
    await db.commit()
    if abandonadas:
        logger.info("Resgate de cadastro: %s/%s fichas prontas", prontas, len(abandonadas))
    return {"avaliadas": len(abandonadas), "prontas": prontas}
