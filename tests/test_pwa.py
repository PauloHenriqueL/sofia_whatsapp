"""PWA: manifest, service worker, ícones (P6).

O teste que mais importa aqui é o do escopo do service worker. Ele é um cache
que roda no celular da Thainá; se alguém "melhorar" o `sw.js` pra cachear o
painel, dado de saúde de paciente passa a ficar no disco do aparelho, sobrevive
ao logout e é servido depois da sessão expirar. Isso não pode regredir em
silêncio.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

ESTATICO = Path(__file__).resolve().parent.parent / "app" / "static"
SW = ESTATICO / "sw.js"


@pytest.fixture
def client():
    return TestClient(app)


class TestRotasNaRaiz:
    """`sw.js` e `manifest` têm que sair da raiz, sem login."""

    def test_service_worker_na_raiz(self, client):
        r = client.get("/sw.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]

    def test_service_worker_allowed_root(self, client):
        """Sem esse header o SW não controla /painel/ e o app não instala."""
        assert client.get("/sw.js").headers["service-worker-allowed"] == "/"

    def test_service_worker_nao_e_cacheado_pelo_navegador(self, client):
        """Senão um SW velho fica preso no aparelho depois de um deploy."""
        assert "no-cache" in client.get("/sw.js").headers["cache-control"]

    def test_manifest_na_raiz(self, client):
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/manifest+json")

    def test_favicon(self, client):
        assert client.get("/favicon.ico").status_code == 200

    def test_nao_exigem_login(self, client):
        """São públicos de propósito: não expõem dado e o SW precisa ser acessível."""
        for url in ("/sw.js", "/manifest.webmanifest", "/favicon.ico"):
            assert client.get(url).status_code == 200, url


class TestManifest:
    """Requisitos de instalabilidade (MDN): name, ícones 192+512, start_url, display."""

    @pytest.fixture
    def manifest(self, client):
        return json.loads(client.get("/manifest.webmanifest").text)

    def test_campos_obrigatorios(self, manifest):
        assert manifest["name"]
        assert manifest["short_name"]
        assert manifest["start_url"] == "/painel/"
        assert manifest["display"] == "standalone"

    def test_tem_icone_192_e_512(self, manifest):
        tamanhos = {i["sizes"] for i in manifest["icons"]}
        assert "192x192" in tamanhos and "512x512" in tamanhos

    def test_tem_icones_maskable(self, manifest):
        """Sem `maskable`, o Android corta as serifas do S ao aplicar a máscara."""
        maskable = [i for i in manifest["icons"] if i["purpose"] == "maskable"]
        assert {i["sizes"] for i in maskable} == {"192x192", "512x512"}

    def test_nao_forca_orientacao(self, manifest):
        """A Thainá também usa o painel no PC, em paisagem."""
        assert "orientation" not in manifest

    def test_todos_os_icones_existem_e_sao_servidos(self, manifest, client):
        for icone in manifest["icons"]:
            r = client.get(icone["src"])
            assert r.status_code == 200, icone["src"]
            assert r.headers["content-type"] == "image/png"

    def test_theme_color_e_a_do_design_system(self, manifest):
        assert manifest["theme_color"] == "#008888"  # --teal-500 (Marrs Green do guia v2)


class TestServiceWorkerNaoCacheiaDadoDePaciente:
    """LGPD: o SW roda no celular. Nada de paciente pode entrar no cache dele."""

    @pytest.fixture
    def codigo(self):
        return SW.read_text()

    def _sem_comentarios(self, codigo: str) -> str:
        linhas = []
        for linha in codigo.splitlines():
            nua = linha.strip()
            if nua.startswith(("//", "*", "/*")):
                continue
            linhas.append(linha)
        return "\n".join(linhas)

    def test_so_intercepta_static(self, codigo):
        ativo = self._sem_comentarios(codigo)
        assert "startsWith('/static/')" in ativo

    def test_nao_menciona_rotas_sensiveis_no_codigo_ativo(self, codigo):
        """`/painel`, `/api` e `/login` só podem aparecer em comentário."""
        ativo = self._sem_comentarios(codigo)
        for rota in ("/painel", "/api/", "/login"):
            assert rota not in ativo, f"{rota} apareceu no código ativo do sw.js"

    def test_o_shell_pre_cacheado_e_so_estatico(self, codigo):
        """Nada fora de /static/ pode entrar no cache de instalação."""
        import re

        bloco = re.search(r"const SHELL = \[(.*?)\]", codigo, re.S)
        assert bloco, "não achei a lista SHELL no sw.js"
        urls = re.findall(r"'([^']+)'", bloco.group(1))
        assert urls, "SHELL vazio"
        for url in urls:
            assert url.startswith("/static/"), f"{url} não é estático"

    def test_ignora_requests_que_nao_sao_get(self, codigo):
        """POST (responder, assumir, upload) nunca pode ser servido do cache."""
        assert "req.method !== 'GET'" in codigo

    def test_ignora_outras_origens(self, codigo):
        assert "url.origin !== self.location.origin" in codigo

    def test_limpa_caches_de_versoes_antigas(self, codigo):
        assert "caches.delete" in codigo


class TestBaseHtmlLigaOPwa:
    """Sem o link do manifest e o registro do SW, nada disso é usado."""

    @pytest.fixture
    def html(self, client):
        # /login é a única página que renderiza base.html sem exigir sessão.
        return client.get("/login").text

    def test_linka_o_manifest(self, html):
        assert 'rel="manifest"' in html and "/manifest.webmanifest" in html

    def test_registra_o_service_worker_da_raiz(self, html):
        assert "serviceWorker.register('/sw.js')" in html

    def test_apple_touch_icon_e_theme_color(self, html):
        assert 'rel="apple-touch-icon"' in html
        assert 'name="theme-color"' in html

    def test_viewport_cobre_o_notch(self, html):
        assert "viewport-fit=cover" in html


class TestBotaoInstalar:
    """Demanda F: o botão que abre a instalação (docs/demandas/07-APP-INSTALAVEL.md).

    O que não pode regredir é o `hidden`: o botão só faz alguma coisa depois
    que o navegador avisa que o site é instalável. Se ele nascer à mostra,
    aparece no iPhone e no Firefox, onde tocar nele não faz nada.
    """

    TEMPLATES = ESTATICO.parent / "templates"

    @pytest.fixture
    def login(self, client):
        return client.get("/login").text

    def _botao(self, html: str) -> str:
        import re

        achado = re.search(r"<button[^>]*data-instalar[^>]*>", html)
        assert achado, "não achei o botão [data-instalar]"
        return achado.group(0)

    def test_login_tem_o_botao_e_ele_nasce_escondido(self, login):
        assert " hidden" in self._botao(login)

    def test_botao_do_login_nao_envia_o_formulario(self, login):
        """`type="button"` e fora do <form>: instalar não pode tentar logar."""
        assert 'type="button"' in self._botao(login)
        formulario = login[login.index("<form") : login.index("</form>")]
        assert "data-instalar" not in formulario

    def test_topo_tem_o_botao_e_ele_nasce_escondido(self):
        botao = self._botao((self.TEMPLATES / "_topbar.html").read_text())
        assert " hidden" in botao and 'type="button"' in botao

    def test_botao_do_topo_escapa_das_regras_que_escondem_no_celular(self):
        """Até 760px o CSS esconde `.topbar-right .muted` e `.topbar-right form`.

        O botão é pro celular: se ganhar a classe `muted` ou for parar dentro
        do form do "Sair", some justamente onde interessa.
        """
        import re

        # Sem os comentários do Jinja: eles citam "<form>" ao explicar a regra.
        topo = re.sub(r"\{#.*?#\}", "", (self.TEMPLATES / "_topbar.html").read_text(), flags=re.S)
        assert "muted" not in self._botao(topo)
        inicio = topo.index('class="topbar-right"')
        trecho = topo[inicio : topo.index("data-instalar")]
        assert "<form" not in trecho

    def test_css_garante_que_hidden_esconde(self):
        """`.btn { display:inline-flex }` vence o `hidden` do navegador."""
        css = (ESTATICO / "allos.css").read_text()
        assert "[data-instalar][hidden] { display:none !important; }" in css

    def test_script_e_servido_e_carregado_sem_defer(self, client, login):
        assert client.get("/static/instalar.js").status_code == 200
        import re

        tag = re.search(r"<script[^>]*instalar\.js[^>]*>", login)
        assert tag, "base.html não carrega o instalar.js"
        assert "defer" not in tag.group(0) and "async" not in tag.group(0)

    def test_script_espera_o_aviso_do_navegador(self):
        """Sem detecção de aparelho: quem decide é o `beforeinstallprompt`."""
        codigo = (ESTATICO / "instalar.js").read_text()
        assert "beforeinstallprompt" in codigo and "appinstalled" in codigo
        assert "userAgent" not in codigo

    def test_manifest_tem_id_estavel(self, client):
        """Sem `id`, mudar o `start_url` faria quem já instalou virar "outro app"."""
        assert json.loads(client.get("/manifest.webmanifest").text)["id"] == "/painel/"
