"""Valores editáveis pela Thainá no painel (preço, follow-up, presença, debounce).

Ficam na tabela `configuracao` (chave/valor texto). Um cache em memória evita ler
o banco a cada mensagem; é populado no startup (main.lifespan) e atualizado a
cada salvamento no painel. O Render free roda 1 instância, então o cache em
memória basta; o padrão de cada campo vem das settings (env/código), e o valor
salvo no painel tem prioridade sobre o env.
"""

import logging
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Configuracao

logger = logging.getLogger(__name__)


class Campo(NamedTuple):
    """Metadados de um ajuste do painel.

    É `NamedTuple` e não `dataclass` de propósito: os três primeiros itens
    continuam acessíveis por índice (`campo[0]`, `campo[2]`), que é como o
    router e os testes antigos liam isto quando `CAMPOS` era tupla pura.

    `ajuda` existe porque a tela não tinha onde pôr explicação: o rótulo virava
    a frase inteira ("Desconto máximo que a Sofia pode oferecer sozinha na
    terapia (%) — 0 desliga") e o resto do contexto ia pra um bloco solto no
    rodapé, longe do campo que ele explicava. `prefixo`/`sufixo` tiram a unidade
    do rótulo e a colam no input (R$ 200, 20 h, 10 %).
    """

    rotulo: str
    padrao: object
    tipo: str  # "int" | "bool" | "texto"
    ajuda: str = ""
    grupo: str = "outros"
    prefixo: str = ""
    sufixo: str = ""


# Seções da tela, na ordem em que aparecem. A primeira é a que muda o que a
# Sofia FAZ; as outras só ajustam números do que ela já faz.
GRUPOS: list[tuple[str, str, str, str]] = [
    (
        "automacoes",
        "O que a Sofia faz sozinha",
        "bi-magic",
        "Cada chave liga um comportamento automático. Desligar é sempre seguro — "
        "ela simplesmente para de fazer aquilo.",
    ),
    (
        "valores",
        "Valores que ela informa",
        "bi-cash-coin",
        "Os números que a Sofia fala na conversa e usa pra gerar link de pagamento.",
    ),
    (
        "ritmo",
        "Ritmo das conversas",
        "bi-clock",
        "O WhatsApp só deixa mandar mensagem livre até <b>24 h</b> depois da última "
        "mensagem do paciente. Por isso tudo aqui é menor que 24.",
    ),
    (
        "alertas",
        "Quando me avisar de uma pesquisa",
        "bi-bell",
        'Nota <b>abaixo</b> do valor aparece em "Precisa de você agora". '
        "<em>Em 0, aquele aviso fica desligado.</em>",
    ),
]

