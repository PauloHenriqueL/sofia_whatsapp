"""Testes para webhook do WhatsApp"""

import hashlib
import hmac
import json
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app
from app.routers.webhook import verify_signature


class TestWebhookVerification:
    """Testes para GET /webhook/whatsapp (validação Meta)"""

    def test_verify_webhook_success(self):
        """Deve validar webhook quando token está correto"""
        # Use o token do .env
        from app.config import settings
        client = TestClient(app)
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": settings.whatsapp_verify_token,
                "hub.challenge": "test-challenge-123",
            },
        )
        assert response.status_code == 200
        assert "test-challenge-123" in response.text

    def test_verify_webhook_invalid_token(self):
        """Deve rejeitar webhook com token inválido"""
        client = TestClient(app)
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token-absolutely-invalid",
                "hub.challenge": "test-challenge-123",
            },
        )
        assert response.status_code == 403

    def test_verify_webhook_invalid_mode(self):
        """Deve rejeitar webhook com mode inválido"""
        from app.config import settings
        client = TestClient(app)
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "invalid",
                "hub.verify_token": settings.whatsapp_verify_token,
                "hub.challenge": "test-challenge-123",
            },
        )
        assert response.status_code == 403


class TestSignatureVerification:
    """Testes para função verify_signature"""

    def test_valid_signature(self):
        """Deve validar assinatura correta"""
        body = b"test payload"
        secret = "test-secret"

        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        x_hub_signature = f"sha256={signature}"

        # Mock config
        from app import config
        original_secret = config.settings.whatsapp_app_secret
        config.settings.whatsapp_app_secret = secret

        try:
            assert verify_signature(body, x_hub_signature) is True
        finally:
            config.settings.whatsapp_app_secret = original_secret

    def test_invalid_signature(self):
        """Deve rejeitar assinatura inválida"""
        body = b"test payload"
        x_hub_signature = "sha256=invalid_signature"

        assert verify_signature(body, x_hub_signature) is False

    def test_missing_signature(self):
        """Deve rejeitar request sem assinatura"""
        body = b"test payload"
        assert verify_signature(body, "") is False


class TestHealthEndpoint:
    """Testes para health check"""

    def test_health_check(self):
        """Deve retornar 200 OK"""
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestRootEndpoint:
    """Testes para root endpoint"""

    def test_root(self):
        """Deve retornar informações da app"""
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["app"] == "Sofia"
