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

O modelo conduz, mas **não é ele quem decide o que fica gravado**. Cada nota e
cada sim/não vai pro Hamilton na hora, por tool (`registrar_resposta_pesquisa`),
validada em `_validar_resposta`. A extração por LLM no fim continua existindo,
mas só pros textos e como rede pro que a tool não gravou — ela relê a
transcrição de fora e é onde mora o risco de trocar uma nota de lugar.

Isso foi uma reversão consciente: extrair tudo no fim era aceitável quando eram
11 perguntas soltas, mas o ORS é um bloco que **se invalida inteiro** se um dos
quatro itens sair errado, e é o número que vai pra prefeitura.

## O alerta é o que transforma isto em produto

Não há time de qualidade além da Sofia. Sem alerta, o desenho seria coletar e
arquivar: um `qualidade_geral = 2` entraria no banco e ninguém saberia. No fim
de cada pesquisa, `_alertar_se_precisar` manda **um** aviso consolidado à Thainá
e põe a conversa numa fila no painel. Os limiares são editáveis em
`/painel/config`. O ORS nunca alerta (ver `motivos_de_alerta`).

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
    cadastro,
    config_negocio,
    config_prompt,
    conversation,
    escalation,
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

# Pesquisa de entrada (linha de base): o caminho principal é a EMENDA — a Sofia
# convida logo depois de confirmar o cadastro, na mesma conversa (`iniciar_entrada`).
# O cron é só a REDE, pra quem foi cadastrado pelo painel ou pra quando o convite
# não saiu; por isso ele espera as 3h (não atropela a emenda que acabou de rodar)
# e desiste em 5 dias, que é quando baseline velho deixa de ser baseline.
#
# Antes a emenda não existia e TUDO dependia do cron: o convite só saía na
# SEGUNDA volta dele (uma criava a Avaliacao, a outra abordava), 3h depois do
# cadastro e com a trava do Hamilton ligada. Três condições invisíveis em série,
# e na prática a linha de base não acontecia.
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


def _primeiro_nome(avaliacao: dict, conversa) -> str:
    """Primeiro nome da pessoa, do Hamilton ou do que a Sofia coletou.

    O POST que cria a linha de base devolve um payload enxuto, sem o nome — e
    nessa hora quem tem o nome é a própria conversa, que acabou de cadastrar.
    """
    nome = avaliacao.get("paciente_nome") or ""
    if not nome:
        nome = ((getattr(conversa, "dados_coletados", None) or {}).get("nome_completo")) or ""
    return nome.split(" ")[0]


