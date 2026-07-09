/* Ordenação client-side de tabelas do painel.
 *
 * Marque a coluna com `<th data-sort>` (texto) ou `<th data-sort="num">`
 * (número: pega o primeiro número da célula, então "12 dias" ordena por 12).
 * Clicar alterna asc/desc. Serve pras tabelas pequenas e já carregadas
 * (acompanhamento); a lista de conversas ordena no servidor, porque é paginada.
 *
 * Linhas com `colspan` (o "nenhum resultado") ficam onde estão.
 */
(function () {
  function valor(celula, tipo) {
    const txt = (celula?.innerText || '').trim();
    if (tipo !== 'num') return txt.toLowerCase();
    const m = txt.replace(/\./g, '').match(/-?\d+([,.]\d+)?/);
    return m ? parseFloat(m[0].replace(',', '.')) : -Infinity;
  }

  function ordenar(tabela, indice, tipo, asc) {
    const corpo = tabela.tBodies[0];
    const linhas = Array.from(corpo.rows).filter((r) => !r.querySelector('[colspan]'));
    linhas.sort((a, b) => {
      const x = valor(a.cells[indice], tipo);
      const y = valor(b.cells[indice], tipo);
      if (x < y) return asc ? -1 : 1;
      if (x > y) return asc ? 1 : -1;
      return 0;
    });
    linhas.forEach((l) => corpo.appendChild(l));
  }

  document.querySelectorAll('table').forEach((tabela) => {
    tabela.querySelectorAll('th[data-sort]').forEach((th, _, todos) => {
      const indice = th.cellIndex;
      const tipo = th.dataset.sort;
      th.classList.add('th-sort-js');
      th.innerHTML += ' <i class="bi bi-arrow-down-up sort-idle"></i>';

      th.addEventListener('click', () => {
        const asc = th.dataset.dir !== 'asc';
        todos.forEach((outro) => {
          if (outro !== th) delete outro.dataset.dir;
        });
        th.dataset.dir = asc ? 'asc' : 'desc';
        ordenar(tabela, indice, tipo, asc);
      });
    });
  });
})();
