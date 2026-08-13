"""Relatório (e aplicação) do fim de linha das assinaturas de parcelado.

Sem argumento: **não escreve nada**. Só mostra, assinatura por assinatura, o que
o reconciliador faria — quem é, quantas parcelas já pagou, em que data a cobrança
seria encerrada e quanto ainda seria cobrado indevidamente sem isso.

    python scripts/relatorio_parcelado.py             # simula, não toca em nada
    python scripts/relatorio_parcelado.py --aplicar   # grava no Stripe

Por que a aplicação é manual e não pelo cron nas antigas: as assinaturas que já
estavam rodando foram vendidas com um combinado verbal que o código não conhece.
Se alguma foi renegociada (parcela a mais, desconto), `parcelas_total` mente e o
corte automático encerraria antes da hora. São poucas — conferir uma a uma custa
minutos. Da criação em diante o `/tasks/stripe` cuida sozinho.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import pagamentos, stripe_client  # noqa: E402


def _data(marca: int | None) -> str:
    return datetime.fromtimestamp(marca, timezone.utc).strftime("%d/%m/%Y") if marca else "—"


async def principal(aplicar: bool) -> int:
    if not stripe_client.configurado():
        raise SystemExit("STRIPE_SECRET_KEY vazia.")
    modo = "APLICANDO NO STRIPE" if aplicar else "simulação (nada é escrito)"
    print(f"Assinaturas de parcelado sem fim definido — {modo}\n")

    resultado = await pagamentos.limitar_parcelado(simular=not aplicar)
    planejadas = resultado["planejadas"]

    if not planejadas:
        print("Nada a ajustar: todo parcelado ativo já tem data de encerramento.")
    else:
        cabecalho = f"{'paciente':<32} {'pagas':>7} {'ação':<12} {'encerra em':<12} motivo"
        print(cabecalho)
        print("-" * len(cabecalho))
        for item in planejadas:
            print(
                f"{item['nome'][:32]:<32} "
                f"{item['parcelas_pagas']}/{item['parcelas_total']:<5} "
                f"{item['acao']:<12} {_data(item['quando']):<12} {item['motivo']}"
            )
            if item["excedente"]:
                print(f"{'':<32} ^^ já cobrou {item['excedente']} parcela(s) a mais")
            if aplicar and not item.get("aplicado"):
                print(f"{'':<32} ^^ FALHOU ao gravar no Stripe")

    if resultado["truncado"]:
        print(f"\n⚠️  Teto de {pagamentos.LIMITE_POR_RODADA} por rodada atingido — rode de novo.")
    if resultado["alertas"]:
        print("\n⚠️  Parece neuro mas não diz em quantas parcelas (NÃO tocadas, confira à mão):")
        for alerta in resultado["alertas"]:
            print(f"   {alerta['id']}  {alerta['status']}  {alerta['produto']}")

    print(f"\nJá tinham fim definido: {resultado['ja_limitadas']}")
    if not aplicar and planejadas:
        print("\nNada foi alterado. Para gravar: python scripts/relatorio_parcelado.py --aplicar")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--aplicar", action="store_true", help="grava no Stripe (irreversível)")
    sys.exit(asyncio.run(principal(parser.parse_args().aplicar)))
