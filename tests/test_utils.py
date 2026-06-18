"""Testes dos utilitários."""

from app.utils import mascarar_telefone


def test_mascara_mostra_4_ultimos():
    assert mascarar_telefone("5531999998888") == "***8888"


def test_mascara_numero_curto():
    assert mascarar_telefone("12") == "***"


def test_mascara_none():
    assert mascarar_telefone(None) == "?"
