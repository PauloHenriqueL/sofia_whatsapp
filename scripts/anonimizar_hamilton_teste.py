"""Anonimiza PII na branch de teste do Hamilton (Neon `sofia-teste`).

Uso (da raiz do repo, que e onde mora o .env):
    python scripts/anonimizar_hamilton_teste.py            # dry-run, nao grava
    python scripts/anonimizar_hamilton_teste.py --apply    # grava

Rodar SEMPRE depois de um "Reset from parent" no Neon: o reset traz de volta os
dados reais de producao, incluindo telefone de paciente. Sem isto, a primeira
execucao do cron de pesquisa manda WhatsApp de verdade pra gente de verdade.

Trava de segurança: só roda se o `neon.timeline_id` for o da branch de teste.
Se alguém apontar a URL pra produção, o script aborta antes de escrever qualquer coisa.

Telefones viram DDD 00 (inexistente no Brasil) para que nenhum envio de WhatsApp
possa chegar a uma pessoa real. Nomes viram pseudônimos estáveis (mesmo id ->
mesmo pseudônimo), pra continuar dando pra debugar "o paciente 412".
Dado clínico (consultas, avaliações, ORS, altas) NÃO é tocado.
"""

import sys

import psycopg2

TIMELINE_TESTE = "d816d0c21c1ca258fce8a80e07b14626"

# (tabela, pk, [(coluna, expressão SQL)])
ALVOS = [
    (
        "pacientes",
        "pk_paciente",
        [
            ("nome", "'Paciente ' || lpad({pk}::text, 4, '0')"),
            ("telefone", "'5500' || lpad({pk}::text, 9, '0')"),
            ("email", "'paciente' || {pk} || '@exemplo.invalid'"),
            ("cpf", "lpad({pk}::text, 11, '0')"),
            ("endereco", "'Rua de Teste, 100'"),
            ("cep", "'00000000'"),
            ("contato_apoio", "'Contato ' || lpad({pk}::text, 4, '0')"),
        ],
    ),
    (
        "associados",
        "pk_associado",
        [
            ("nome", "'Associado ' || lpad({pk}::text, 4, '0')"),
            ("telefone", "'5500' || lpad({pk}::text, 9, '0')"),
            ("email", "'associado' || {pk} || '@exemplo.invalid'"),
            ("cpf", "lpad({pk}::text, 11, '0')"),
            ("endereco", "'Rua de Teste, 100'"),
            ("contato_apoio", "'Contato ' || lpad({pk}::text, 4, '0')"),
        ],
    ),
    (
        "auth_user",
        "id",
        [
            # username NÃO é tocado: é o que permite logar no Hamilton pra testar.
            ("first_name", "'Usuario'"),
            ("last_name", "lpad({pk}::text, 4, '0')"),
            ("email", "'usuario' || {pk} || '@exemplo.invalid'"),
        ],
    ),
    (
        "plantao_horarios",
        "id",
        [
            ("nome_paciente", "'Paciente plantao ' || lpad({pk}::text, 4, '0')"),
            ("telefone", "'5500' || lpad({pk}::text, 9, '0')"),
        ],
    ),
    (
        "plantao_medico_pacientes",
        "id",
        [
            ("nome_completo", "'Paciente plantao ' || lpad({pk}::text, 4, '0')"),
            ("nome_social", "'Paciente plantao ' || lpad({pk}::text, 4, '0')"),
            ("celular", "'5500' || lpad({pk}::text, 9, '0')"),
            ("email", "'plantao' || {pk} || '@exemplo.invalid'"),
            ("cpf", "lpad({pk}::text, 11, '0')"),
            ("logradouro", "'Rua de Teste, 100'"),
            ("cep", "'00000000'"),
        ],
    ),
    ("pagamento", "id", [("nome", "'Pagador ' || lpad({pk}::text, 4, '0')")]),
    ("selecao", "pk_selecao", [("nome", "'Candidato ' || lpad(pk_selecao::text, 4, '0')")]),
    (
        "backup_vlr_sessao_20260504_174513",
        None,
        [("nome", "'Paciente backup'")],
    ),
]


def main(url: str, dry_run: bool) -> int:
    conn = psycopg2.connect(url)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("show neon.timeline_id")
    timeline = cur.fetchone()[0]
    cur.execute("select current_database()")
    banco = cur.fetchone()[0]
    print(f"banco    = {banco}")
    print(f"timeline = {timeline}")

    if timeline != TIMELINE_TESTE:
        print(f"\nABORTADO: timeline nao e a da branch de teste ({TIMELINE_TESTE}).")
        return 1

    cur.execute(
        "select table_name, column_name from information_schema.columns where table_schema='public'"
    )
    existentes = {(t, c) for t, c in cur.fetchall()}

    print()
    total = 0
    for tabela, _pk, colunas in ALVOS:
        cols = [(c, expr) for c, expr in colunas if (tabela, c) in existentes]
        if not cols:
            print(f"  {tabela:.<36} (tabela/colunas ausentes, pulado)")
            continue
        cur.execute(
            """select a.attname from pg_index i
              join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
              where i.indrelid = %s::regclass and i.indisprimary""",
            (tabela,),
        )
        achado = cur.fetchone()
        precisa_pk = any("{pk}" in expr for _c, expr in cols)
        if not achado and precisa_pk:
            print(f"  {tabela:.<36} (sem chave primaria, pulado)")
            continue
        pk = achado[0] if achado else ""
        sets = ", ".join(f"{c} = {expr.format(pk=pk)}" for c, expr in cols)
        cur.execute(f"update {tabela} set {sets}")  # noqa: S608 - nomes vêm de constante
        print(f"  {tabela:.<36} {cur.rowcount:>5} linhas  ({len(cols)} colunas)")
        total += cur.rowcount

    if dry_run:
        conn.rollback()
        print(f"\nDRY-RUN: {total} linhas seriam alteradas. Nada foi gravado.")
    else:
        conn.commit()
        print(f"\nOK: {total} linhas anonimizadas e commitadas.")

    conn.close()
    return 0


if __name__ == "__main__":
    linha = next(
        line
        for line in open(".env", encoding="utf-8")
        if line.startswith("DATABASE_HAMILTON_TESTE")
    )
    url = linha.split("=", 1)[1].strip().strip('"').replace("&channel_binding=require", "")
    sys.exit(main(url, dry_run="--apply" not in sys.argv))
