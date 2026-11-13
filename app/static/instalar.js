/* Botão "Instalar" do painel (Demanda F — docs/demandas/07-APP-INSTALAVEL.md).
 *
 * O navegador avisa quando o site pode ser instalado (`beforeinstallprompt`);
 * só aí o botão aparece. Não há detecção de aparelho: iPhone e Firefox nunca
 * mandam esse aviso, então o botão nunca aparece neles, e com o app já
 * instalado o Chrome também não manda.
 *
 * Carregado no <head>, sem `defer`, de propósito: o aviso pode chegar cedo, e
 * quem registra o listener tarde perde o disparo.
 */
(function () {
  'use strict';

  // O aviso do navegador, guardado até a pessoa tocar no botão. Só serve uma vez.
  var aviso = null;

  function mostrar(sim) {
    document.querySelectorAll('[data-instalar]').forEach(function (botao) {
      botao.hidden = !sim;
    });
  }

  window.addEventListener('beforeinstallprompt', function (evento) {
    // Sem isto o Chrome mostra a faixa dele por conta própria, fora do nosso botão.
    evento.preventDefault();
    aviso = evento;
    mostrar(true);
  });

  // O aviso pode chegar antes de o botão existir na página.
  document.addEventListener('DOMContentLoaded', function () {
    if (aviso) mostrar(true);
  });

  // Delegado no document: a página pode ter o botão do topo ou o do login.
  document.addEventListener('click', function (evento) {
    var botao = evento.target.closest('[data-instalar]');
    if (!botao || !aviso) return;
    var usado = aviso;
    aviso = null;
    // Recusou? O navegador manda um aviso novo no próximo carregamento e o
    // botão volta. Deixá-lo à mostra agora seria um botão que não faz nada.
    mostrar(false);
    usado.prompt();
  });

  window.addEventListener('appinstalled', function () {
    aviso = null;
    mostrar(false);
  });
})();
