"""Utilitários pequenos compartilhados."""


def mascarar_telefone(numero) -> str:
    """Mascara um telefone para logs (mostra só os 4 últimos dígitos).

    Ex.: '5531999998888' -> '***8888'. Evita expor PII completa nos logs.
    """
    if not numero:
        return "?"
    s = str(numero)
    if len(s) <= 4:
        return "***"
    return f"***{s[-4:]}"
