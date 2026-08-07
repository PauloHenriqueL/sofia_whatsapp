"""Pesquisa de satisfação conduzida pela Sofia no WhatsApp.

Substitui o trabalho que a equipe de Qualidade fazia à mão: abordar o paciente,
fazer as perguntas uma a uma e anotar as respostas fora do sistema.

## Como a Sofia descobre que tem pesquisa pra fazer

Ela **pergunta**, não é avisada. O Hamilton já cria a `Avaliacao` com
`status='pendente'` por gatilho interno quando o terapeuta lança a primeira
consulta ou registra uma alta/desistência — ou seja, **a fila já existe lá**.
A Sofia lê essa fila no mesmo cron que já roda os follow-ups.

A alternativa (o Hamilton chamar a Sofia no momento do salvamento) foi
descartada: o Hamilton é síncrono, sem fila nem worker, então a chamada rodaria
dentro do request do terapeuta salvando o prontuário — que ficaria esperando a
Sofia acordar. Puxar em vez de empurrar também dá retry de graça: o pendente
continua lá até ser respondido.

## Como a pesquisa acontece

A conversa entra em "modo pesquisa" (`conversa.pesquisa_avaliacao_id`) e o turno
do bot passa a rodar com o prompt da pesquisa em vez do prompt de acolhimento —
a pessoa já é paciente, não é mais um lead a ser qualificado e cadastrado.

O modelo conduz e o modelo extrai: no fim, uma chamada separada transforma a
transcrição no JSON das respostas, que vai pro Hamilton por PATCH. Isso é uma
decisão consciente, com um custo conhecido: nada garante que todas as perguntas
sejam feitas nem que cada resposta seja lida corretamente, e ninguém confere
depois. Se aparecer dado incompleto ou impreciso na `Avaliacao`, é aqui que se
mexe (o conserto é registrar cada resposta por tool, em vez de extrair no fim).

## Prazos

Lembrete único em 20h de silêncio, encerramento em 44h. Os dois ficam colados na
janela de 24h da Meta: passada ela, texto livre não sai mais (só template), então
o lembrete tem que ser antes — e o encerramento é só marcação interna, não
precisa de mensagem.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversa, Escalada
from app.services import (
    config_prompt,
    conversation,
    hamilton_client,
    llm_client,
    tools,
    whatsapp_client,
)
from app.utils import mascarar_telefone

logger = logging.getLogger(__name__)

# Silêncio até insistir uma única vez, e até desistir. Ver a nota sobre a janela
# de 24h da Meta no topo do módulo.
HORAS_LEMBRETE = 20
HORAS_ENCERRAMENTO = 44

# Os quatro questionários. O `momento` da `Avaliacao` é o seletor: ele decide
# qual roteiro a Sofia aplica. Estes literais são os choices do Hamilton — mudar
# um caractere aqui manda a pessoa pro roteiro errado.
MOMENTO_LINHA_DE_BASE = "Antes da primeira sessão (linha de base)"
MOMENTO_PRIMEIRA_SESSAO = "No início do processo (primeira sessão)"
MOMENTO_ACOMPANHAMENTO = "Durante o acompanhamento terapêutico"
MOMENTO_ENCERRAMENTO = "Após o encerramento da terapia"

# Pesquisa de entrada (linha de base): as 3h evitam emendar na conversa de
# acolhimento, que é onde mora a receita. Os 5 dias são o ponto em que baseline
# velho deixa de ser baseline — depois disso a pessoa já pode ter tido a sessão,
# e um ORS colhido aí não é comparável com um colhido antes.
HORAS_ESPERA_ENTRADA = 3
DIAS_LIMITE_ENTRADA = 5

# Onde ficam registrados os campos que a TOOL já gravou nesta pesquisa. Serve
# pra extração do fim não sobrescrever o que veio da tool: a tool é a fonte
# preferida (leu a resposta na hora, com o contexto do turno), a extração é só
# rede pro que ficou em branco.
_CHAVE_GRAVADOS = "pesquisa_respostas_gravadas"

# Texto do lembrete. Fixo e sem LLM: é um empurrãozinho, não um turno de
# conversa, e insistir com criatividade seria pressão.
LEMBRETE_TEXTO = (
    "Oi, passando pra lembrar da pesquisa que te mandei. "
    "Se puder responder, ajuda demais a gente a melhorar. "
    "Se preferir deixar pra lá, tudo bem também."
)


class PesquisaError(Exception):
    """Falha ao conduzir ou registrar a pesquisa."""


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _aware(valor: datetime | None) -> datetime | None:
    """Datas vindas do SQLite voltam sem tzinfo; compara-las com aware estoura."""
    if valor is not None and valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


def em_pesquisa(conversa: Conversa) -> bool:
    return conversa.pesquisa_avaliacao_id is not None


# --------------------------------------------------------------------------- #
# Montagem do prompt
# --------------------------------------------------------------------------- #


def _contexto_encerramento(avaliacao: dict) -> str:
    """Instrução de tom pra pesquisa de encerramento.

    O texto muda conforme o tipo de saída e quem a iniciou. Não é preciosismo:
    perguntar "por que você decidiu interromper?" a quem foi desligado pelo
    terapeuta é factualmente errado, e tratar um reencaminhamento como saída é
    pior ainda — a pessoa continua na Allos, só vai trocar de terapeuta.
    """
    tipo = (avaliacao.get("tipo_saida") or "").strip().lower()
    cancelador = (avaliacao.get("cancelador") or "").strip().lower()
    partes: list[str] = []

    if "reencaminhamento" in tipo:
        partes.append(
            "ESTE CASO É UM REENCAMINHAMENTO: a pessoa NÃO está saindo da Allos, ela vai "
            "trocar de terapeuta e continua sendo atendida. Deixe isso claro logo no começo, "
            "com naturalidade. NÃO pergunte por que ela decidiu interromper (ela não "
            "interrompeu nada). Pergunte como foi a experiência com o terapeuta anterior. "
            "Diga que a Thainá, coordenadora clínica, vai entrar em contato pra combinar os "
            "detalhes e tirar dúvidas, sem prometer prazo."
        )
    elif tipo == "alta":
        partes.append(
            "ESTE CASO É UMA ALTA: o processo terminou bem, foi uma conclusão, não um "
            "abandono. Reconheça isso com leveza. NÃO pergunte por que ela decidiu "
            "interromper — pergunte como foi a experiência ao longo do processo."
        )
    elif "não responde" in tipo or "nao responde" in tipo:
        partes.append(
            "O terapeuta registrou que a pessoa parou de responder. Aborde sem cobrança e "
            "sem culpa: só diga que sentiu falta dela por aqui e que gostaria de saber como "
            "foi a experiência. Se ela não responder, tudo bem."
        )
    else:
        partes.append(
            "Pergunte, com cuidado e sem cobrança, o motivo pelo qual a pessoa decidiu "
            "interromper o processo."
        )

    if cancelador == "terapeuta":
        partes.append(
            "ATENÇÃO: quem encerrou foi o terapeuta, não a pessoa. NUNCA pergunte por que "
            "ELA decidiu interromper — seria factualmente errado e soaria como cobrança."
        )
    return "\n".join(partes)


# momento -> (chave do roteiro em config_prompt, contexto fixo).
# O contexto do encerramento é o único calculado, porque depende do tipo de saída
# e de quem a encerrou.
_ROTEIROS = {
    MOMENTO_LINHA_DE_BASE: (
        "prompt_pesquisa_entrada",
        "A pessoa acabou de ser cadastrada e AINDA NÃO teve a primeira sessão. "
        "Este é um retrato de como ela está hoje, antes de começar — é o que "
        "permite comparar lá na frente e saber se o processo ajudou.",
    ),
    MOMENTO_PRIMEIRA_SESSAO: (
        "prompt_pesquisa_primeira_sessao",
        "A pessoa acabou de ter a PRIMEIRA sessão com o terapeuta. Esta pesquisa "
        "é sobre como foi esse começo.",
    ),
    MOMENTO_ACOMPANHAMENTO: (
        "prompt_pesquisa_reencaminhamento",
        "A pessoa vai TROCAR de terapeuta e CONTINUA sendo atendida na Allos. "
        "Isto não é uma saída. Deixe isso claro logo no começo, com naturalidade, "
        "e nunca pergunte por que ela decidiu interromper — ela não interrompeu "
        "nada. As perguntas sobre o atendimento são sobre o terapeuta ANTERIOR.",
    ),
}


def _quem_responde(conversa) -> str | None:
    """O que já se sabe, do acolhimento, sobre quem está do outro lado.

    O número cadastrado é muitas vezes o do responsável ou do cônjuge. Sem este
    cuidado a Sofia pergunta "o quanto você se sente bem com quem você é" pra mãe
    do paciente e grava como nota do filho.
    """
    dados = getattr(conversa, "dados_coletados", None) or {}
    valor = dados.get("quem_fala")
    return valor if valor in ("paciente", "acompanhante") else None


def montar_prompt(avaliacao: dict, conversa=None) -> str:
    """System prompt do turno de pesquisa (substitui o prompt de acolhimento)."""
    base = config_prompt.texto("prompt_pesquisa")
    momento = avaliacao.get("momento") or ""
    nome = (avaliacao.get("paciente_nome") or "").split(" ")[0]
    terapeuta = avaliacao.get("terapeuta_nome") or "o terapeuta"

    if momento == MOMENTO_ENCERRAMENTO:
        chave, contexto = "prompt_pesquisa_encerramento", _contexto_encerramento(avaliacao)
    else:
        # Momento desconhecido cai na 1ª sessão, que é o questionário mais curto
        # e mais neutro — o pior que acontece é perguntar de menos.
        chave, contexto = _ROTEIROS.get(momento, _ROTEIROS[MOMENTO_PRIMEIRA_SESSAO])

    partes = [
        base,
        "## Contexto deste atendimento\n\n"
        f"Primeiro nome da pessoa: {nome or '(desconhecido)'}\n"
        f"Terapeuta dela: {terapeuta}",
        contexto,
    ]

    quem = _quem_responde(conversa)
    if quem == "acompanhante":
        partes.append(
            "QUEM RESPONDE NÃO É O PACIENTE: este número é de um acompanhante "
            "(responsável, cônjuge). **PULE inteiro o bloco de perguntas de 0 a 10 "
            "sobre como a PESSOA ATENDIDA está se sentindo** — quem responde não "
            "tem como responder por ela. Faça só as perguntas sobre o atendimento "
            "e o serviço. Não explique essa regra, apenas não faça as perguntas."
        )
    elif quem == "paciente":
        partes.append("Quem responde é a própria pessoa atendida. Siga o roteiro inteiro.")
    else:
        partes.append(
            "NÃO se sabe se quem responde é a própria pessoa atendida ou um "
            "acompanhante. Antes das perguntas de 0 a 10, confirme isso em UMA "
            "frase curta e natural. Se for acompanhante, pule o bloco de perguntas "
            "de 0 a 10 sobre como a pessoa atendida se sente."
        )

    partes.append(f"## Roteiro de perguntas\n\n{config_prompt.texto(chave)}")
    return "\n\n".join(partes)


# --------------------------------------------------------------------------- #
# Ciclo de vida da pesquisa
# --------------------------------------------------------------------------- #


async def iniciar(
    db: AsyncSession, conversa: Conversa, avaliacao: dict, agora: datetime | None = None
) -> bool:
    """Manda o convite da pesquisa e coloca a conversa em modo pesquisa.

    Só marca a conversa se o convite realmente saiu: falha de envio deixa a
    avaliação pendente e ela volta na próxima rodada.
    """
    agora = agora or _agora()
    pk = avaliacao.get("pk_avaliacao")
    if not pk:
        return False

    try:
        resposta = await _turno(conversa, avaliacao, historico=[], abertura=True, db=db)
    except llm_client.LLMError as exc:
        logger.error("Não consegui gerar o convite da pesquisa %s: %s", pk, exc)
        return False

    if not await _enviar(db, conversa, resposta):
        return False

    conversa.pesquisa_avaliacao_id = pk
    conversa.pesquisa_iniciada_em = agora
    await db.flush()

    # Marca no Hamilton que já abordamos: 'pendente' quer dizer "sem resposta",
    # não "sem envio" — sem isso a pessoa seria abordada de novo a cada rodada.
    try:
        await hamilton_client.get_hamilton_client().atualizar_avaliacao(
            pk, {"sofia_enviada_em": agora.isoformat()}
        )
    except hamilton_client.HamiltonError as exc:
        logger.error("Pesquisa %s enviada mas não marcada no Hamilton: %s", pk, exc)
    await db.commit()
    return True


async def responder(db: AsyncSession, conversa: Conversa, numero: str) -> None:
    """Conduz um turno da pesquisa (chamado no lugar do turno normal do bot)."""
    avaliacao = await _buscar_avaliacao(conversa)
    if avaliacao is None:
        # Sem contexto não dá pra conduzir; encerra o modo pesquisa e deixa a
        # conversa voltar ao fluxo normal em vez de travar o paciente.
        logger.warning(
            "Pesquisa %s sumiu do Hamilton; saindo do modo pesquisa", conversa.pesquisa_avaliacao_id
        )
        await _limpar(db, conversa)
        return

    historico = await conversation.carregar_historico(db, conversa)
    try:
        resposta = await _turno(conversa, avaliacao, historico=historico, db=db)
    except llm_client.LLMError:
        logger.error("LLM falhou no turno da pesquisa da conversa %s", conversa.id)
        return

    concluida = _marcador_de_fim(resposta)
    await _enviar(db, conversa, _sem_marcador(resposta))
    if concluida:
        await finalizar(db, conversa, avaliacao, recusou=concluida == "recusou")


async def finalizar(
    db: AsyncSession, conversa: Conversa, avaliacao: dict, recusou: bool = False
) -> None:
    """Extrai as respostas da conversa, grava no Hamilton e sai do modo pesquisa.

    Recusa e silêncio caem os dois em `nao_respondeu`: pro time de Qualidade a
    diferença não muda o que fazer, e o Hamilton não tem status separado.
    """
    pk = conversa.pesquisa_avaliacao_id
    # Resposta parcial vira `avaliado`: quem respondeu 2 de 8 perguntas e sumiu
    # não é "não respondeu" — marcar assim apagaria essas 2 do radar. O rigor
    # de quem precisa dele (o relatório de ORS) vem do filtro "os quatro itens
    # presentes", não do status.
    respondeu_algo = bool(_ja_gravados(conversa))
    payload: dict = {"status": "nao_respondeu" if (recusou and not respondeu_algo) else "avaliado"}

    # Quem respondeu alguma coisa e depois sumiu ainda tem texto a extrair.
    # Quem recusou de cara não: a transcrição são duas mensagens e a chamada
    # ao modelo seria desperdício.
    if respondeu_algo or not recusou:
        historico = await conversation.carregar_historico(db, conversa, limite=60)
        try:
            extraido = await extrair_respostas(historico, avaliacao)
            # A TOOL TEM PRECEDÊNCIA. Ela leu a resposta no turno em que ela foi
            # dada; a extração relê a transcrição inteira de fora e é onde mora o
            # risco de trocar uma nota de lugar. Aqui ela só preenche buraco.
            gravados = _ja_gravados(conversa)
            descartados = sorted(set(extraido) & gravados)
            if descartados:
                logger.info(
                    "Pesquisa %s: extração ignorada em %s (a tool já gravou)",
                    pk,
                    ", ".join(descartados),
                )
            payload.update({k: v for k, v in extraido.items() if k not in gravados})
        except (llm_client.LLMError, PesquisaError) as exc:
            # Sem extração, ainda assim marcamos como avaliado: a conversa está
            # no painel e a pessoa respondeu de verdade. Perder o status seria
            # pior — a pesquisa seria reenviada pra quem já respondeu.
            logger.error("Não consegui extrair as respostas da pesquisa %s: %s", pk, exc)

    try:
        await hamilton_client.get_hamilton_client().atualizar_avaliacao(pk, payload)
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui gravar a pesquisa %s no Hamilton: %s", pk, exc)

    await _limpar(db, conversa)


async def extrair_respostas(historico: list[dict], avaliacao: dict) -> dict:
    """Transforma a conversa da pesquisa no payload de respostas do Hamilton.

    Só devolve campos que a `Avaliacao` conhece e cujos valores são plausíveis:
    o modelo pode devolver campo inventado, nota fora da escala ou texto onde
    era pra ser número, e nada disso pode chegar ao banco.
    """
    transcricao = "\n".join(
        f"{'Paciente' if m.get('role') == 'user' else 'Sofia'}: {m.get('content')}"
        for m in historico
        if m.get("role") in ("user", "assistant") and m.get("content")
    )
    if not transcricao.strip():
        raise PesquisaError("Sem transcrição pra extrair")

    instrucao = config_prompt.texto("prompt_pesquisa_extracao")
    resposta = await llm_client.get_llm_client().gerar_resposta(
        [{"role": "user", "content": transcricao}], system_prompt=instrucao
    )
    return _normalizar_extracao(resposta.texto, avaliacao)


# Campos graváveis na Avaliacao, por tipo. Allowlist: o que o modelo devolver
# fora daqui é descartado em silêncio, sem chance de virar erro 400 no Hamilton.
#
# Os numéricos/booleanos são os MESMOS da tool (`tools.CAMPOS_PESQUISA`), e não
# uma segunda lista: duas listas divergiriam na primeira mudança de roteiro. Aqui
# eles são rede — quem grava primeiro é a tool, e a extração só preenche o que
# ficou em branco (ver `finalizar`).
_NOTAS = tools.CAMPOS_PESQUISA_NOTA
_BOOLEANOS = tools.CAMPOS_PESQUISA_BOOLEANO
# Texto continua saindo só da extração: errar texto custa pouco, e forçar uma
# tool pra texto longo atrapalha a conversa.
_TEXTOS = ("feedback_livre", "motivo_encerramento")


def _normalizar_extracao(bruto: str | None, avaliacao: dict) -> dict:
    """Valida e converte o JSON devolvido pelo modelo."""
    if not bruto:
        raise PesquisaError("Extração vazia")
    texto = bruto.strip()
    # O modelo às vezes embrulha o JSON num bloco de código.
    if texto.startswith("```"):
        texto = texto.strip("`")
        texto = texto.split("\n", 1)[-1] if texto.lower().startswith("json") else texto
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fim == -1:
        raise PesquisaError("Extração não trouxe JSON")
    try:
        dados = json.loads(texto[inicio : fim + 1])
    except json.JSONDecodeError as exc:
        raise PesquisaError(f"JSON inválido na extração: {exc}") from exc
    if not isinstance(dados, dict):
        raise PesquisaError("Extração não é um objeto")

    limpo: dict = {}
    for campo in _NOTAS:
        valor = dados.get(campo)
        if valor is None:
            continue
        try:
            nota = int(valor)
        except (TypeError, ValueError):
            continue
        if 0 <= nota <= 10:  # fora da escala é dado ruim, não dado parcial
            limpo[campo] = nota
    for campo in _TEXTOS:
        valor = dados.get(campo)
        if isinstance(valor, str) and valor.strip():
            limpo[campo] = valor.strip()
    for campo in _BOOLEANOS:
        valor = dados.get(campo)
        if isinstance(valor, bool):
            limpo[campo] = valor

    # `motivo_encerramento` existe na saída E na troca de terapeuta (é um campo
    # só: o `momento` já diz qual dos dois foi). Nos outros dois questionários
    # não há motivo nenhum a registrar.
    if avaliacao.get("momento") not in (MOMENTO_ENCERRAMENTO, MOMENTO_ACOMPANHAMENTO):
        limpo.pop("motivo_encerramento", None)
    return limpo


# --------------------------------------------------------------------------- #
# Rodada do cron
# --------------------------------------------------------------------------- #


async def rodar_pesquisas(db: AsyncSession, agora: datetime | None = None) -> dict:
    """Uma rodada: envia convites novos, manda lembretes e encerra por prazo.

    Devolve um resumo (enviadas/lembretes/encerradas) pra quem chamou logar.
    Falha do Hamilton não derruba nada: a fila continua lá na próxima rodada.
    """
    agora = agora or _agora()
    resumo = {"enviadas": 0, "lembretes": 0, "encerradas": 0, "entradas_criadas": 0}

    # A avaliação de linha de base não existe até a Sofia criar. Isso vem ANTES
    # de ler a fila: criada agora, ela entra na fila do próximo tick — nunca no
    # mesmo, então a janela de 3h nunca é encurtada por acidente.
    resumo["entradas_criadas"] = await _criar_entradas(db, agora)

    try:
        pendentes = await hamilton_client.get_hamilton_client().avaliacoes_pendentes()
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui buscar as avaliações pendentes: %s", exc)
        pendentes = []

    for avaliacao in pendentes:
        conversa = await _conversa_do_paciente(db, avaliacao)
        if conversa is None:
            # Paciente que não veio pela Sofia não tem conversa aberta, e fora da
            # janela de 24h da Meta não dá pra iniciar uma com texto livre.
            # Fica pra equipe abordar (exigiria um template aprovado).
            continue
        if em_pesquisa(conversa) or conversa.modo == "humano":
            continue  # já tem pesquisa em curso, ou a conversa está com a Thainá
        if await iniciar(db, conversa, avaliacao, agora):
            resumo["enviadas"] += 1

    resumo.update(await _acompanhar_em_curso(db, agora))
    logger.info(
        "Pesquisas: %s entradas criadas, %s enviadas, %s lembretes, %s encerradas",
        resumo["entradas_criadas"],
        resumo["enviadas"],
        resumo["lembretes"],
        resumo["encerradas"],
    )
    return resumo


async def _criar_entradas(db: AsyncSession, agora: datetime) -> int:
    """Cria no Hamilton a avaliação de linha de base de quem acabou de se cadastrar.

    É o único ponto de medida ANTES do tratamento: sem ele não existe par
    pré/pós, e o ORS sozinho não significa nada — o dado só vale como
    `ORS saída − ORS entrada`.

    As guardas são para não emendar na conversa de acolhimento nem atropelar
    outro assunto em curso. Se uma delas bloquear, tenta de novo nos próximos
    ticks até os 5 dias.

    Conversas cadastradas antes desta feature existir têm `cadastrado_em` NULL e
    ficam de fora para sempre — é o que impede a estreia disto de virar um
    disparo em massa pra base inteira.
    """
    janela_inicio = agora - timedelta(days=DIAS_LIMITE_ENTRADA)
    janela_fim = agora - timedelta(hours=HORAS_ESPERA_ENTRADA)

    # Escaladas abertas: assunto não resolvido com a Thainá, a pesquisa espera.
    escaladas_abertas = (
        select(Escalada.conversa_id).where(Escalada.resolvida_em.is_(None)).scalar_subquery()
    )
    q = select(Conversa).where(
        Conversa.cadastrado_em.isnot(None),
        Conversa.cadastrado_em <= janela_fim,
        Conversa.cadastrado_em >= janela_inicio,
        Conversa.paciente_hamilton_id.isnot(None),
        # `cadastro_pendente` significa que o Hamilton falhou: não há paciente
        # de verdade pra vincular a avaliação.
        Conversa.estado == "cadastrado",
        Conversa.modo == "bot",
        Conversa.pesquisa_avaliacao_id.is_(None),
        Conversa.arquivada_em.is_(None),
        Conversa.id.not_in(escaladas_abertas),
    )

    cliente = hamilton_client.get_hamilton_client()
    criadas = 0
    for conversa in (await db.execute(q)).scalars().all():
        try:
            # Idempotente do lado do Hamilton: repetir devolve a que já existe.
            # Por isso não é preciso marcar nada aqui.
            await cliente.criar_avaliacao_entrada(conversa.paciente_hamilton_id)
            criadas += 1
        except hamilton_client.HamiltonError as exc:
            logger.error(
                "Não consegui criar a avaliação de entrada da conversa %s: %s", conversa.id, exc
            )
    return criadas


async def _acompanhar_em_curso(db: AsyncSession, agora: datetime) -> dict:
    """Lembra quem está em silêncio há 20h e encerra quem passou de 44h."""
    resumo = {"lembretes": 0, "encerradas": 0}
    conversas = (
        (await db.execute(select(Conversa).where(Conversa.pesquisa_avaliacao_id.isnot(None))))
        .scalars()
        .all()
    )
    for conversa in conversas:
        silencio = agora - (_aware(conversa.pesquisa_iniciada_em) or agora)
        if silencio >= timedelta(hours=HORAS_ENCERRAMENTO):
            avaliacao = await _buscar_avaliacao(conversa) or {}
            await finalizar(db, conversa, avaliacao, recusou=True)
            resumo["encerradas"] += 1
        elif silencio >= timedelta(hours=HORAS_LEMBRETE):
            if await _mandar_lembrete(db, conversa, agora):
                resumo["lembretes"] += 1
    return resumo


async def _mandar_lembrete(db: AsyncSession, conversa: Conversa, agora: datetime) -> bool:
    """Manda o lembrete único, se ainda não foi mandado."""
    pk = conversa.pesquisa_avaliacao_id
    cliente = hamilton_client.get_hamilton_client()
    try:
        pendentes = await cliente.avaliacoes_pendentes(incluir_enviadas=True)
    except hamilton_client.HamiltonError:
        return False
    atual = next((a for a in pendentes if a.get("pk_avaliacao") == pk), None)
    if atual is None or atual.get("sofia_lembrete_em"):
        return False  # já lembramos; insistir de novo vira pressão

    if not await _enviar(db, conversa, LEMBRETE_TEXTO):
        return False
    try:
        await cliente.atualizar_avaliacao(pk, {"sofia_lembrete_em": agora.isoformat()})
    except hamilton_client.HamiltonError as exc:
        logger.error("Lembrete da pesquisa %s enviado mas não marcado: %s", pk, exc)
    await db.commit()
    return True


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #

# O modelo sinaliza o fim da pesquisa com um marcador no final da fala. É a única
# forma de saber que acabou sem uma máquina de estado — e ele nunca chega ao
# paciente (é removido antes do envio, e a sanitização de saída é a última rede).
MARCADOR_FIM = "[[PESQUISA_CONCLUIDA]]"
MARCADOR_RECUSA = "[[PESQUISA_RECUSADA]]"


def _marcador_de_fim(texto: str | None) -> str | None:
    if not texto:
        return None
    if MARCADOR_RECUSA in texto:
        return "recusou"
    if MARCADOR_FIM in texto:
        return "concluiu"
    return None


def _sem_marcador(texto: str | None) -> str | None:
    if not texto:
        return texto
    return texto.replace(MARCADOR_FIM, "").replace(MARCADOR_RECUSA, "").strip()


async def _turno(
    conversa,
    avaliacao: dict,
    historico: list[dict],
    abertura: bool = False,
    db: AsyncSession | None = None,
) -> str:
    """Uma geração do modelo com o prompt da pesquisa, com tool calling.

    A tool `registrar_resposta_pesquisa` grava cada resposta **na hora**. Isso
    existe porque resposta parcial é o caso comum: a pessoa responde três
    perguntas e some. Extrair só no fim perderia tudo — e o ORS se invalida
    inteiro se faltar um dos quatro itens.
    """
    mensagens = list(historico)
    if abertura:
        mensagens.append(
            {
                "role": "system",
                "content": (
                    "[Aviso do sistema: comece a pesquisa agora. Você já conhece esta "
                    "pessoa e já falou com ela antes — NÃO se reapresente. Peça "
                    "consentimento antes de começar as perguntas.]"
                ),
            }
        )
    system_prompt = montar_prompt(avaliacao, conversa)
    cliente = llm_client.get_llm_client()
    resposta = await cliente.gerar_resposta(
        mensagens, tools=tools.TOOLS_PESQUISA, system_prompt=system_prompt
    )
    if not resposta.tool_calls:
        return resposta.texto or ""

    resultados = [(tc, await _registrar_resposta(db, conversa, tc)) for tc in resposta.tool_calls]

    # Round-trip: devolve os resultados ao modelo pra ele gerar a fala. O
    # `system_prompt` PRECISA ir de novo — sem ele o modelo cairia no prompt de
    # acolhimento no meio de uma pesquisa.
    assistant_msg = {
        "role": "assistant",
        "content": resposta.texto or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc, _ in resultados
        ],
    }
    tool_msgs = [
        {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(res, ensure_ascii=False)}
        for tc, res in resultados
    ]
    final = await cliente.gerar_resposta(
        [*mensagens, assistant_msg, *tool_msgs], system_prompt=system_prompt
    )
    # Se o round-trip não gerar fala, vale o texto que veio junto das tool calls.
    return final.texto or resposta.texto or ""


def _validar_resposta(campo, valor) -> tuple[str, int | bool] | None:
    """Confere o par (campo, valor) que o MODELO escolheu.

    O `campo` vem de um enum, mas o modelo erra enum; e `valor` chega como JSON,
    onde `True` é instância de `int` em Python — sem a checagem explícita, um
    `true` viraria a nota 1 no ORS.
    """
    if campo not in tools.CAMPOS_PESQUISA:
        return None

    if campo in tools.CAMPOS_PESQUISA_BOOLEANO:
        # Só booleano de verdade: "sim" e 1 não viram True. Ver a decisão sobre
        # extração no doc do modelo de avaliação.
        return (campo, valor) if isinstance(valor, bool) else None

    if isinstance(valor, bool):
        return None
    try:
        nota = int(valor)
    except (TypeError, ValueError):
        return None
    return (campo, nota) if 0 <= nota <= 10 else None


async def _registrar_resposta(db: AsyncSession | None, conversa, tc) -> dict:
    """Grava UMA resposta no Hamilton, na hora.

    PATCH imediato em vez de acumular na memória: acumular reintroduziria
    exatamente o ponto único de falha que a tool veio remover — se a Sofia cair
    (ou a pessoa sumir) no meio, o que ela já disse fica gravado.
    """
    validado = _validar_resposta(tc.arguments.get("campo"), tc.arguments.get("valor"))
    if validado is None:
        # Sem o valor no log: é resposta de pesquisa de saúde.
        logger.warning(
            "Pesquisa: tool recusada (campo=%r inválido ou valor fora do formato)",
            tc.arguments.get("campo"),
        )
        return {"status": "recusado", "motivo": "campo ou valor inválido"}

    campo, valor = validado
    pk = getattr(conversa, "pesquisa_avaliacao_id", None)
    if not pk:
        return {"status": "recusado", "motivo": "sem pesquisa em curso"}

    try:
        await hamilton_client.get_hamilton_client().atualizar_avaliacao(pk, {campo: valor})
    except hamilton_client.HamiltonError as exc:
        # Não marca como gravado: a extração do fim ainda tenta preencher.
        logger.error("Não consegui gravar %s da pesquisa %s: %s", campo, pk, exc)
        return {"status": "erro", "motivo": "não consegui gravar agora"}

    await _marcar_gravado(db, conversa, campo)
    return {"status": "registrado", "campo": campo}


async def _marcar_gravado(db: AsyncSession | None, conversa, campo: str) -> None:
    """Anota que a tool já gravou este campo (a extração do fim não o toca)."""
    dados = dict(getattr(conversa, "dados_coletados", None) or {})
    gravados = list(dados.get(_CHAVE_GRAVADOS) or [])
    if campo not in gravados:
        gravados.append(campo)
    dados[_CHAVE_GRAVADOS] = gravados
    conversa.dados_coletados = dados
    if db is not None:
        await db.flush()


def _ja_gravados(conversa) -> set[str]:
    dados = getattr(conversa, "dados_coletados", None) or {}
    return set(dados.get(_CHAVE_GRAVADOS) or [])


async def _enviar(db: AsyncSession, conversa: Conversa, texto: str | None) -> bool:
    """Envia a fala da pesquisa em bolhas, persistindo cada uma.

    Importado aqui dentro porque o router do webhook importa este módulo: no topo
    do arquivo daria import circular.
    """
    if not texto or not texto.strip():
        return False
    from app.routers import webhook

    try:
        await webhook._enviar_em_bolhas(db, conversa, conversa.numero_whatsapp, texto)
    except whatsapp_client.WhatsAppError:
        logger.error(
            "Não consegui falar com o número %s na pesquisa",
            mascarar_telefone(conversa.numero_whatsapp),
        )
        return False
    return True


async def _buscar_avaliacao(conversa: Conversa) -> dict | None:
    """Recupera do Hamilton a avaliação em curso nesta conversa."""
    if conversa.pesquisa_avaliacao_id is None:
        return None
    try:
        pendentes = await hamilton_client.get_hamilton_client().avaliacoes_pendentes(
            incluir_enviadas=True
        )
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui recuperar a avaliação em curso: %s", exc)
        return None
    return next(
        (a for a in pendentes if a.get("pk_avaliacao") == conversa.pesquisa_avaliacao_id), None
    )


async def _conversa_do_paciente(db: AsyncSession, avaliacao: dict) -> Conversa | None:
    """Acha a conversa da Sofia com aquele paciente.

    Primeiro pelo vínculo de cadastro (`paciente_hamilton_id`), que é o caminho
    confiável; depois pelo telefone, que cobre quem conversou com a Sofia mas foi
    cadastrado à mão no Hamilton. Compara só os dígitos, porque o Hamilton guarda
    sem DDI (31...) e o WhatsApp manda com (5531...).
    """
    pid = avaliacao.get("fk_paciente")
    if pid:
        conversa = (
            await db.execute(select(Conversa).where(Conversa.paciente_hamilton_id == pid))
        ).scalar_one_or_none()
        if conversa is not None:
            return conversa

    telefone = hamilton_client.normalizar_telefone(avaliacao.get("paciente_telefone"))
    if not telefone:
        return None
    for conversa in (await db.execute(select(Conversa))).scalars().all():
        if hamilton_client.normalizar_telefone(conversa.numero_whatsapp) == telefone:
            return conversa
    return None


async def _limpar(db: AsyncSession, conversa: Conversa) -> None:
    """Tira a conversa do modo pesquisa (ela volta ao fluxo normal)."""
    conversa.pesquisa_avaliacao_id = None
    conversa.pesquisa_iniciada_em = None
    # Zera o rastro de quais campos a tool gravou: a pessoa recebe até quatro
    # pesquisas ao longo do processo, e o registro da anterior não pode fazer a
    # extração da seguinte pular campo que ninguém gravou.
    dados = dict(conversa.dados_coletados or {})
    if dados.pop(_CHAVE_GRAVADOS, None) is not None:
        conversa.dados_coletados = dados
    await db.commit()
