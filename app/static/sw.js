/* Service worker do painel da Sofia.
 *
 * Escopo deliberadamente pequeno: só existe pra o navegador aceitar instalar o
 * PWA (ícone na tela inicial) e pra os arquivos estáticos carregarem rápido.
 *
 * NÃO cacheia nada do /painel/, /api/ ou /login:
 * - são dados de saúde de paciente (LGPD): não podem ficar no disco do celular;
 * - a sessão expira, e servir uma página do cache mostraria conteúdo de uma
 *   sessão encerrada;
 * - o painel já se atualiza sozinho via HTMX (poll), então cache atrapalha.
 *
 * Estratégia: network-first pra tudo, com cache só como acervo dos estáticos
 * (para o app abrir offline mostrando algo, em vez do dinossauro).
 */
const VERSAO = 'sofia-v1';
const SHELL = [
  '/static/allos.css',
  '/static/ordenar-tabela.js',
  '/static/icons/sofia-192.png',
  '/static/icons/sofia-512.png',
];

self.addEventListener('install', (evento) => {
  evento.waitUntil(
    caches.open(VERSAO).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (evento) => {
  // Remove caches de versões antigas do SW.
  evento.waitUntil(
    caches
      .keys()
      .then((chaves) => Promise.all(chaves.filter((c) => c !== VERSAO).map((c) => caches.delete(c))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (evento) => {
  const req = evento.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Só o estático passa pelo cache. Todo o resto vai direto à rede, sempre.
  const ehEstatico = url.pathname.startsWith('/static/');
  if (!ehEstatico) return;

  // Estático: rede primeiro (pra pegar deploy novo), cache como reserva.
  evento.respondWith(
    fetch(req)
      .then((resposta) => {
        if (resposta && resposta.ok) {
          const copia = resposta.clone();
          caches.open(VERSAO).then((cache) => cache.put(req, copia));
        }
        return resposta;
      })
      .catch(() => caches.match(req))
  );
});
