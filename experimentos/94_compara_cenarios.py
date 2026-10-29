#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
94_compara_cenarios.py — compara DOIS cenários (linha de base × TE) a partir dos
metricas.json das repetições válidas: média ± dp, mín–máx, ganho relativo e teste
de permutação exato da diferença de médias. Só stdlib.

Uso: python3 experimentos/94_compara_cenarios.py <pasta_base> <pasta_te>
         [--rotulo-a TEXTO] [--rotulo-b TEXTO] [--saida ARQ.md] [--csv ARQ.csv]

Padrão de saída: relatorios/<DATA_EXP ou hoje>/COMPARACAO-CENARIOS.md (+ .csv).
Se alguma repetição foi marcada por travada da VM (lag.txt), a tabela é repetida
sem elas; nada é descartado em silêncio.

Ganho: para métricas em que menos é melhor (perdas, RTT, exposição), (A − B)/A em %;
para vazão, (B − A)/A em %. Com base A = 0 o ganho percentual não é definido (n/d).
"""
import argparse
import csv
import datetime
import itertools
import math
import os
import random
import re
import statistics as st
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import exp_apoio as ea

# (nome, unidade, menos_e_melhor, extrator)
METRICAS = [
    ("Perda total (fluxo TE + fundo)", "pkts", True, lambda m: m["perdas"]["total_perd"]),
    ("Perda total (fluxo TE + fundo)", "%", True, lambda m: m["perdas"]["total_perd_pct"]),
    ("Perda do fluxo TE", "pkts", True, lambda m: m["perdas"]["te_perd"]),
    ("Perda do fluxo TE na carga (iperf)", "%", True, lambda m: m["fases"]["carga"]["perda_pct"]),
    ("Vazão do fluxo TE na carga", "Mbit/s", False, lambda m: m["fases"]["carga"]["vazao_mbps"]),
    ("RTT mediano na carga", "ms", True, lambda m: m["fases"]["carga"]["rtt_ms_med"]),
    ("Jitter na carga", "ms", True, lambda m: m["fases"]["carga"]["jitter_ms"]),
    ("Tempo no nó saturado", "s", True, lambda m: m.get("exposicao_s")),
    ("Descartes de fila (tc) na carga", "pkts", True, lambda m: m["perdas"]["tc"].get("carga", {}).get("total")),
    ("Trocas de rota", "n", True, lambda m: m["controle"]["n_trocas"]),
]
MAX_COMBINACOES = 200000


def valores(metricas, extrator):
    out = []
    for m in metricas:
        try:
            v = extrator(m)
        except (KeyError, TypeError):
            v = None
        if v is not None:
            out.append(float(v))
    return out


def p_permutacao(a, b, semente=20261019):
    """p bilateral exato (ou Monte Carlo com semente fixa se C(n,k) > MAX) para a
    diferença de médias; devolve (p, exato, p_minimo_possivel)."""
    n, k = len(a) + len(b), len(a)
    tudo = a + b
    obs = abs(st.mean(a) - st.mean(b))
    total = math.comb(n, k)
    # com grupos de tamanhos diferentes a rotulagem espelhada não existe: o menor p é 1/C(n,k)
    pmin = (2.0 if len(a) == len(b) else 1.0) / total if total > 1 else 1.0
    extremos = cont = 0
    if total <= MAX_COMBINACOES:
        soma = sum(tudo)
        for idx in itertools.combinations(range(n), k):
            sa = sum(tudo[i] for i in idx)
            d = abs(sa / k - (soma - sa) / (n - k))
            extremos += d >= obs - 1e-12
            cont += 1
        return extremos / cont, True, pmin
    rng = random.Random(semente)
    for _ in range(MAX_COMBINACOES):
        idx = set(rng.sample(range(n), k))
        da = [tudo[i] for i in idx]
        db = [tudo[i] for i in range(n) if i not in idx]
        extremos += abs(st.mean(da) - st.mean(db)) >= obs - 1e-12
    return (extremos + 1) / (MAX_COMBINACOES + 1), False, pmin


def resumo(v):
    if not v:
        return None
    return {"n": len(v), "media": st.mean(v), "dp": st.stdev(v) if len(v) > 1 else 0.0,
            "min": min(v), "max": max(v)}


def fmt(r, casas=2):
    if r is None:
        return "n/d"
    return "%.*f ± %.*f (%.*f–%.*f)" % (casas, r["media"], casas, r["dp"], casas, r["min"], casas, r["max"])


def ganho(a, b, menos_e_melhor):
    if a["media"] == 0:
        return None
    g = (a["media"] - b["media"]) / a["media"] * 100.0
    return g if menos_e_melhor else -g


def carrega(pasta):
    reps = ea.metricas_do_cenario(pasta)
    val = [(d, m) for d, m in reps if m and m.get("valida")]
    inval = [d for d, m in reps if not (m and m.get("valida"))]
    return val, inval


def meta_env(pasta):
    """Linha 'env:' e 'nproc' do meta.txt da primeira repetição (rastreabilidade)."""
    for d in sorted(os.listdir(pasta)):
        mt = os.path.join(pasta, d, "meta.txt")
        if d.startswith("rep") and os.path.exists(mt):
            txt = open(mt, errors="replace").read()
            env = re.search(r"^env: (.*)$", txt, re.M)
            nproc = re.search(r"nproc: (\d+)", txt)
            return (env.group(1).strip() if env else "?"), (nproc.group(1) if nproc else "?")
    return "?", "?"


def tabela(rot_a, rot_b, ma, mb, linhas_csv, secao):
    out = ["| Métrica | %s | %s | Ganho | p (permutação) |" % (rot_a, rot_b), "|---|---|---|---|---|"]
    pmin = None
    for nome, un, menos, ext in METRICAS:
        va, vb = valores(ma, ext), valores(mb, ext)
        ra, rb = resumo(va), resumo(vb)
        if ra is None or rb is None:
            out.append("| %s (%s) | %s | %s | n/d | n/d |" % (nome, un, fmt(ra), fmt(rb)))
            continue
        g = ganho(ra, rb, menos)
        p, exato, pm = p_permutacao(va, vb)
        pmin = pm
        gtxt = "n/d (base 0)" if g is None else "%+.1f%%" % g
        ptxt = "%.4f%s" % (p, "" if exato else " (MC)")
        out.append("| %s (%s) | %s | %s | %s | %s |" % (nome, un, fmt(ra), fmt(rb), gtxt, ptxt))
        linhas_csv.append([secao, nome, un, ra["n"], ra["media"], ra["dp"], ra["min"], ra["max"],
                           rb["n"], rb["media"], rb["dp"], rb["min"], rb["max"],
                           "" if g is None else g, p])
    nota = ("Células: média ± dp (mín–máx). Ganho: redução relativa para métricas em que menos é "
            "melhor, aumento relativo para vazão. p: teste de permutação bilateral da diferença de médias")
    if pmin is not None:
        nota += "; com n = %d × %d o menor p possível é %.4f" % (len(ma), len(mb), pmin)
    return "\n".join(out) + "\n\n" + nota + ".\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base")
    ap.add_argument("te")
    ap.add_argument("--rotulo-a", default="IPv6 + OSPF (métrica estática)")
    ap.add_argument("--rotulo-b", default="SRv6 + TE + SDN")
    ap.add_argument("--saida")
    ap.add_argument("--csv")
    a = ap.parse_args()

    data = os.environ.get("DATA_EXP") or datetime.date.today().isoformat()
    saida = a.saida or os.path.join(AQUI, "..", "relatorios", data, "COMPARACAO-CENARIOS.md")
    csv_path = a.csv or os.path.splitext(saida)[0] + ".csv"

    va, inva = carrega(a.base)
    vb, invb = carrega(a.te)
    if not va or not vb:
        sys.exit("sem repetições válidas em %s ou %s" % (a.base if not va else "-", a.te if not vb else "-"))

    def marcada(m):
        return bool((m.get("lag") or {}).get("travada_vm"))

    linhas_csv = [["secao", "metrica", "unidade", "n_a", "media_a", "dp_a", "min_a", "max_a",
                   "n_b", "media_b", "dp_b", "min_b", "max_b", "ganho_pct", "p"]]
    md = ["# Comparação: %s × %s\n" % (a.rotulo_a, a.rotulo_b),
          "Gerado por `experimentos/94_compara_cenarios.py` em %s." % datetime.date.today().isoformat(),
          "", "- A (linha de base): `%s` — %d repetições válidas, inválidas: %s" % (
              os.path.abspath(a.base), len(va), inva or "nenhuma"),
          "- B (TE): `%s` — %d repetições válidas, inválidas: %s" % (
              os.path.abspath(a.te), len(vb), invb or "nenhuma")]
    for rot, pasta in (("A", a.base), ("B", a.te)):
        env, nproc = meta_env(pasta)
        md.append("- Parâmetros de %s (meta.txt): nproc=%s; %s" % (rot, nproc, env))
    md += ["", "## Todas as repetições válidas\n", tabela(a.rotulo_a, a.rotulo_b, [m for _, m in va],
                                                          [m for _, m in vb], linhas_csv, "todas")]
    marc_a = [d for d, m in va if marcada(m)]
    marc_b = [d for d, m in vb if marcada(m)]
    md.append("Repetições marcadas por travada da VM (atraso de sleep > %d ms na carga): A %s; B %s.\n" % (
        ea.LIMIAR_TRAVADA_MS, marc_a or "nenhuma", marc_b or "nenhuma"))
    if marc_a or marc_b:
        la = [m for _, m in va if not marcada(m)]
        lb = [m for _, m in vb if not marcada(m)]
        if la and lb:
            md += ["## Sem as repetições marcadas\n", tabela(a.rotulo_a, a.rotulo_b, la, lb, linhas_csv, "sem_travadas")]
        else:
            md.append("Sem as marcadas, um dos cenários ficaria sem repetições; tabela omitida.\n")
    md += ["## Como ler\n",
           "- O ganho mede o efeito CONJUNTO de TE + SDN + SRv6 contra o IPv6 com métrica estática; "
           "este desenho não separa quanto é do TE e quanto é do plano de dados SRv6.",
           "- Com poucas repetições o p é discreto; o texto deve citar n, a faixa mín–máx e as "
           "ressalvas (dois caminhos, banda escalada, VM com travadas, fundo UDP sintético)."]

    os.makedirs(os.path.dirname(os.path.abspath(saida)), exist_ok=True)
    with open(saida, "w") as fp:
        fp.write("\n".join(md) + "\n")
    with open(csv_path, "w", newline="") as fp:
        csv.writer(fp).writerows(linhas_csv)
    print("\n".join(md))
    print("\nescrito: %s e %s" % (saida, csv_path))


if __name__ == "__main__":
    main()