def montar_prompt(avaliacao: dict, conversa=None) -> str:
    """System prompt do turno de pesquisa (substitui o prompt de acolhimento)."""
    base = config_prompt.texto("prompt_pesquisa")
    momento = avaliacao.get("momento") or ""
    nome = _primeiro_nome(avaliacao, conversa)

    if momento == MOMENTO_ENCERRAMENTO:
        chave, contexto = "prompt_pesquisa_encerramento", _contexto_encerramento(avaliacao)
    else:
        # Momento desconhecido cai na 1ª sessão, que é o questionário mais curto
        # e mais neutro — o pior que acontece é perguntar de menos.
        chave, contexto = _ROTEIROS.get(momento, _ROTEIROS[MOMENTO_PRIMEIRA_SESSAO])

    contexto_atendimento = f"Primeiro nome da pessoa: {nome or '(desconhecido)'}"
    # Na linha de base NÃO existe terapeuta: a coordenação ainda não fez o match
    # e o Hamilton grava um sentinela pra não deixar a FK nula. Passar esse nome
    # pro modelo faria a Sofia citar como "o terapeuta dela" uma pessoa que não
    # atende ninguém.
    if momento != MOMENTO_LINHA_DE_BASE:
        contexto_atendimento += (
            f"\nTerapeuta dela: {avaliacao.get('terapeuta_nome') or 'o terapeuta'}"
        )

    partes = [
        base,
        f"## Contexto deste atendimento\n\n{contexto_atendimento}",
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
    elif momento == MOMENTO_LINHA_DE_BASE:
        # A linha de base É o bloco de 0 a 10. Sem ele não sobra pesquisa: quem
        # responde por outra pessoa não tem como dizer como ELA se sente, e um
        # palpite viraria número errado no relatório da prefeitura. Por isso aqui
        # o desconhecido não "pula o bloco" — ele encerra.
        partes.append(
            "NÃO se sabe se quem responde é a própria pessoa que vai ser atendida "
            "ou um acompanhante (responsável, cônjuge). Confirme isso em UMA frase "
            "curta e natural ANTES de começar as perguntas. Se for acompanhante, "
            "NÃO faça nenhuma das perguntas: agradeça em uma frase, diga que a "
            "Thainá segue com o contato normalmente e encerre com "
            f"{MARCADOR_RECUSA}. Não explique a regra."
        )
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


# Sinal de que a conversa é de neuroavaliação, e não de terapia. A linha de base
# é o ORS, que é instrumento de PROCESSO TERAPÊUTICO: quem vai fazer avaliação
# neuropsicológica não tem "antes e depois do tratamento" pra medir, e negociação,
# prazo e laudo dela correm com a Amanda, fora do fluxo da Sofia.
_MARCA_NEURO = "neuro"


async def _e_neuro(db: AsyncSession, conversa: Conversa) -> bool:
    """A conversa é de neuroavaliação?

    Dois sinais, porque nenhum sozinho basta. A escalada `neuro_reuniao` é o
    registro objetivo de que a pessoa foi pra fila da Amanda — vale mesmo já
    resolvida, porque o que ela diz é "o assunto aqui foi neuro". O texto livre
    pega quem falou de avaliação e ainda assim chegou ao cadastro (ex.: pediu as
    duas coisas). O Hamilton não ajuda aqui: `Paciente` não tem campo de tipo de
    serviço — `fk_modalidade` é online/presencial.
    """
    dados = conversa.dados_coletados or {}
    texto = " ".join(str(dados.get(c) or "") for c in ("motivo_busca", "observacoes")).lower()
    if _MARCA_NEURO in texto:
        return True
    achado = await db.execute(
        select(Escalada.id)
        .where(Escalada.conversa_id == conversa.id, Escalada.motivo == "neuro_reuniao")
        .limit(1)
    )
    return achado.scalar_one_or_none() is not None


async def motivo_para_pular_entrada(db: AsyncSession, conversa: Conversa) -> str | None:
    """Por que NÃO pedir o ORS de entrada desta pessoa (ou `None` se pode pedir).

    Vale pros dois caminhos — a emenda no cadastro e a rede do cron —, de
    propósito: duas listas de guarda divergiriam na primeira mudança, e o custo
    de errar aqui é perguntar "como você está antes de começar?" pra quem já
    começou, ou perguntar ORS pra quem veio fazer uma avaliação neuropsicológica.
    """
    if not config_negocio.valor("pesquisa_entrada_ativa"):
        return "pesquisa de entrada desligada no painel"
    if conversa.estado != "cadastrado" or not conversa.paciente_hamilton_id:
        return "cadastro não concluído no Hamilton"
    if not cadastro.foi_cadastro_novo(conversa):
        return "a ficha já existia no Hamilton (reencontro)"
    if em_pesquisa(conversa):
        return "já tem pesquisa em curso"
    # Mesmo teste de `cobranca.em_cobranca`, inline: importar `cobranca` aqui no
    # topo fecharia o ciclo (ele importa este módulo).
    if conversa.cobranca_iniciada_em is not None and conversa.cobranca_encerrada_em is None:
        return "cobrança em curso"
    if conversa.modo != "bot":
        # A Thainá está conduzindo. Emendar pesquisa por cima é a Sofia
        # atropelando um humano — o mesmo problema que o "Assumir controle"
        # existiu pra resolver.
        return "conversa em modo humano"
    if conversa.arquivada_em is not None:
        return "conversa arquivada"
    if _quem_responde(conversa) == "acompanhante":
        # Sem o ORS não sobra pesquisa: o que restaria é só a nota do acolhimento,
        # e ela não justifica abrir um questionário pra quem acabou de responder
        # um cadastro inteiro pelo filho.
        return "quem escreve é acompanhante, não o paciente"
    if await _e_neuro(db, conversa):
        return "avaliação neuropsicológica (o ORS é de terapia)"
    return None


async def iniciar_entrada(
    db: AsyncSession, conversa: Conversa, agora: datetime | None = None
) -> bool:
    """Abre o ORS de linha de base logo depois do cadastro, na mesma conversa.

    Este é o caminho principal da pesquisa de entrada. Ele não passa pela fila
    de pendentes do Hamilton (e portanto não depende de `SOFIA_PESQUISAS_ATIVAS`):
    a Sofia acabou de criar essa avaliação e já tem o `pk` dela na mão. As travas
    de lá seguram o acumulado histórico de pendentes; aqui não há acumulado
    nenhum, é um cadastro que aconteceu há segundos.
    """
    motivo = await motivo_para_pular_entrada(db, conversa)
    if motivo:
        logger.info("Pesquisa de entrada dispensada (conversa=%s): %s", conversa.id, motivo)
        return False

    try:
        # Idempotente do lado do Hamilton: repetir devolve a que já existe.
        avaliacao = await hamilton_client.get_hamilton_client().criar_avaliacao_entrada(
            conversa.paciente_hamilton_id
        )
    except hamilton_client.HamiltonError as exc:
        logger.error(
            "Não consegui criar a avaliação de entrada da conversa %s: %s", conversa.id, exc
        )
        return False
    if not avaliacao:
        return False

    # Já abordada antes (a rede do cron rodando em cima de uma emenda que já
    # saiu, ou o contrário). 'pendente' quer dizer sem RESPOSTA, não sem envio.
    if avaliacao.get("sofia_enviada_em") or (avaliacao.get("status") or "pendente") != "pendente":
        return False

    # O POST devolve um payload enxuto; o `momento` é o seletor do roteiro e
    # precisa estar lá mesmo que o serializer mude.
    return await iniciar(db, conversa, {**avaliacao, "momento": MOMENTO_LINHA_DE_BASE}, agora)


async def responder(db: AsyncSession, conversa: Conversa, numero: str) -> None:
    """Conduz um turno da pesquisa (chamado no lugar do turno normal do bot)."""
    try:
        avaliacao = await _buscar_avaliacao(conversa)
    except hamilton_client.HamiltonError as exc:
        # "Não consegui saber" NÃO é "não existe". Falha transitória mantém a
        # pesquisa de pé: a pessoa repete, ou o lembrete de 20h pega. Encerrar
        # aqui destruiria estado por causa de um 502 do proxy — foi o que
        # aconteceu com a pesquisa 392 e matou a conversa em silêncio.
        logger.error(
            "Hamilton indisponível no turno da pesquisa %s; mantendo em curso: %s",
            conversa.pesquisa_avaliacao_id,
            exc,
        )
        return
    if avaliacao is None:
        # Aqui o Hamilton RESPONDEU e a avaliação não está lá. Sem contexto não
        # dá pra conduzir; encerra o modo pesquisa e deixa a conversa voltar ao
        # fluxo normal em vez de travar o paciente.
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
    reclamacao = False

    # Quem respondeu alguma coisa e depois sumiu ainda tem texto a extrair.
    # Quem recusou de cara não: a transcrição são duas mensagens e a chamada
    # ao modelo seria desperdício.
    if respondeu_algo or not recusou:
        historico = await conversation.carregar_historico(db, conversa, limite=60)
        try:
            extraido = await extrair_respostas(historico, avaliacao)
            # Sinal só pro alerta: NÃO é campo da Avaliacao e não pode ir no PATCH.
            reclamacao = bool(extraido.pop(CHAVE_RECLAMACAO, False))
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

    # O alerta vem DEPOIS de gravar: mesmo que o Hamilton esteja fora, a Thainá
    # precisa saber que alguém deu nota 2 no terapeuta.
    # O alerta olha o UNIÃO do que a tool gravou com o que a extração
    # preencheu: a precedência da tool tira do payload justamente os campos
    # do caminho principal, e sem isso o alerta ficaria cego pra eles.
    await _alertar_se_precisar(db, conversa, {**_valores_gravados(conversa), **payload}, reclamacao)
    await _limpar(db, conversa)

    # Encadeia a cobrança da mensalidade (Demanda D). Os três desfechos passam por
    # aqui — respondida, recusada e expirada por prazo —, então é o único ponto que
    # precisa da chamada. Vem DEPOIS do `_limpar` porque `cobranca._elegivel` recusa
    # conversa em pesquisa: com o modo ainda ligado, ela nunca cobraria ninguém.
    #
    # Não é dependência técnica, é sequência: quem não tem pesquisa é cobrado pelo
    # cron. E `encadear` revalida tudo no Hamilton, porque a pesquisa dispara pra
    # quem só teve a consulta LANÇADA e a cobrança exige REALIZADA — quem faltou à
    # primeira sessão responde a pesquisa e não pode ser cobrado.
    from app.services import cobranca

    try:
        await cobranca.encadear(db, conversa)
    except Exception as exc:  # noqa: BLE001 — cobrança nunca derruba a pesquisa
        logger.error("Falha ao encadear a cobrança da conversa %s: %s", conversa.id, exc)


# --------------------------------------------------------------------------- #
# Alertas pra Thainá
# --------------------------------------------------------------------------- #

# Chave que o modelo devolve na extração pra sinalizar reclamação. NÃO é campo da
# `Avaliacao`: é retirada do payload antes do PATCH. Reclamação se detecta lendo
# a conversa, não por palavra-chave — "foi ruim pra mim naquele período" não é
# reclamação, e em português a lista de palavras erraria nos dois sentidos.
CHAVE_RECLAMACAO = "alerta_reclamacao"

# nota -> (chave do limiar em config_negocio, como descrever pra Thainá).
_LIMIARES = {
    "qualidade_geral": ("alerta_nota_terapeuta", "nota do terapeuta"),
    "nota_sofia": ("alerta_nota_sofia", "nota do acolhimento"),
    "nota_indicacao": ("alerta_nota_indicacao", "nota de indicação"),
}


def motivos_de_alerta(payload: dict, reclamacao: bool = False) -> list[str]:
    """O que nesta pesquisa merece a atenção de um humano.

    **O ORS não gera alerta nenhum**, de propósito: a Sofia não se intromete com
    nota de bem-estar — ela segue o fluxo e o terapeuta competente cuida disso.
    Crise se detecta pela descrição clara do paciente, com o modelo de crise que
    já existe, nunca por nota de escala.
    """
    motivos: list[str] = []
    for campo, (chave, rotulo) in _LIMIARES.items():
        nota = payload.get(campo)
        limiar = config_negocio.valor(chave)
        # Limiar zero desliga o alerta daquela nota (ajustável no painel).
        if isinstance(nota, int) and not isinstance(nota, bool) and limiar and nota < limiar:
            motivos.append(f"{rotulo} {nota}")

    if payload.get("continuar_terapeuta") is False:
        # Sempre alerta: pegar match ruim na sessão 1 vale mais que qualquer nota.
        motivos.append("não sentiu encaixe com o terapeuta")
    if payload.get("continuar_allos") is True:
        # Isto é boa notícia, mas exige ação: alguém tem que fazer o novo match.
        motivos.append("QUER CONTINUAR na Allos com outro terapeuta")
    if reclamacao:
        motivos.append("RELATOU EXPERIÊNCIA RUIM / reclamação")
    return motivos


async def _alertar_se_precisar(
    db: AsyncSession, conversa: Conversa, payload: dict, reclamacao: bool
) -> None:
    """Manda um alerta consolidado e põe a conversa na fila do painel.

    Sem isto o desenho inteiro seria **coletar e arquivar**: as respostas
    entrariam no banco e ninguém saberia. É o alerta que transforma a pesquisa de
    custo em produto.
    """
    motivos = motivos_de_alerta(payload, reclamacao)
    if not motivos:
        return

    conversa.alerta_pesquisa_em = _agora()
    conversa.alerta_pesquisa_motivos = "; ".join(motivos)
    # Alerta novo reabre a fila: a Thainá precisa ver este, mesmo tendo tratado
    # o da pesquisa anterior desta mesma pessoa.
    conversa.alerta_resolvido_em = None
    await db.flush()
    # Falha no envio não derruba nada: a fila do painel já registrou.
    await escalation.alertar_pesquisa(conversa, motivos)


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

    # Sinal de alerta: não é campo da Avaliacao, mas atravessa a normalização
    # pra chegar em `finalizar`, que o retira antes do PATCH.
    if dados.get(CHAVE_RECLAMACAO) is True:
        limpo[CHAVE_RECLAMACAO] = True

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
    resumo = {
        "enviadas": 0,
        "lembretes": 0,
        "encerradas": 0,
        "entradas_criadas": 0,
        "entradas_encerradas": 0,
    }

    # Vem ANTES de tudo: derrubar a linha de base de quem já foi atendido libera a
    # conversa pra pesquisa da 1ª sessão já nesta mesma rodada. No fim da função
    # custaria um tick inteiro de atraso por pessoa.
    resumo["entradas_encerradas"] = await _encerrar_entradas_atendidas(db)

    # Rede da linha de base: o caminho principal é a emenda no cadastro
    # (`iniciar_entrada`), e isto aqui pega quem escapou dela — cadastro feito
    # pelo painel, ou convite que não conseguiu sair na hora. Diferente do resto
    # da rodada, não passa pela fila de pendentes do Hamilton.
    resumo["entradas_criadas"] = await _abrir_entradas(db, agora)

    try:
        pendentes = await hamilton_client.get_hamilton_client().avaliacoes_pendentes()
    except hamilton_client.HamiltonError as exc:
        logger.error("Não consegui buscar as avaliações pendentes: %s", exc)
        pendentes = []

    pendentes = await _descartar_entradas_obsoletas(pendentes)

    for avaliacao in pendentes:
        conversa = await _conversa_do_paciente(db, avaliacao)
        if conversa is None:
            # Paciente que não veio pela Sofia não tem conversa aberta, e fora da
            # janela de 24h da Meta não dá pra iniciar uma com texto livre.
            # Fica pra equipe abordar (exigiria um template aprovado).
            continue
        # Modo humano NÃO bloqueia mais: a pesquisa e a cobrança acontecem mesmo
        # com escalada aberta (decisão do Paulo — "se teve primeira sessão
        # realizada, essa conversa TEM que acontecer"). O portão do webhook abre
        # exceção pros dois modos, então a resposta da pessoa não cai no vazio.
        if em_pesquisa(conversa):
            continue  # já tem pesquisa em curso, ou a conversa está com a Thainá
        if await iniciar(db, conversa, avaliacao, agora):
            resumo["enviadas"] += 1

    resumo.update(await _acompanhar_em_curso(db, agora))
    logger.info(
        "Pesquisas: %s entradas abertas, %s entradas obsoletas encerradas, "
        "%s enviadas, %s lembretes, %s encerradas",
        resumo["entradas_criadas"],
        resumo["entradas_encerradas"],
        resumo["enviadas"],
        resumo["lembretes"],
        resumo["encerradas"],
    )
    return resumo


async def _descartar_entradas_obsoletas(pendentes: list[dict]) -> list[dict]:
    """Tira da fila a linha de base de quem já não tem mais um "antes".

    A linha de base mede **quem ainda não começou**. Duas coisas tiram esse
    sentido, e as duas nasceram do mesmo teste:

    1. A pessoa já tem pendente a pesquisa da 1ª sessão (ou uma de saída): o par
       pré/pós não existe mais.
    2. A **primeira sessão já foi realizada**. Este é o critério que manda, e não
       o item 1: o gatilho da fila é a consulta ser *lançada*, então o item 1 só
       enxerga quem tem duas pendentes ao mesmo tempo — quem respondeu a da 1ª
       sessão (ou nunca a recebeu) passava batido.

    Disparar num desses casos significa perguntar *"como você está antes de
    começar?"* pra quem já foi atendido e já pagou. Foi o que aconteceu com a
    avaliação 393 no teste de 09/08.

    `is_realizado` e não "consulta lançada" porque **faltar não é ter começado**:
    quem remarcou a primeira sessão continua sem um "antes" medido, e é o único
    momento em que esse número pode ser colhido. É o mesmo critério da cobrança.

    Marcar como `nao_respondeu` em vez de deixar pendente é o que impede a
    obsoleta de ressuscitar num tick futuro.
    """
    entradas = [a for a in pendentes if a.get("momento") == MOMENTO_LINHA_DE_BASE]
    if not entradas:
        return pendentes

    com_outra = {
        a.get("fk_paciente")
        for a in pendentes
        if a.get("momento") != MOMENTO_LINHA_DE_BASE and a.get("fk_paciente")
    }

    cliente = hamilton_client.get_hamilton_client()

    ja_atendidos: set[int] = set()
    ids = [a["fk_paciente"] for a in entradas if a.get("fk_paciente")]
    if ids:
        try:
            status = await cliente.status_primeira_consulta(ids)
            ja_atendidos = {
                pid for pid, info in status.items() if info.get("primeira_consulta_realizada")
            }
        except hamilton_client.HamiltonError as exc:
            # Segue com o critério 1 só. Descartar às cegas jogaria fora a única
            # medida pré-tratamento por causa de um 502 — e ela não tem segunda
            # chance: depois da primeira sessão não existe mais "antes".
            logger.warning("Não consegui checar a 1ª consulta das linhas de base: %s", exc)

    vivas = []
    for avaliacao in pendentes:
        paciente = avaliacao.get("fk_paciente")
        atendido = paciente in ja_atendidos
        obsoleta = avaliacao.get("momento") == MOMENTO_LINHA_DE_BASE and (
            atendido or paciente in com_outra
        )
        if not obsoleta:
            vivas.append(avaliacao)
            continue
        pk = avaliacao.get("pk_avaliacao")
        logger.info(
            "Linha de base %s descartada: %s",
            pk,
            "a 1ª sessão já foi realizada"
            if atendido
            else "o paciente já tem pesquisa de outro momento",
        )
        try:
            await cliente.atualizar_avaliacao(pk, {"status": "nao_respondeu"})
        except hamilton_client.HamiltonError as exc:
            # Fica pendente e cai aqui de novo no próximo tick. O que não pode é
            # ela seguir pro `iniciar` desta rodada.
            logger.error("Não consegui descartar a linha de base %s: %s", pk, exc)
    return vivas


async def _encerrar_entradas_atendidas(db: AsyncSession) -> int:
    """Derruba a linha de base **em curso** de quem já fez a primeira sessão.

    `_descartar_entradas_obsoletas` cuida da fila do Hamilton; isto cuida de quem
    já foi abordado e está com a pesquisa aberta — caso que nada pegava. A janela
    da entrada é de 5 dias (`DIAS_LIMITE_ENTRADA`) e o encerramento por silêncio
    é de 44h (`HORAS_ENCERRAMENTO`), os dois maiores que o tempo típico até a 1ª
    sessão, então não é borda:

    * a pesquisa da 1ª sessão ficava esperando a faixa (é uma por conversa), e
    * se a pessoa respondesse depois da sessão, o ORS "de antes" entrava no banco
      medindo alguém que já está em tratamento. O par pré/pós ficava
      **corrompido**, não vazio — pior, porque um número errado ninguém percebe.

    Quem não respondeu nada vira `nao_respondeu` sem gastar chamada ao modelo;
    quem respondeu parte fica `avaliado` com o que deu (`finalizar` decide, e o
    relatório de ORS filtra pelos quatro itens presentes, não pelo status).
    `finalizar` também libera a faixa e encadeia a cobrança — quem fez a sessão
    pode ser cobrado.
    """
    conversas = list(
        (
            await db.execute(
                select(Conversa).where(
                    Conversa.pesquisa_avaliacao_id.isnot(None),
                    Conversa.paciente_hamilton_id.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not conversas:
        return 0

    try:
        status = await hamilton_client.get_hamilton_client().status_primeira_consulta(
            [c.paciente_hamilton_id for c in conversas]
        )
    except hamilton_client.HamiltonError as exc:
        # Sem o status não há o que decidir. A pesquisa em curso continua de pé e
        # o próximo tick tenta de novo; encerrar às cegas apagaria uma pesquisa
        # legítima por causa de uma falha de rede.
        logger.warning("Não consegui checar a 1ª consulta das pesquisas em curso: %s", exc)
        return 0
    atendidos = {pid for pid, info in status.items() if info.get("primeira_consulta_realizada")}
    if not atendidos:
        return 0

    encerradas = 0
    for conversa in conversas:
        if conversa.paciente_hamilton_id not in atendidos:
            continue
        # O `momento` não é coluna da conversa: só o Hamilton sabe qual das quatro
        # pesquisas está em curso, e derrubar a errada calaria a pesquisa da 1ª
        # sessão justamente de quem acabou de ser atendido.
        try:
            avaliacao = await _buscar_avaliacao(conversa) or {}
        except hamilton_client.HamiltonError as exc:
            logger.warning(
                "Não consegui ler a avaliação %s da conversa %s: %s",
                conversa.pesquisa_avaliacao_id,
                conversa.id,
                exc,
            )
            continue
        if avaliacao.get("momento") != MOMENTO_LINHA_DE_BASE:
            continue
        logger.info(
            "Linha de base %s encerrada (conversa=%s): a 1ª sessão já foi realizada",
            conversa.pesquisa_avaliacao_id,
            conversa.id,
        )
        await finalizar(db, conversa, avaliacao, recusou=True)
        encerradas += 1
    return encerradas


async def _abrir_entradas(db: AsyncSession, agora: datetime) -> int:
    """Rede da pesquisa de linha de base: pega quem a emenda no cadastro não pegou.

    É o único ponto de medida ANTES do tratamento: sem ele não existe par
    pré/pós, e o ORS sozinho não significa nada — o dado só vale como
    `ORS saída − ORS entrada`.

    Diferente do resto da rodada, aqui a Sofia **cria e aborda no mesmo tick**,
    sem passar pela fila de pendentes do Hamilton — ela acabou de criar a
    avaliação e já tem o `pk`. Antes eram dois ticks (um criava, o seguinte
    abordava) e a coisa dependia de `SOFIA_PESQUISAS_ATIVAS`, que existe pra
    segurar o acumulado histórico e não tem nada a ver com um cadastro de ontem.

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
    candidatas = list((await db.execute(q)).scalars().all())
    if not candidatas:
        return 0

    # Quem já foi atendido não tem mais "antes". A guarda vem do Hamilton porque
    # é lá que mora o `is_realizado`; se ele estiver fora, seguimos sem ela — o
    # `_descartar_entradas_obsoletas` ainda pega o caso pela fila.
    ja_atendidos: set[int] = set()
    try:
        status = await hamilton_client.get_hamilton_client().status_primeira_consulta(
            [c.paciente_hamilton_id for c in candidatas]
        )
        ja_atendidos = {
            pid for pid, info in status.items() if info.get("primeira_consulta_realizada")
        }
    except hamilton_client.HamiltonError as exc:
        logger.warning("Não consegui checar a 1ª consulta antes da linha de base: %s", exc)

    abertas = 0
    for conversa in candidatas:
        if conversa.paciente_hamilton_id in ja_atendidos:
            logger.info(
                "Linha de base pulada (conversa=%s): a primeira consulta já foi realizada",
                conversa.id,
            )
            continue
        if await iniciar_entrada(db, conversa, agora):
            abertas += 1
    return abertas


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
            try:
                avaliacao = await _buscar_avaliacao(conversa) or {}
            except hamilton_client.HamiltonError as exc:
                # Encerrar às cegas gravaria o desfecho errado (ou nenhum). O
                # prazo já passou: mais um tick de espera não custa nada.
                logger.error(
                    "Hamilton indisponível; encerramento da pesquisa %s adiado: %s",
                    conversa.pesquisa_avaliacao_id,
                    exc,
                )
                continue
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
        atual = await cliente.obter_avaliacao(pk)
    except hamilton_client.HamiltonError:
        return False  # tenta no próximo tick; não insiste às cegas
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

    await _marcar_gravado(db, conversa, campo, valor)
    return {"status": "registrado", "campo": campo}


async def _marcar_gravado(db: AsyncSession | None, conversa, campo: str, valor) -> None:
    """Anota o que a tool gravou: o campo **e o valor**.

    O valor importa porque o alerta pra Thainá é decidido no fim, e o que a tool
    gravou não passa pelo `payload` do PATCH final (a precedência da tool tira de
    lá). Guardar só o nome do campo deixaria o alerta cego justamente para o
    caminho principal — nota 2 no terapeuta não geraria aviso nenhum.
    """
    dados = dict(getattr(conversa, "dados_coletados", None) or {})
    gravados = dict(_valores_gravados(conversa))
    gravados[campo] = valor
    dados[_CHAVE_GRAVADOS] = gravados
    conversa.dados_coletados = dados
    if db is not None:
        await db.flush()


def _valores_gravados(conversa) -> dict:
    """O que a tool gravou nesta pesquisa, como {campo: valor}.

    Tolera o formato antigo (lista de nomes): uma pesquisa que já estava em curso
    no momento do deploy não pode explodir aqui.
    """
    dados = getattr(conversa, "dados_coletados", None) or {}
    bruto = dados.get(_CHAVE_GRAVADOS)
    if isinstance(bruto, dict):
        return bruto
    if isinstance(bruto, list):
        return {campo: None for campo in bruto}
    return {}


def _ja_gravados(conversa) -> set[str]:
    return set(_valores_gravados(conversa))


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
    """Recupera do Hamilton a avaliação em curso nesta conversa.

    `None` significa **o Hamilton respondeu e ela não existe mais**. Falha de
    rede/servidor sobe como `HamiltonError` — de propósito, e quem chama tem que
    tratar. Antes os dois casos viravam `None`, e o `responder` encerrava a
    pesquisa: um 502 do proxy no meio de uma conversa em andamento apagava o
    `pesquisa_avaliacao_id` e a pessoa ficava sem resposta, sem erro visível.
    """
    if conversa.pesquisa_avaliacao_id is None:
        return None
    return await hamilton_client.get_hamilton_client().obter_avaliacao(
        conversa.pesquisa_avaliacao_id
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


# Aliases públicos: o painel precisa destes dois quando a Thainá **interrompe** uma
# pesquisa em curso pra assumir a conversa (`painel.assumir`). São o mesmo código
# usado internamente — expostos com nome público em vez de o painel alcançar um
# `_privado` de outro módulo.
buscar_avaliacao = _buscar_avaliacao
limpar = _limpar
