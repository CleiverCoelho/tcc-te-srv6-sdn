#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
92_analisa_u0.py — análise do U0 (calibração do sensor de utilização, v2).

Uso: python3 experimentos/92_analisa_u0.py relatorios/<data>/experimentos/U0_calibracao

Para cada (repetição, plano, taxa):
  medido   = média do util_r1 do timeline na janela estável do fluxo
  esperado = pps do iperf3 × tamanho de quadro modal do pcap (L2) × 8 ÷ banda
  erro %   = (medido − esperado) / esperado
Critérios (plano v2): |erro| ≤ 5 % em toda taxa; razão util(srv6)/util(ipv6)
dentro de ±1 ponto percentual da razão dos quadros medidos no pcap.
"""
import collections
import glob
import json
import os
import statistics as st
import struct
import sys

BANDA_MBIT = 10.0
TOL_ERRO_PCT = 5.0
TOL_RAZAO_PP = 1.0
SKIP_INI_S, SKIP_FIM_S = 4.0, 1.0      # fluxo começa ~2 s após INI_*; descarta rampa


def tamanhos_pcap(caminho):
    d = open(caminho, "rb").read()
    if len(d) < 24:
        return []
    le = d[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
    e = "<" if le else ">"
    pos, out = 24, []
    while pos + 16 <= len(d):
        _, _, incl, orig = struct.unpack(e + "IIII", d[pos:pos + 16])
        out.append(orig)
        pos += 16 + incl
    return out


def quadro_modal(tamanhos):
    """Tamanho de quadro mais frequente entre os quadros de dados (> 500 B)."""
    grandes = [t for t in tamanhos if t > 500]
    return collections.Counter(grandes).most_common(1)[0][0] if grandes else None


def marcadores(rep):
    m = {}
    for ln in open(os.path.join(rep, "marcadores.txt")):
        p = ln.split()
        if len(p) == 2:
            m[p[1]] = float(p[0])
    return m


def timeline(rep):
    with open(os.path.join(rep, "timeline.tsv")) as fp:
        cab = fp.readline().rstrip("\n").split("\t")
        for ln in fp:
            v = ln.rstrip("\n").split("\t")
            if len(v) == len(cab):
                d = dict(zip(cab, v))
                try:
                    yield float(d["t_epoch"]), (None if d["util_r1"] == "NA" else float(d["util_r1"]))
                except (KeyError, ValueError):
                    continue


def analisa_rep(rep):
    mk = marcadores(rep)
    pts = list(timeline(rep))
    res = []
    for k in sorted(x for x in mk if x.startswith("INI_")):
        tag = k[4:]
        plano, taxa = tag.split("_", 1)
        cli = os.path.join(rep, "iperf_cli_%s.json" % tag)
        pcap = os.path.join(rep, "%s.pcap" % tag)
        if not (os.path.exists(cli) and os.path.exists(pcap)):
            continue
        try:
            j = json.load(open(cli))
            soma = j["end"]["sum"]
            pps = soma["packets"] / soma["seconds"]
            dur = soma["seconds"]
        except (ValueError, KeyError):
            continue
        quadro = quadro_modal(tamanhos_pcap(pcap))
        a, b = mk[k] + SKIP_INI_S, mk[k] + 2.0 + dur - SKIP_FIM_S
        v = [u for t, u in pts if a <= t <= b and u is not None]
        if not v or not quadro:
            continue
        medido = st.mean(v)
        esperado = 100.0 * pps * quadro * 8.0 / (BANDA_MBIT * 1e6)
        res.append({"plano": plano, "taxa": taxa, "medido": medido, "esperado": esperado,
                    "erro_pct": 100.0 * (medido - esperado) / esperado if esperado else None,
                    "quadro": quadro, "n_amostras": len(v)})
    return res


def main(base):
    reps = sorted(glob.glob(os.path.join(base, "rep*")))
    todos = [dict(r, rep=os.path.basename(p)) for p in reps for r in analisa_rep(p)]
    if not todos:
        print("sem dados analisáveis em", base)
        return 1
    print("U0 — calibração do sensor de utilização (banda %.0f Mbit/s, n=%d reps)" % (BANDA_MBIT, len(reps)))
    print("%-5s %-6s %9s %9s %9s %7s" % ("plano", "taxa", "medido%", "esper.%", "erro%", "quadro"))
    chaves = sorted({(r["plano"], r["taxa"]) for r in todos},
                    key=lambda c: (c[0], float(c[1].rstrip("M"))))
    ok = True
    media = {}
    for pl, tx in chaves:
        g = [r for r in todos if (r["plano"], r["taxa"]) == (pl, tx)]
        med, esp = st.mean(r["medido"] for r in g), st.mean(r["esperado"] for r in g)
        err = 100.0 * (med - esp) / esp
        media[(pl, tx)] = (med, g[0]["quadro"])
        flag = "" if abs(err) <= TOL_ERRO_PCT else "  <-- FORA"
        ok &= abs(err) <= TOL_ERRO_PCT
        print("%-5s %-6s %9.2f %9.2f %+9.2f %7d%s" % (pl, tx, med, esp, err, g[0]["quadro"], flag))
    print("\nCusto do SRH no enlace (razão srv6/ipv6):")
    for pl, tx in chaves:
        if pl != "srv6" or ("ipv6", tx) not in media:
            continue
        (m6, q6), (ms, qs) = media[("ipv6", tx)], media[("srv6", tx)]
        prev, obs = qs / q6, ms / m6 if m6 else float("nan")
        dpp = 100.0 * (obs - prev)
        flag = "" if abs(dpp) <= TOL_RAZAO_PP else "  <-- FORA"
        ok &= abs(dpp) <= TOL_RAZAO_PP
        print("  %-6s quadros %d -> %d (+%d B)  razão prevista %.4f  observada %.4f  diferença %+.2f p.p.%s"
              % (tx, q6, qs, qs - q6, prev, obs, dpp, flag))
    print("\nCRITÉRIO U0: %s" % ("APROVADO" if ok else "REPROVADO — corrigir o sensor antes dos cenários"))
    return 0 if ok else 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
