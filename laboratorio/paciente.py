"""O paciente simulado: um LLM barato interpretando uma persona de arquivo.

Ele é o **outro lado** da conversa, e a inversão de papéis importa: para este
modelo, a fala da Sofia é `user` e a dele é `assistant`. Sem isso ele começa a
completar a fala da Sofia em vez de responder a ela.

Duas garantias que este módulo dá:

1. **Ele não sabe que é um teste.** Nada no prompt fala de prompt, de Sofia, de
   avaliação ou de bot. Um paciente que sabe que está sendo avaliado colabora, e
   paciente colaborativo é o teste que não serve pra nada.
2. **`falas_obrigatorias` são cobradas.** O caso de borda tem que ser exercitado
   de fato; deixar por conta do sorteio do modelo faz a persona "passar" por
   nunca ter tocado no assunto que ela existe pra tocar.
"""

import logging
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Tolerante a colchete extra/faltando: o paciente simulado já emitiu
# `[[]ENCERROU:ok]]`, o regex estrito não casou, o encerramento passou batido e o
# marcador vazou pra dentro da fala dele na transcrição. Erro de formatação do
# simulador não pode virar erro de medição.
MARCADOR = re.compile(r"\[*\s*\]*\s*ENCERROU\s*:\s*(ok|desisti)\s*\]*", re.IGNORECASE)
SEPARADOR = "---"


def _system_prompt(persona: dict) -> str:
    ident = persona.get("identidade", {})
    linhas_ident = "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in ident.items())
    comp = persona.get("comportamento", {}) or {}

    regras = [
        "Escreva como gente escreve no WhatsApp: frases curtas, minúscula quando "
        "for natural, sem pontuação caprichada, sem parágrafo formal.",
        "Nunca escreva mais do que 2 ou 3 linhas por mensagem.",
        "Você não sabe nada sobre a Allos além do que te contarem nesta conversa.",
        "Não seja prestativo nem colaborativo além do que a sua personalidade "
        "pede. Se te derem informação demais de uma vez, reaja como você reagiria.",
        "Só forneça um dado seu (nome, nascimento, endereço) quando pedirem.",
    ]
    if comp.get("responde_curto"):
        regras.append("Você responde curto. Às vezes uma palavra só.")
    if comp.get("manda_picado"):
        regras.append(
            f"Você costuma mandar o pensamento picado em 2 ou 3 mensagens seguidas. "
            f"Quando fizer isso, separe as mensagens com uma linha contendo só "
            f"`{SEPARADOR}`."
        )
    if comp.get("demora_a_confiar"):
        regras.append("Você desconfia. Antes de topar qualquer coisa, você questiona.")

    return f"""Você é uma pessoa real escrevendo pelo WhatsApp para uma ONG de psicologia
em Belo Horizonte. Você está do outro lado, como cliente.

# Quem você é
{linhas_ident}

# Como você é
{persona.get('personalidade', '').strip()}

# O que te levou a escrever
{persona.get('objetivo', '').strip()}

# Como você escreve
{chr(10).join('- ' + r for r in regras)}

# Como esta conversa termina
Só existe um jeito de esta conversa acabar bem:

{persona.get('encerra_quando', 'Você combinou um próximo passo concreto e não tem mais nada a perguntar.').strip()}

Quando isso acontecer, mande sua última mensagem e termine com `[[ENCERROU:ok]]`.

**Receber uma informação não é acabar.** Se te responderam o preço, você ainda
quer saber do horário. Se te explicaram como funciona, você ainda quer saber
como começa. Uma pessoa de verdade não agradece e some assim que ouve um número;
ela continua até resolver o que a trouxe aqui. Enquanto houver uma pergunta
natural a fazer, faça a pergunta em vez de encerrar.

Se, ao contrário, você perder o interesse, se irritar, se cansar de não ser
respondida ou decidir procurar outro lugar, mande sua última mensagem e termine
com `[[ENCERROU:desisti]]`. Desista de verdade só se você desistiria de verdade
— mas não force educação: se a conversa te perdeu, ela te perdeu.

Responda apenas com o texto da sua mensagem. Nada de aspas, nada de narração.
"""


class Paciente:
    """Gera a próxima fala do paciente a partir da conversa até aqui."""

    def __init__(self, persona: dict, modelo: str, contador, api_key: str) -> None:
        self.persona = persona
        self._modelo = modelo
        self._contador = contador
        self._cliente = AsyncOpenAI(api_key=api_key)
        self._system = _system_prompt(persona)
        self._pendentes = list(persona.get("falas_obrigatorias") or [])

    def _cobranca(self, turno: int) -> list[dict]:
        """Lembra o modelo do assunto que esta persona existe pra levantar.

        Só depois do 2º turno: cobrar cedo demais faz a pessoa despejar a
        pergunta de forma artificial na primeira mensagem.
        """
        if not self._pendentes or turno < 2:
            return []
        return [
            {
                "role": "system",
                "content": (
                    "Antes desta conversa acabar, você ainda precisa ter tocado "
                    "nestes assuntos, com as suas palavras e quando fizer sentido: "
                    + "; ".join(self._pendentes)
                ),
            }
        ]

    def _baixar_pendentes(self, texto: str) -> None:
        alvo = texto.lower()
        # Casamento por palavra-chave: a persona diz com as palavras dela, então
        # exigir a frase literal só geraria cobrança eterna.
        self._pendentes = [
            p
            for p in self._pendentes
            if not any(t in alvo for t in p.lower().split() if len(t) > 4)
        ]

    async def falar(self, historico_sofia: list[dict], turno: int) -> tuple[list[str], str | None]:
        """Devolve (mensagens, encerramento).

        `mensagens` é uma lista porque uma persona `manda_picado` manda várias
        seguidas antes de a Sofia responder — que é o que a Demanda 2 (debounce)
        existe pra tratar e o que o harness em processo não simula sozinho.
        `encerramento` é 'ok', 'desisti' ou None.
        """
        mensagens = [
            {"role": "system", "content": self._system},
            *historico_sofia,
            *self._cobranca(turno),
        ]
        resp = await self._cliente.chat.completions.create(
            model=self._modelo, messages=mensagens
        )
        self._contador.registrar(self._modelo, getattr(resp, "usage", None))
        texto = (resp.choices[0].message.content or "").strip()

        encerramento = None
        achado = MARCADOR.search(texto)
        if achado:
            encerramento = achado.group(1).lower()
            texto = MARCADOR.sub("", texto).strip()

        self._baixar_pendentes(texto)
        partes = [p.strip() for p in texto.split(f"\n{SEPARADOR}") if p.strip()]
        if not partes:
            # Modelo respondeu só o marcador: a pessoa saiu sem falar nada.
            partes = ["..."] if encerramento is None else []
        return partes, encerramento
