"""Conversas de EXEMPLO no painel da Sofia, para a Thainá ver os casos que aparecem.

⚠️ **Valores aqui são escritos à mão e envelhecem.** A mensalidade real vive em
`preco_terapia_mensal` (250 em 17/11/2026) e o desconto máximo em
`desconto_maximo_pct` (10%). Ao mudar o preço no painel, reescrever os diálogos
2 e 7 e rodar este script de novo — senão a Thainá treina num valor que não
existe mais.

Dois deles ("(a implementar)" no título) mostram o comportamento **decidido**, que
ainda não está no roteiro: criança como paciente comum e Profectum sem falar de
dinheiro. Ver `docs/demandas/09-BACKLOG-ROTEIRO.md`.

Seguras de propósito:
- número fictício (5500000000NN): nenhuma mensagem alcança gente de verdade;
- `modo='humano'`: a Sofia não conduz, não responde e não escala;
- `seguimento_enviado_em` preenchido: o follow-up automático nunca dispara;
- `contato_conferido_por='manual'`: a conferência do cron não pergunta ao Hamilton;
- `criada_em` 90 dias atrás: ficam fora do "pessoas novas nos 7 dias";
- escalada só nos casos que de fato param a Sofia: é na aba Hoje que a Thainá trabalha.
Apagar é um DELETE por `numero_whatsapp LIKE '55000000%'`.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from app.database import async_session
from app.models import Conversa, Escalada, Mensagem

AGORA = datetime.now(timezone.utc)
VELHO = AGORA - timedelta(days=90)

# (sufixo, nome, estado, [(quem, texto), ...])
EXEMPLOS = [
    ("01", "[EXEMPLO] Neuro — avaliação para o filho", "escalado", [
        ("paciente", "Oi, quanto custa a avaliação neuropsicológica?"),
        ("bot", "Oi! Sou a Sofia, da Allos. Posso te explicar sim. A avaliação é pra você ou pra outra pessoa?"),
        ("paciente", "Pro meu filho, 8 anos. A escola pediu um laudo."),
        ("bot", "Entendi. A avaliação neuropsicológica é um processo de várias sessões, com "
                "testes e devolutiva, e o valor fica em R$ 1.000,00, parcelável. Quem conduz "
                "essa conversa é a Amanda, nossa coordenadora: ela explica o processo todo e "
                "tira suas dúvidas numa reunião. Posso marcar?"),
        ("paciente", "Pode sim"),
        ("bot", "Combinado! Vou passar seu contato pra Amanda e ela fala com você pra marcar."),
    ]),
    ("02", "[EXEMPLO] (a implementar) Mãe cadastrando a filha de 9 anos", "cadastrado", [
        ("paciente", "Boa tarde, queria terapia para minha filha de 9 anos"),
        ("bot", "Boa tarde! Que bom que procurou. Me conta um pouquinho: o que vocês estão "
                "buscando pra ela?"),
        ("paciente", "Ela anda muito ansiosa desde que mudou de escola"),
        ("bot", "Entendi, obrigada por contar. A mensalidade é R$ 250,00, com uma sessão por "
                "semana, online. Vou fazer o cadastro dela: me diz o nome completo e a data de "
                "nascimento da sua filha?"),
        ("paciente", "Helena Martins Rocha, 14/05/2017"),
        ("bot", "Anotado! Como ela é menor de 18, vai ser necessário o termo de consentimento "
                "dos pais ou responsável — a Thainá te explica, é simples. Me passa um telefone "
                "de contato pra ficha dela? Pode ser o seu mesmo."),
        ("paciente", "pode ser esse meu"),
        ("bot", "Perfeito. A Thainá entra em contato pra combinar o horário e dizer quem vai "
                "atender a Helena."),
    ]),
    ("03", "[EXEMPLO] Risco de vida — crise", "escalado", [
        ("paciente", "não aguento mais, vou me matar hoje"),
        ("bot", "Eu li o que você escreveu e levei a sério. Você está em segurança agora? "
                "Tem alguém com você?"),
        ("paciente", "to sozinha"),
        ("bot", "Fica comigo aqui. Se houver risco imediato, ligue 188 (CVV, 24h, gratuito) "
                "ou 192. Já estou chamando alguém da equipe pra falar com você agora."),
    ]),
    ("04", "[EXEMPLO] Prefeitura conveniada — gratuito", "cadastrado", [
        ("paciente", "Oi, vim pelo convênio da prefeitura"),
        ("bot", "Oi! Sou a Sofia, da Allos. Qual prefeitura?"),
        ("paciente", "Bela Vista de Minas, sou servidora de lá"),
        ("bot", "Perfeito, temos convênio com a Prefeitura de Bela Vista de Minas. Nesse caso "
                "o atendimento é custeado pela prefeitura e gratuito pra você. Vou fazer seu "
                "cadastro: me confirma seu nome completo e data de nascimento?"),
        ("paciente", "Maria das Graças Silva, 12/03/1985"),
        ("bot", "Anotado! Cadastro feito. A Thainá entra em contato pra combinar o horário e "
                "dizer quem vai te atender."),
    ]),
    ("05", "[EXEMPLO] (a implementar) Profectum — dinheiro é com o financeiro deles", "cadastrado", [
        ("paciente", "Boa noite, me indicaram vocês pelo Profectum"),
        ("bot", "Boa noite! Sou a Sofia, da Allos. Me conta o que você está buscando?"),
        ("paciente", "Terapia pra mim. Pelo Profectum é gratuito, né?"),
        ("bot", "Quem cuida dessa parte é o setor financeiro do Profectum — vale perguntar "
                "direto com eles, que te explicam certinho. Do nosso lado eu já posso seguir: o "
                "atendimento é online, uma sessão por semana de 50 minutos, em dia e horário "
                "fixos. Posso fazer seu cadastro?"),
        ("paciente", "pode sim"),
        ("bot", "Me diz seu nome completo e sua data de nascimento?"),
        ("paciente", "Rafael Moreira Dias, 02/09/1994"),
        ("bot", "Anotado! A Thainá entra em contato pra combinar o horário e dizer quem vai "
                "te atender."),
    ]),
    ("06", "[EXEMPLO] Menor de 18 — termo dos pais", "cadastrado", [
        ("paciente", "oi, tenho 16 anos e queria fazer terapia"),
        ("bot", "Oi! Que bom que você procurou. Posso te atender sim. Como você tem menos de "
                "18, vai ser necessário o termo de consentimento dos seus pais ou responsável "
                "— a Thainá te explica como é, é simples. Tudo bem?"),
        ("paciente", "tudo, minha mãe sabe"),
        ("bot", "Ótimo. Vou anotar aqui pra equipe já chegar com o termo preparado."),
    ]),
    ("07", "[EXEMPLO] Objeção de preço — desconto", "cadastrado", [
        ("paciente", "R$ 250 tá fora do meu orçamento agora"),
        ("bot", "Entendo, e obrigada por falar aberto. Consigo fazer R$ 225,00 na mensalidade, "
                "que é o máximo que está na minha mão. Isso ajuda?"),
        ("paciente", "ajuda sim"),
        ("bot", "Então deixo registrado assim. A Thainá confirma com você antes da primeira "
                "sessão."),
    ]),
    ("08", "[EXEMPLO] Terapeuta escrevendo como paciente", "novo", [
        ("paciente", "oi sofia, é a Juliana, terapeuta daqui. meu paciente não apareceu hoje"),
        ("bot", "[ATENÇÃO: número não reconhecido como terapeuta no Hamilton — por isso caiu "
                "no fluxo de paciente. Conserto: conferir o telefone no cadastro dela, ou "
                "marcar a conversa como 'Terapeuta' pelo botão desta tela.]"),
    ]),
]


# Motivo da escalada por exemplo: é o que faz o caso aparecer em "Precisa de
# você agora", onde a Thainá trabalha. Só os que de fato param a Sofia.
ESCALADAS = {
    "01": ("neuro_reuniao", "Avaliação neuropsicológica para o filho de 8 anos; aceitou a reunião com a Amanda."),
    "03": ("crise", "RISCO DE VIDA. Pessoa sozinha, falou em se matar hoje. Atender agora."),
    "08": ("pedido_humano", "Diz ser terapeuta (Juliana) mas o número não está no cadastro do Hamilton."),
}


async def main():
    """Idempotente **sem trocar os ids**: a conversa de cada número é reaproveitada.

    Recriar apagando quebra quem estiver com a tela aberta — o link da fila
    aponta para o id antigo e a página responde "Conversa não encontrada".
    """
    async with async_session() as db:
        for i, (sufixo, nome, estado, falas) in enumerate(EXEMPLOS):
            numero = f"55000000000{sufixo}"
            conversa = (
                await db.execute(select(Conversa).where(Conversa.numero_whatsapp == numero))
            ).scalar_one_or_none()
            if conversa is None:
                conversa = Conversa(numero_whatsapp=numero, criada_em=VELHO)
                db.add(conversa)
                await db.flush()
            conversa.modo = "humano"
            conversa.estado = estado
            conversa.dados_coletados = {"nome_completo": nome, "exemplo": True}
            conversa.seguimento_enviado_em = AGORA
            conversa.contato_conferido_por = "manual"
            conversa.eh_terapeuta = False
            conversa.arquivada_em = None
            conversa.atualizada_em = AGORA - timedelta(minutes=5 * i)

            await db.execute(delete(Mensagem).where(Mensagem.conversa_id == conversa.id))
            await db.execute(delete(Escalada).where(Escalada.conversa_id == conversa.id))
            base = AGORA - timedelta(days=90)
            for j, (quem, texto) in enumerate(falas):
                db.add(Mensagem(
                    conversa_id=conversa.id,
                    direcao="recebida" if quem == "paciente" else "enviada",
                    origem=quem,
                    tipo="texto",
                    texto=texto,
                    criada_em=base + timedelta(minutes=j),
                ))
            if sufixo in ESCALADAS:
                motivo, contexto = ESCALADAS[sufixo]
                db.add(Escalada(conversa_id=conversa.id, motivo=motivo, contexto=contexto,
                                criada_em=AGORA - timedelta(minutes=10 * i)))
            print(f"  {conversa.id}: {nome}")
        await db.commit()
        print(f"{len(EXEMPLOS)} conversas de exemplo em dia")


asyncio.run(main())

# Para apagar todos os exemplos:
#   PYTHONPATH=. venv/bin/python -c "
#   import asyncio
#   from sqlalchemy import text
#   from app.database import engine
#   async def m():
#       async with engine.begin() as c:
#           for t in ('escalada', 'mensagem'):
#               await c.execute(text(f\"DELETE FROM {t} WHERE conversa_id IN \"
#                                    \"(SELECT id FROM conversa WHERE numero_whatsapp LIKE '55000000%')\"))
#           await c.execute(text(\"DELETE FROM conversa WHERE numero_whatsapp LIKE '55000000%'\"))
#   asyncio.run(m())"
