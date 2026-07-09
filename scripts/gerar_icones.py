"""Gera os ícones do PWA: 'S' da Fraunces sobre o gradiente teal do design system.

Os PNGs já estão versionados em `app/static/icons/`. Este script só é necessário
pra REGERAR (mudou a cor, o tamanho, a letra). Não é dependência de runtime.

    pip install fonttools pillow
    curl -sL -o /tmp/Fraunces.ttf \
      "https://github.com/google/fonts/raw/main/ofl/fraunces/Fraunces%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf"
    python -c "from fontTools.varLib.instancer import instantiateVariableFont as i; \
      from fontTools.ttLib import TTFont; \
      i(TTFont('/tmp/Fraunces.ttf'), {'wght':700,'opsz':144,'SOFT':0,'WONK':0}).save('/tmp/Fraunces-700.ttf')"
    python scripts/gerar_icones.py

A Fraunces é variável; instanciamos em wght=700 (o mesmo peso do `.brand .logo`
no allos.css) e opsz=144 (óptico de display).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "app" / "static" / "icons"
FONTE = Path("/tmp/Fraunces-700.ttf")  # ver docstring

# Mesmas cores do .brand .logo em allos.css
TEAL_A = (46, 158, 143)  # #2E9E8F
TEAL_B = (58, 175, 160)  # #3AAFA0


def gradiente(tamanho: int) -> Image.Image:
    """Gradiente diagonal 135deg, como o linear-gradient do CSS."""
    img = Image.new("RGB", (tamanho, tamanho))
    px = img.load()
    for y in range(tamanho):
        for x in range(tamanho):
            t = (x + y) / (2 * tamanho - 2)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(TEAL_A, TEAL_B))
    return img


def cantos_arredondados(img: Image.Image, raio: int) -> Image.Image:
    mascara = Image.new("L", img.size, 0)
    ImageDraw.Draw(mascara).rounded_rectangle([(0, 0), img.size], radius=raio, fill=255)
    saida = Image.new("RGBA", img.size, (0, 0, 0, 0))
    saida.paste(img, (0, 0), mascara)
    return saida


def desenhar_s(img: Image.Image, altura_alvo: float) -> None:
    """Centra o 'S' opticamente pela bbox real do glifo (não pelo avanço)."""
    draw = ImageDraw.Draw(img)
    tamanho = img.size[0]
    # Busca o font-size cujo glifo tem a altura desejada.
    ponto = int(altura_alvo * 1.35)
    for _ in range(24):
        fonte = ImageFont.truetype(str(FONTE), ponto)
        caixa = draw.textbbox((0, 0), "S", font=fonte)
        altura = caixa[3] - caixa[1]
        if abs(altura - altura_alvo) <= 1:
            break
        ponto = max(1, int(ponto * altura_alvo / max(altura, 1)))
    fonte = ImageFont.truetype(str(FONTE), ponto)
    caixa = draw.textbbox((0, 0), "S", font=fonte)
    largura, altura = caixa[2] - caixa[0], caixa[3] - caixa[1]
    x = (tamanho - largura) / 2 - caixa[0]
    y = (tamanho - altura) / 2 - caixa[1]
    draw.text((x, y), "S", font=fonte, fill=(255, 255, 255))


def gerar(tamanho: int, maskable: bool) -> Image.Image:
    fundo = gradiente(tamanho)
    if maskable:
        # Maskable: o Android recorta até 20% de cada borda. O fundo vai até a
        # borda (sem cantos), e o 'S' fica dentro da zona segura (60% central).
        img = fundo.convert("RGBA")
        desenhar_s(img, altura_alvo=tamanho * 0.42)
    else:
        img = cantos_arredondados(fundo, raio=int(tamanho * 0.22))
        desenhar_s(img, altura_alvo=tamanho * 0.58)
    return img


if __name__ == "__main__":
    DESTINO.mkdir(parents=True, exist_ok=True)
    for tam in (192, 512):
        gerar(tam, maskable=False).save(DESTINO / f"sofia-{tam}.png")
        gerar(tam, maskable=True).save(DESTINO / f"sofia-{tam}-maskable.png")
    # Apple: sem transparência e sem cantos (o iOS aplica a máscara dele).
    apple = gradiente(180).convert("RGBA")
    desenhar_s(apple, altura_alvo=180 * 0.55)
    apple.convert("RGB").save(DESTINO / "sofia-apple-180.png")
    # Favicon multi-resolução.
    fav = gerar(64, maskable=False)
    fav.save(DESTINO / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print("gerados:", sorted(p.name for p in DESTINO.iterdir()))
