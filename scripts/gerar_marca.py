"""Gera os arquivos da marca Allos usados pelo painel (dev-only, não roda em produção).

Fonte: `app/static/marca/allos-grafismo.png` e `allos.png` — recortes do guia de
identidade v2, com o traço raspado da marca guardado no **canal alfa** (RGB é
sólido). Isso permite recolorir sem perder a textura.

O que sai daqui:
- `app/static/marca/*-teal.png` — as versões coloridas que os templates usam.
- `app/static/icons/*` — os ícones do PWA e o favicon, sobre o fundo do painel.

O guia reserva o **grafismo** (o α) para "avatares, ícones, etc." e manda
priorizar o lockup completo onde couber — por isso o ícone do app é o grafismo e
a tela de login usa a marca inteira.

Rodar depois de trocar um asset de origem:  python scripts/gerar_marca.py
"""

import pathlib

from PIL import Image

RAIZ = pathlib.Path(__file__).resolve().parent.parent
MARCA = RAIZ / "app" / "static" / "marca"
ICONES = RAIZ / "app" / "static" / "icons"

TEAL = (0, 136, 136)  # Marrs Green 500 — cor primária do guia
FUNDO = (241, 244, 243)  # mesmo off-white esverdeado do painel


def tingir(origem: pathlib.Path, cor: tuple[int, int, int], largura: int) -> Image.Image:
    """Pinta o alfa da máscara com `cor` e redimensiona pela largura."""
    im = Image.open(origem).convert("RGBA")
    alfa = im.getchannel("A")
    im = Image.new("RGBA", im.size, (*cor, 0))
    im.putalpha(alfa)
    altura = max(1, round(im.height * largura / im.width))
    return im.resize((largura, altura), Image.LANCZOS)


def sobre_fundo(mark: Image.Image, lado: int, ocupacao: float) -> Image.Image:
    """Centraliza a marca num quadrado de fundo claro.

    `ocupacao` é a fração do lado que a marca ocupa: o Android recorta ~20% das
    bordas do ícone maskable, então lá a marca precisa vir menor.
    """
    largura = round(lado * ocupacao)
    m = mark.resize((largura, max(1, round(mark.height * largura / mark.width))), Image.LANCZOS)
    tela = Image.new("RGBA", (lado, lado), (*FUNDO, 255))
    tela.alpha_composite(m, ((lado - m.width) // 2, (lado - m.height) // 2))
    return tela


def main() -> None:
    grafismo = MARCA / "allos-grafismo.png"
    marca = MARCA / "allos.png"

    tingir(grafismo, TEAL, 256).save(MARCA / "allos-grafismo-teal.png")
    tingir(marca, TEAL, 560).save(MARCA / "allos-teal.png")

    base = tingir(grafismo, TEAL, 1024)
    sobre_fundo(base, 192, 0.62).convert("RGB").save(ICONES / "sofia-192.png")
    sobre_fundo(base, 512, 0.62).convert("RGB").save(ICONES / "sofia-512.png")
    sobre_fundo(base, 180, 0.62).convert("RGB").save(ICONES / "sofia-apple-180.png")
    sobre_fundo(base, 192, 0.46).convert("RGB").save(ICONES / "sofia-192-maskable.png")
    sobre_fundo(base, 512, 0.46).convert("RGB").save(ICONES / "sofia-512-maskable.png")
    sobre_fundo(base, 256, 0.62).convert("RGB").save(
        ICONES / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)]
    )
    print("marca e ícones regerados")


if __name__ == "__main__":
    main()
