#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
92_analisa_u0.py — análise do U0 (calibração do sensor de utilização, v2).

Uso: python3 experimentos/92_analisa_u0.py relatorios/<data>/experimentos/U0_calibracao

Para cada (repetição, plano, taxa), numa janela estável do fluxo definida pelo
instante REAL de início do cliente iperf3 (timestamp do JSON), [t0+4 s, t0+dur-1 s]:
  medido   = média do util_r1 do timeline (agente, /proc/<pid>/net/dev de r1)
  esperado = pps (dos intervalos de 1 s do cliente NA MESMA janela) × quadro L2 modal
             do pcap × 8 ÷ banda
Critérios (plano v2), sobre a MÉDIA das repetições e só para taxas sem perda (<= 1 %):
  (a) |erro| <= 5 %;
  (b) fator de overhead por bit de payload (util × banda ÷ goodput) = quadro/blksize
      em cada plano, ±5 %;
  (c) custo do SRH = razão dos fatores srv6/ipv6, previsto = (q6/b6)/(qs/bs) ±2 p.p.
Achado de projeto: com SRv6 o quadro no enlace tem o MESMO tamanho (1490 B) porque o
iperf3 deriva o datagrama do MSS do TCP de controle, que já desconta a encapsulação;
o custo do SRH aparece como MENOS payload por quadro (blksize), não como quadro maior.
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
TOL_FATOR_PCT = 5.0
TOL_RAZAO_PP = 2.0
PERDA_MAX_PCT = 1.0
SKIP_INI_S, SKIP_FIM_S = 4.0, 1.0


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
    grandes = [t for t in tamanhos if t > 500]
    return collections.Counter(grandes).most_common(1)[0][0] if grandes else None


def json_de(caminho):
    try:
        return json.load(open(caminho))
    except (OSError, ValueError):
        return None


def marcadores(rep):
    m = {}
    for ln in open(os.path.join(rep, "marcadores.txt")):
        p = ln.split()
        if len(p) == 2:
            m[p[1]] = float(p[0])
    return m


def timeline(rep):
    out = []
    with open(os.path.join(rep, "timeline.tsv")) as fp:
        cab = fp.readline().rstrip("\n").split("\t")
        for ln in fp:
            v = ln.rstrip("\n").split("\t")
            if len(v) == len(cab):
                d = dict(zip(cab, v))
                try:
                    out.append((float(d["t_epoch"]), None if d["util_r1"] == "NA" else float(d["util_r1"])))
                except (KeyError, ValueError):
                    continue
    return out


def analisa_rep(rep):
    mk, pts = marcadores(rep), timeline(rep)
    res, incompletas = [], []
    for k in sorted(x for x in mk if x.startswith("INI_")):
        tag = k[4:]
        plano, taxa = tag.split("_", 1)
        j = json_de(os.path.join(rep, "iperf_cli_%s.json" % tag))
        srv = json_de(os.path.join(rep, "iperf_srv_%s.json" % tag))
        pcap = os.path.join(rep, "%s.pcap" % tag)
        if not j or not j.get("intervals") or not os.path.exists(pcap):
            incompletas.append(tag)
            continue
        t0 = j["start"]["timestamp"]["timesecs"]
        blk = j["start"]["test_start"]["blksize"]
        dur = j["start"]["test_start"]["duration"]
        a, b = t0 + SKIP_INI_S, t0 + dur - SKIP_FIM_S
        ivs = [i["sum"] for i in j["intervals"] if a <= t0 + i["sum"]["start"] and t0 + i["sum"]["end"] <= b + 0.5]
        quadro = quadro_modal(tamanhos_pcap(pcap))
        v = [u for t, u in pts if a <= t <= b and u is not None]
        if not ivs or not v or not quadro:
            incompletas.append(tag)
            continue
        pps = sum(i["packets"] for i in ivs) / sum(i["end"] - i["start"] for i in ivs)
        goodput = pps * blk * 8.0
        medido = st.mean(v)
        esperado = 100.0 * pps * quadro * 8.0 / (BANDA_MBIT * 1e6)
        perda = ((srv or {}).get("end", {}).get("sum", {}) or {}).get("lost_percent")
        res.append({"plano": plano, "taxa": taxa, "medido": medido, "esperado": esperado,
                    "quadro": quadro, "blk": blk, "goodput": goodput, "perda": perda,
                    "fator_obs": (medido / 100.0 * BANDA_MBIT * 1e6) / goodput,
                    "fator_prev": quadro / float(blk), "n_amostras": len(v),
                    "dp": st.stdev(v) if len(v) > 1 else 0.0})
    return res, incompletas


