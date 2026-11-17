"""Conversas de EXEMPLO no painel da Sofia, para a Thainá ver os casos que aparecem.

Seguras de propósito:
- número fictício (5500000000NN): nenhuma mensagem alcança gente de verdade;
- `modo='humano'`: a Sofia não conduz, não responde e não escala;
- `seguimento_enviado_em` preenchido: o follow-up automático nunca dispara;
- `contato_conferido_por='manual'`: a conferência do cron não pergunta ao Hamilton;
- `criada_em` 90 dias atrás: ficam fora do "pessoas novas nos 7 dias";
- sem `Escalada`: não entram na fila "Precisa de você agora".
Apagar é um DELETE por `numero_whatsapp LIKE '55000000%'`.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from app.database import async_session
from app.models import Conversa, Mensagem

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
    ("02", "[EXEMPLO] Criança de 9 anos — menor de 11", "escalado", [
        ("paciente", "Boa tarde, queria terapia para minha filha de 9 anos"),
        ("bot", "Boa tarde! Que bom que procurou. Me conta um pouquinho: o que vocês estão "
                "buscando pra ela?"),
        ("paciente", "Ela anda muito ansiosa desde que mudou de escola"),
        ("bot", "Entendo. Preciso te falar uma coisa com franqueza: nessa idade o atendimento "
                "online costuma não funcionar tão bem — criança pequena precisa de outro tipo "
                "de contato. A Allos tem atendimento presencial na sede, com poucas vagas. Vou "
                "pedir pra Thainá olhar o caso dela e ver a melhor opção, pode ser?"),
        ("paciente", "Pode, obrigada"),
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
    ("05", "[EXEMPLO] Profectum — origem não marcada como parceria", "escalado", [
        ("paciente", "Boa noite, me indicaram vocês pelo Profectum"),
        ("bot", "Boa noite! Sou a Sofia, da Allos. Me conta o que você está buscando?"),
        ("paciente", "Terapia pra mim. Pelo Profectum é gratuito, né?"),
        ("bot", "[ATENÇÃO — ponto a decidir: hoje o Profectum está cadastrado como origem, mas "
                "NÃO está marcado como parceria no Hamilton. Do jeito que está, a Sofia trata "
                "como paciente comum e cobra R$ 200,00. Se for parceria, marcar no admin.]"),
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
        ("paciente", "R$ 200 tá fora do meu orçamento agora"),
        ("bot", "Entendo, e obrigada por falar aberto. Consigo fazer R$ 180,00 na mensalidade, "
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


async def main():
    async with async_session() as db:
        existentes = (await db.execute(
            select(Conversa).where(Conversa.numero_whatsapp.like("55000000%"))
        )).scalars().all()
        if existentes:
            ids = [c.id for c in existentes]
            await db.execute(delete(Mensagem).where(Mensagem.conversa_id.in_(ids)))
            await db.execute(delete(Conversa).where(Conversa.id.in_(ids)))
            print(f"removidos {len(ids)} exemplos antigos")

        for i, (sufixo, nome, estado, falas) in enumerate(EXEMPLOS):
            conversa = Conversa(
                numero_whatsapp=f"55000000000{sufixo}",
                modo="humano",
                estado=estado,
                dados_coletados={"nome_completo": nome, "exemplo": True},
                seguimento_enviado_em=AGORA,
                contato_conferido_por="manual",
                eh_terapeuta=False,
                criada_em=VELHO,
                atualizada_em=AGORA - timedelta(minutes=5 * i),
            )
            db.add(conversa)
            await db.flush()
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
            print(f"  {conversa.id}: {nome}")
        await db.commit()
        print(f"{len(EXEMPLOS)} conversas de exemplo criadas")


asyncio.run(main())

# Para apagar todos os exemplos:
#   PYTHONPATH=. venv/bin/python -c "
#   import asyncio
#   from sqlalchemy import text
#   from app.database import engine
#   async def m():
#       async with engine.begin() as c:
#           await c.execute(text(\"DELETE FROM mensagem WHERE conversa_id IN \"
#                                \"(SELECT id FROM conversa WHERE numero_whatsapp LIKE '55000000%')\"))
#           await c.execute(text(\"DELETE FROM conversa WHERE numero_whatsapp LIKE '55000000%'\"))
#   asyncio.run(m())"