# chave -> Campo. A ordem aqui é a ordem dentro de cada grupo.
CAMPOS: dict[str, Campo] = {
    # --- Cobrança da mensalidade (Demanda D) ---
    # Desligada por padrão, de propósito. É o mesmo desenho das travas
    # SOFIA_PESQUISAS_* do Hamilton: um fluxo automático que fala de DINHEIRO com
    # paciente sobe dark e é ligado por ato explícito de quem opera, nunca por
    # deploy. Desligada, o cron não aborda ninguém e nada mais muda.
    "cobranca_ativa": Campo(
        "Cobrar a mensalidade",
        False,
        "bool",
        "Depois que o terapeuta marca a primeira sessão como <b>realizada</b>, a Sofia "
        "manda o valor, o Pix e o link do cartão — e insiste uma vez. "
        "<em>Ligada, ela fala de dinheiro com o paciente sem passar por você.</em>",
        "automacoes",
    ),
    # Contrato terapêutico assinado (Demanda E). Desligado por padrão, e com
    # chave PRÓPRIA — não pendurada na `cobranca_ativa`.
    #
    # A cobrança nunca rodou em produção. Estrear as duas ao mesmo tempo, no mesmo
    # turno que fala de dinheiro com paciente, é o tipo de risco que este projeto
    # já pagou: o parcelado do Stripe subiu sem ter rodado de verdade uma vez e
    # passou 18 assinaturas cobrando pra sempre. Liga-se a cobrança primeiro,
    # sozinha, e o contrato semanas depois.
    "contrato_ativo": Campo(
        "Mandar o contrato pra assinar",
        False,
        "bool",
        "Junto da cobrança da mensalidade, a Sofia manda o contrato terapêutico "
        "pra pessoa assinar pelo celular. <em>Assinar não trava o atendimento: quem "
        "não assinar aparece na tela Hoje.</em> O texto fica em "
        '<a href="/painel/prompts">Prompts</a>.',
        "automacoes",
    ),
    # Interruptor da pesquisa de ENTRADA (ORS de linha de base), e só dela.
    #
    # Nasce LIGADA, ao contrário da cobrança: ela não fala de dinheiro e não tem
    # como virar disparo em massa — só dispara em cadastro que a Sofia acabou de
    # fazer, um por vez. As outras três pesquisas continuam presas às travas
    # SOFIA_PESQUISAS_* do Hamilton, que existem pra segurar uma coisa diferente
    # (o acumulado de anos de pendentes que os signals criaram). Amarrar as duas
    # coisas fazia "ligar a linha de base" ser uma decisão de risco alto quando
    # devia ser de risco zero.
    "pesquisa_entrada_ativa": Campo(
        "Pedir o ORS de entrada",
        True,
        "bool",
        "Logo depois do cadastro de terapia, a Sofia colhe as quatro notas de linha "
        "de base. Sem isso não dá pra medir a evolução do paciente depois.",
        "automacoes",
    ),
    # Nasce LIGADA, e aqui a convenção das outras chaves se INVERTE: em pesquisa e
    # cobrança, desligado = a Sofia não faz nada. Nesta, desligado = as assinaturas
    # de parcelado seguem cobrando depois da última parcela combinada. O estado
    # seguro é ligado.
    "limitar_parcelado_ativo": Campo(
        "Encerrar o parcelado na última parcela",
        True,
        "bool",
        "A avaliação neuropsicológica parcelada é uma assinatura mensal, e o Stripe não "
        "aceita marcar o fim na criação. Ligada, a Sofia marca o encerramento assim que a "
        "pessoa paga. <em>Desligada, a cobrança não para sozinha.</em>",
        "automacoes",
    ),
    "transcrever_audio": Campo(
        "Ouvir áudios",
        settings.transcrever_audio,
        "bool",
        "Transcreve o áudio do paciente e responde em texto. <em>Tem custo por minuto.</em> "
        "Desligada, todo áudio vira escalada pra você.",
        "automacoes",
    ),
    "simular_digitacao": Campo(
        'Mostrar "digitando…" e o visto',
        settings.simular_digitacao,
        "bool",
        "Os tiques azuis e o indicador de digitação, como uma pessoa faria.",
        "automacoes",
    ),
    "preco_terapia_mensal": Campo(
        "Mensalidade da terapia",
        settings.preco_terapia_mensal,
        "int",
        "Cobrada na entrada e todo mês depois disso.",
        "valores",
        prefixo="R$",
    ),
    "preco_neuro": Campo(
        "Orçamento da neuroavaliação",
        settings.preco_neuro,
        "int",
        "A Sofia informa e passa pra Amanda — ela não fecha neuro sozinha.",
        "valores",
        prefixo="R$",
    ),
    "parcelas_max": Campo(
        "Parcelas máximas no cartão",
        settings.parcelas_max,
        "int",
        "Vale pra neuro. São cobranças mensais no cartão, não parcelamento da operadora.",
        "valores",
        sufixo="×",
    ),
    "desconto_maximo_pct": Campo(
        "Desconto que ela pode dar sozinha",
        settings.desconto_maximo_pct,
        "int",
        "Acima disso ela chama você. <em>Em 0, ela nunca oferece desconto.</em>",
        "valores",
        sufixo="%",
    ),
    # Vazia = a Sofia não oferece Pix (só o link do cartão). Não é segredo — é o
    # CNPJ que já vai na nota —, então mora aqui e não nas env vars: muda sem deploy.
    "chave_pix": Campo(
        "Chave Pix da Allos",
        "50.990.346/0001-52",
        "texto",
        "É o CNPJ que já vai na nota. <em>Em branco, a Sofia oferece só o cartão.</em>",
        "valores",
    ),
    "debounce_segundos": Campo(
        "Espera antes de responder",
        int(settings.debounce_segundos),
        "int",
        "Junta as mensagens que chegam em rajada numa resposta só, em vez de responder cada linha.",
        "ritmo",
        sufixo="seg",
    ),
    "followup_horas": Campo(
        "Cutucar o lead que sumiu",
        settings.followup_horas,
        "int",
        "Quem parou de responder recebe uma mensagem, uma vez só.",
        "ritmo",
        sufixo="h",
    ),
    # Lembrete único da cobrança. Tem que caber na janela de 24h da Meta, igual ao
    # follow-up: passada ela, só template resolve, e não temos template aprovado.
    "cobranca_lembrete_horas": Campo(
        "Lembrete da cobrança",
        20,
        "int",
        "Se não respondeu sobre a mensalidade, a Sofia lembra uma vez.",
        "ritmo",
        sufixo="h",
    ),
    # Limiares dos alertas de pesquisa. Nota ABAIXO do valor alerta a Thainá.
    # Padrão literal (não vem de env, diferente da maioria dos campos): o ponto
    # destes três é serem apertados ou afrouxados no painel conforme o volume
    # incomodar, e uma env var seria um botão que ninguém usaria. Zero desliga.
    "alerta_nota_terapeuta": Campo(
        "Nota do terapeuta",
        6,
        "int",
        "Como o paciente avaliou quem o atendeu.",
        "alertas",
        sufixo="/10",
    ),
    "alerta_nota_sofia": Campo(
        "Nota do acolhimento (a Sofia)",
        6,
        "int",
        "Como ele avaliou a própria conversa com o bot.",
        "alertas",
        sufixo="/10",
    ),
    "alerta_nota_indicacao": Campo(
        "Nota de indicação",
        6,
        "int",
        "O quanto ele indicaria a Allos pra outra pessoa.",
        "alertas",
        sufixo="/10",
    ),
}