def main(base):
    reps = sorted(glob.glob(os.path.join(base, "rep*")))
    todos, inc = [], []
    for p in reps:
        r, i = analisa_rep(p)
        todos += [dict(x, rep=os.path.basename(p)) for x in r]
        inc += ["%s/%s" % (os.path.basename(p), t) for t in i]
    if not todos:
        print("sem dados analisáveis em", base)
        return 1
    print("U0 — calibração do sensor de utilização (banda %.0f Mbit/s, n=%d reps)" % (BANDA_MBIT, len(reps)))
    if inc:
        print("Corridas INCOMPLETAS (sem JSON/pcap/amostras; excluídas): %s" % ", ".join(inc))
    print("%-5s %-6s %8s %8s %8s %7s %6s %7s %6s %s" % (
        "plano", "taxa", "medido%", "esper.%", "erro%", "quadro", "blk", "perda%", "dp%", "reps"))
    chaves = sorted({(r["plano"], r["taxa"]) for r in todos}, key=lambda c: (c[0], float(c[1].rstrip("M"))))
    ok, media = True, {}
    for pl, tx in chaves:
        g = [r for r in todos if (r["plano"], r["taxa"]) == (pl, tx)]
        med, esp = st.mean(r["medido"] for r in g), st.mean(r["esperado"] for r in g)
        err = 100.0 * (med - esp) / esp
        perda = max((r["perda"] or 0.0) for r in g)
        sat = perda > PERDA_MAX_PCT
        flag = "  (saturado: fora do critério)" if sat else ("" if abs(err) <= TOL_ERRO_PCT else "  <-- FORA")
        if not sat:
            ok &= abs(err) <= TOL_ERRO_PCT
            media[(pl, tx)] = (med, g[0]["quadro"], g[0]["blk"], st.mean(r["fator_obs"] for r in g))
        print("%-5s %-6s %8.2f %8.2f %+8.2f %7d %6d %7.2f %6.2f %d%s" % (
            pl, tx, med, esp, err, g[0]["quadro"], g[0]["blk"], perda, st.mean(r["dp"] for r in g), len(g), flag))
    print("\nFator de overhead por bit de payload (util×banda ÷ goodput) — previsto = quadro/blksize:")
    for (pl, tx), (med, q, blk, fobs) in sorted(media.items(), key=lambda kv: (kv[0][0], float(kv[0][1].rstrip("M")))):
        prev = q / float(blk)
        dev = 100.0 * (fobs - prev) / prev
        flag = "" if abs(dev) <= TOL_FATOR_PCT else "  <-- FORA"
        ok &= abs(dev) <= TOL_FATOR_PCT
        print("  %-5s %-6s previsto %.4f  observado %.4f  desvio %+.2f %%%s" % (pl, tx, prev, fobs, dev, flag))
    print("\nCusto do SRH (razão srv6/ipv6 do overhead por bit de payload):")
    for pl, tx in sorted(media, key=lambda c: (c[0], float(c[1].rstrip("M")))):
        if pl != "srv6" or ("ipv6", tx) not in media:
            continue
        (_, qs, bs, fs), (_, q6, b6, f6) = media[("srv6", tx)], media[("ipv6", tx)]
        prev, obs = (qs / bs) / (q6 / b6), fs / f6
        dpp = 100.0 * (obs - prev)
        flag = "" if abs(dpp) <= TOL_RAZAO_PP else "  <-- FORA"
        ok &= abs(dpp) <= TOL_RAZAO_PP
        print("  %-6s quadro %d/%d B  payload ipv6=%d srv6=%d B (−%.1f %%)  razão prevista %.4f  observada %.4f  diferença %+.2f p.p.%s"
              % (tx, q6, qs, b6, bs, 100.0 * (1 - bs / float(b6)), prev, obs, dpp, flag))
    print("\nCRITÉRIO U0: %s" % ("APROVADO" if ok else "REPROVADO — ver linhas marcadas"))
    return 0 if ok else 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
