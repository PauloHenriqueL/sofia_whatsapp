"""Revisão da fala da Sofia antes de ela sair (ideia tirada do Parlant).

O caso que motivou (conversa 106, 23-24/11/2026): uma terapeuta foi atendida
como lead e a Sofia, sem nenhuma ferramenta para isso, respondeu "Vou considerar
isso na conferência" e "Ficou registrado assim" sobre pagamentos que ela não
registra. No dia seguinte chamou a terapeuta de "Maria", nome de uma paciente
que aparecia no histórico. O prompt já proibia as duas coisas; o modelo não leu.

O Parlant resolve isso conferindo a resposta contra as regras críticas ANTES de
enviar. Aqui são duas camadas:

1. **Código** (`corrigir_nome`): vocativo com nome que não é o da pessoa, quando
   o nome dela é conhecido. Sem custo, sem modelo.
2. **Modelo** (`_revisar_com_modelo`): uma chamada curta, sem ferramentas, que
   confere três regras — ação que não aconteceu, nome errado e repetição — e
   devolve a fala corrigida com a menor mudança possível.

**Falha aberta, sempre.** Revisor fora, lento, ou com resposta ilegível: sai a
fala original. A revisão é uma rede a mais; ela não pode virar o motivo de a
Sofia ficar muda. Também por isso o revisor tem cliente próprio, sem retry e com
prazo curto: o paciente está esperando do outro lado.

Liga e desliga em `/painel/config` (`revisar_respostas`). Vale para o turno de
acolhimento (`webhook.processar_turno_bot`); pesquisa e cobrança têm prompt e
fluxo próprios e ainda não passam por aqui.
"""

import asyncio
import json
import logging
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.services import config_negocio, llm_client

logger = logging.getLogger(__name__)

PROMPT = (Path(__file__).resolve().parents[2] / "prompt" / "revisao.txt").read_text(
    encoding="utf-8"
)
# O revisor roda com o paciente esperando: mais que isso, sai a fala original.
PRAZO_SEGUNDOS = 12
MENSAGENS_DE_CONTEXTO = 12
PROBLEMAS = ("ACAO_INEXISTENTE", "NOME_ERRADO", "REPETICAO")

# Em memória, como `saida.bloqueios()`: zera no restart; o registro é o log.
_correcoes: Counter = Counter()

# Abertura comum seguida de vírgula e de um nome: "Desculpa, Maria." A lista é
# curta de propósito — falso positivo aqui apagaria um pedaço de fala legítima.
_ABERTURAS = (
    "Oi|Olá|Ola|Opa|Desculpa|Desculpe|Obrigada|Obrigado|Perfeito|Certo|Entendi|"
    "Tudo bem|Bom dia|Boa tarde|Boa noite|Que bom|Prazer|Imagina|Combinado|Claro|"
    "Ótimo|Otimo|Beleza|Tá bom|Ta bom|Pode deixar|Anotado|Isso"
)
_VOCATIVO = re.compile(
    rf"(?P<abertura>(?:^|(?<=[.!?\n])\s*)(?:{_ABERTURAS})),\s+"
    r"(?P<nome>[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõçà]+)(?P<fim>[.,!?])",
    re.MULTILINE,
)
# Vocativo no fim da frase: "Obrigada por confirmar, Maria!". Só com ! ou ?: com
# ponto final casaria aposto ("quem paga é a mãe, Tatiana."), que é terceiro.
_VOCATIVO_FINAL = re.compile(
    r"(?P<abertura>\w),\s+(?P<nome>[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõçà]+)(?P<fim>[!?])"
)


def correcoes() -> dict[str, int]:
    return dict(_correcoes)


def total_corrigidas() -> int:
    return _correcoes["falas"]


def zerar() -> None:
    _correcoes.clear()


def _sem_acento(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    ).casefold()