def campos_do_grupo(grupo: str) -> dict[str, Campo]:
    return {c: campo for c, campo in CAMPOS.items() if campo.grupo == grupo}


_cache: dict[str, object] = {chave: campo.padrao for chave, campo in CAMPOS.items()}


def _tipo(chave: str) -> str:
    return CAMPOS[chave].tipo


def _parse(chave: str, texto: str):
    """Converte o texto guardado no banco pro tipo do campo."""
    tipo = _tipo(chave)
    if tipo == "bool":
        return str(texto).strip().lower() in ("true", "1", "sim", "on")
    if tipo == "texto":
        # Sem int(): campo livre. Vazio é um valor válido (desliga o que depende dele).
        return str(texto).strip()
    return int(texto)


def _serialize(chave: str, valor) -> str:
    """Converte o valor pro texto que vai pro banco."""
    tipo = _tipo(chave)
    if tipo == "bool":
        return "true" if valor else "false"
    if tipo == "texto":
        return str(valor).strip()
    return str(int(valor))


def valores() -> dict[str, object]:
    """Snapshot dos valores atuais (cópia, pra ninguém mutar o cache por engano)."""
    return dict(_cache)


def valor(chave: str):
    return _cache.get(chave, CAMPOS[chave].padrao)


async def carregar_do_banco(db: AsyncSession) -> None:
    """Sobrepõe os padrões com o que estiver salvo no banco. Chamado no startup."""
    rows = (await db.execute(select(Configuracao))).scalars().all()
    for r in rows:
        if r.chave in CAMPOS:
            try:
                _cache[r.chave] = _parse(r.chave, r.valor)
            except (TypeError, ValueError):
                logger.warning("Config inválida ignorada: %s=%r", r.chave, r.valor)


async def salvar(db: AsyncSession, novos: dict) -> None:
    """Persiste (upsert) os valores informados e atualiza o cache em memória."""
    for chave, valor_novo in novos.items():
        if chave not in CAMPOS:
            continue
        texto = _serialize(chave, valor_novo)
        existente = (
            await db.execute(select(Configuracao).where(Configuracao.chave == chave))
        ).scalar_one_or_none()
        if existente:
            existente.valor = texto
        else:
            db.add(Configuracao(chave=chave, valor=texto))
        _cache[chave] = _parse(chave, texto)
    await db.commit()