def nome_da_pessoa(conversa) -> str | None:
    """O nome que a Sofia conhece da pessoa: o do paciente ou o do terapeuta."""
    dados = getattr(conversa, "dados_coletados", None) or {}
    for chave in ("nome_completo", "nome", "nome_terapeuta"):
        valor = dados.get(chave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
    return None


def corrigir_nome(texto: str, conversa) -> str:
    """Tira o vocativo com nome que não é o da pessoa ("Desculpa, Maria." → "Desculpa.").

    Só age quando o nome da pessoa é conhecido; sem ele, quem julga é o revisor.
    Não troca por outro nome: tirar é seguro, trocar seria apostar de novo.
    """
    nome = nome_da_pessoa(conversa)
    if not texto or not nome:
        return texto
    partes = {_sem_acento(p) for p in re.findall(r"\w+", nome)}

    def trocar(m: re.Match) -> str:
        if _sem_acento(m.group("nome")) in partes:
            return str(m.group(0))
        _correcoes["NOME_ERRADO"] += 1
        return f"{m.group('abertura')}{m.group('fim')}"

    novo = _VOCATIVO_FINAL.sub(trocar, _VOCATIVO.sub(trocar, texto))
    if novo != texto:
        logger.warning("Revisão: vocativo com nome errado removido (conversa %s)", conversa.id)
    return novo


@lru_cache(maxsize=1)
def get_revisor() -> llm_client.LLMClient:
    """Cliente só do revisor: sem retry e com prazo curto (ver o topo)."""
    return llm_client.OpenAIClient(
        model=settings.openai_model_revisao or settings.openai_model,
        client=AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0, timeout=PRAZO_SEGUNDOS),
        temperature=0,
        esforco=settings.openai_reasoning_effort,
    )


def _acoes(resultados) -> str:
    if not resultados:
        return "nenhuma"
    linhas = []
    for tc, res in resultados:
        status = res.get("status") if isinstance(res, dict) else None
        if status is None and isinstance(res, dict) and "ok" in res:
            status = "ok" if res["ok"] else "falhou"
        linhas.append(f"- {tc.name}: {status or 'executada'}")
    return "\n".join(linhas)


def montar_pedido(conversa, historico: list[dict], texto: str, resultados) -> str:
    falas = [
        f"{'Pessoa' if m['role'] == 'user' else 'Sofia'}: {m['content']}"
        for m in historico
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
    ][-MENSAGENS_DE_CONTEXTO:]
    quem = (
        "terapeuta da Allos"
        if getattr(conversa, "eh_terapeuta", False)
        else ("paciente ou interessado em atendimento")
    )
    return (
        f"NOME DA PESSOA: {nome_da_pessoa(conversa) or 'desconhecido'}\n"
        f"QUEM É: {quem}\n"
        f"AÇÕES DESTE TURNO:\n{_acoes(resultados)}\n\n"
        "HISTÓRICO (mais antigo primeiro):\n" + "\n".join(falas) + "\n\n"
        f"FALA A CONFERIR:\n{texto}"
    )


def interpretar(resposta: str | None, original: str) -> tuple[str, list[str]]:
    """JSON do revisor → (fala a enviar, problemas). Ilegível = aprovada."""
    if not resposta:
        return original, []
    inicio, fim = resposta.find("{"), resposta.rfind("}")
    if inicio < 0 or fim <= inicio:
        return original, []
    try:
        dados = json.loads(resposta[inicio : fim + 1])
    except json.JSONDecodeError:
        return original, []
    if not isinstance(dados, dict) or dados.get("aprovada", True) is not False:
        return original, []
    reescrita = dados.get("reescrita")
    problemas = [p for p in dados.get("problemas") or [] if p in PROBLEMAS]
    if not isinstance(reescrita, str) or not reescrita.strip() or not problemas:
        return original, []
    reescrita = reescrita.strip()
    # Reescrita muito maior que a fala é o revisor inventando, não corrigindo.
    if len(reescrita) > max(2 * len(original), len(original) + 300):
        return original, []
    return reescrita, problemas


async def _revisar_com_modelo(conversa, historico, texto, resultados) -> str:
    resp = await asyncio.wait_for(
        get_revisor().gerar_resposta(
            [{"role": "user", "content": montar_pedido(conversa, historico, texto, resultados)}],
            system_prompt=PROMPT,
        ),
        timeout=PRAZO_SEGUNDOS,
    )
    final, problemas = interpretar(getattr(resp, "texto", None), texto)
    if problemas:
        for p in problemas:
            _correcoes[p] += 1
        # Sem o conteúdo (LGPD): só o que foi corrigido.
        logger.warning(
            "Revisão corrigiu a fala (conversa %s): %s", conversa.id, ",".join(problemas)
        )
    return final


async def revisar(
    conversa, historico: list[dict], texto: str | None, resultados=None
) -> str | None:
    """Confere a fala antes do envio. Nunca levanta: na dúvida, sai a original."""
    if not texto or not config_negocio.valor("revisar_respostas"):
        return texto
    original = texto
    texto = corrigir_nome(texto, conversa)
    try:
        final = await _revisar_com_modelo(conversa, historico, texto, resultados)
    except Exception as exc:  # noqa: BLE001 — falha aberta, de propósito
        logger.warning("Revisão indisponível, fala segue sem conferência: %s", type(exc).__name__)
        final = texto
    if final != original:
        _correcoes["falas"] += 1  # uma por resposta, mesmo corrigida pelas duas camadas
    return final
