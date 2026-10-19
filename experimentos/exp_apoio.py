#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exp_apoio.py — análise por repetição e por cenário (só stdlib).

  valida <rep_dir> --carga 0|1 --cenario ID   valida a repetição, grava metricas.json
                                              e o arquivo INVALIDA se reprovar
  resumo-cenario <dir_cenario>                média ± dp das repetições válidas

Fases (Seção 3.9.3) vêm dos marcadores; as janelas de vazão/perda/jitter vêm do
JSON do SERVIDOR iperf3 (o do cliente não traz perda por intervalo).
"""
import csv
import json
import math
import os
import re
import statistics as st
import sys

CAMPOS = ["vazao_mbps", "perda_pct", "jitter_ms", "rtt_ms_med", "rtt_ms_p95", "rtt_perda_pct"]


def _json(caminho):
    try:
        with open(caminho) as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


def marcadores(rep):
    m = {}
    try:
        for ln in open(os.path.join(rep, "marcadores.txt")):
            p = ln.split()
            if len(p) == 2:
                m[p[1]] = float(p[0])
    except OSError:
        pass
    return m


def intervalos_servidor(rep):
    d = _json(os.path.join(rep, "iperf_servidor.json"))
    if not d or "intervals" not in d:
        return None, []
    t0 = d["start"]["timestamp"]["timesecs"]
    out = []
    for iv in d["intervals"]:
        s = iv["sum"]
        out.append({"t": t0 + s["start"], "mbps": s["bits_per_second"] / 1e6,
                    "jit": s.get("jitter_ms", 0.0), "lost": s.get("lost_packets", 0),
                    "pkts": s.get("packets", 0)})
    return t0, out


def rtts(rep):
    """[(epoch, rtt_ms)] e nº de pacotes enviados esperados."""
    pts, enviados = [], None
    try:
        for ln in open(os.path.join(rep, "ping.txt")):
            m = re.match(r"\[(\d+\.\d+)\].*time=([\d.]+) ms", ln)
            if m:
                pts.append((float(m.group(1)), float(m.group(2))))
            m2 = re.search(r"(\d+) packets transmitted", ln)
            if m2:
                enviados = int(m2.group(1))
    except OSError:
        pass
    return pts, enviados


def _fases(mk, comcarga):
    """Janelas [ini, fim) em epoch: warmup, carga, relaxamento."""
    ini = mk.get("WARMUP_INICIO")
    fim = mk.get("FIM_TRAFEGO", mk.get("REP_FIM"))
    if ini is None or fim is None:
        return {}
    if comcarga and "CARGA_ON" in mk and "CARGA_OFF" in mk:
        return {"warmup": (ini, mk["CARGA_ON"]), "carga": (mk["CARGA_ON"], mk["CARGA_OFF"]),
                "relax": (mk["CARGA_OFF"], fim)}
    return {"total": (ini, fim)}


def _p95(v):
    if not v:
        return None
    v = sorted(v)
    return v[min(len(v) - 1, int(math.ceil(0.95 * len(v))) - 1)]


def metricas_fase(ivs, pts, janela):
    a, b = janela
    sel = [i for i in ivs if a <= i["t"] < b]
    r = [x for t, x in pts if a <= t < b]
    pk = sum(i["pkts"] for i in sel)
    return {
        "vazao_mbps": st.mean(i["mbps"] for i in sel) if sel else None,
        "perda_pct": (100.0 * sum(i["lost"] for i in sel) / pk) if pk else None,
        "jitter_ms": st.mean(i["jit"] for i in sel) if sel else None,
        "rtt_ms_med": st.mean(r) if r else None,
        "rtt_ms_p95": _p95(r),
        "n_amostras_rtt": len(r),
    }


def ctrl_csv(rep):
    try:
        with open(os.path.join(rep, "ctrl.csv")) as fp:
            return list(csv.DictReader(fp))
    except OSError:
        return []


def eventos_controle(linhas, mk):
    """tempo de convergência, nº de trocas, reversão."""
    on, off = mk.get("CARGA_ON"), mk.get("CARGA_OFF")
    troc = [float(l["t_epoch"]) for l in linhas if l["trocou"] == "1"]
    conv = next((t - on for t in troc if on is not None and t >= on), None)
    rev = next((t - off for t in troc if off is not None and t >= off), None)
    return {"n_trocas": len(troc), "convergencia_s": conv, "reversao_s": rev,
            "caminho_inicial": linhas[0]["escolhido"] if linhas else None,
            "caminho_final": linhas[-1]["escolhido"] if linhas else None}


def contadores_end(rep):
    """(antes, depois) por nó, a partir de 'r1: N pacotes' na sessão da CLI."""
    vals = {"r1": [], "r2": []}
    try:
        for ln in open(os.path.join(rep, "sessao.log")):
            m = re.match(r"\s*(r[12]): (\d+) pacotes", ln)
            if m:
                vals[m.group(1)].append(int(m.group(2)))
    except OSError:
        pass
    return {n: (v[-1] - v[0]) if len(v) >= 2 else None for n, v in vals.items()}


def timeline_max_cpu(rep, no, prefixo="cpu_"):
    try:
        with open(os.path.join(rep, "timeline.tsv")) as fp:
            col = prefixo + no
            v = [float(l[col]) for l in csv.DictReader(fp, delimiter="\t")
                 if l.get(col) not in (None, "NA", "")]
            return max(v) if v else None
    except (OSError, ValueError):
        return None


def valida(rep, comcarga, cenario):
    mk = marcadores(rep)
    t0, ivs = intervalos_servidor(rep)
    pts, enviados = rtts(rep)
    linhas = ctrl_csv(rep)
    ends = contadores_end(rep)
    ok = []

    def chk(nome, cond, critico=True, info=""):
        ok.append((nome, bool(cond), critico, info))

    chk("iperf servidor com intervalos", len(ivs) > 0)
    chk("tráfego recebido (>0 pacotes)", sum(i["pkts"] for i in ivs) > 0)
    chk("ping com respostas", len(pts) > 0)
    if cenario != "E1":
        chk("ctrl.csv com ciclos", len(linhas) >= 3, True, "%d linhas" % len(linhas))
        chk("sem ERRO do RYU", not os.path.exists(os.path.join(rep, "ERRO")))
    no_carga = os.environ.get("CARGA_NO", "r1")
    pico_cpu = timeline_max_cpu(rep, no_carga)
    pico_util = timeline_max_cpu(rep, no_carga, "util_")
    if comcarga and os.environ.get("CARGA_TIPO") == "trafego":
        # v2: a saturação nasce de tráfego real; o sensor é a utilização de enlace
        chk("agente viu a saturação (util>=80%)", pico_util is not None and pico_util >= 80,
            True, "pico_util=%s cpu=%s" % (pico_util, pico_cpu))
    elif comcarga:
        chk("agente viu a carga (CPU>=50%)", pico_cpu is not None and pico_cpu >= 50, True,
            "pico=%s" % pico_cpu)
    if cenario in ("E4", "E4u"):
        chk("função End executou (delta>0)", (ends.get("r1") or 0) + (ends.get("r2") or 0) > 0, True, str(ends))
    else:
        chk("sem SRH (delta End == 0)", (ends.get("r1") or 0) + (ends.get("r2") or 0) == 0, False, str(ends))

    fases = _fases(mk, comcarga)
    met = {"fases": {k: metricas_fase(ivs, pts, j) for k, j in fases.items()},
           "controle": eventos_controle(linhas, mk), "end_delta": ends,
           "rtt_pacotes_enviados": enviados, "rtt_pacotes_recebidos": len(pts),
           "pico_util_pct": pico_util, "pico_cpu_pct": pico_cpu}
    # Janelas em torno da 1ª troca (V-D): o fluxo TE sofre a saturação até o
    # controlador desviá-lo e deve ficar limpo depois (margem de 5 s p/ filas).
    troc = [float(l["t_epoch"]) for l in linhas if l["trocou"] == "1"]
    if troc and "CARGA_ON" in mk and "CARGA_OFF" in mk:
        t1 = troc[0]
        if mk["CARGA_ON"] <= t1 < mk["CARGA_OFF"]:
            met["janelas_troca"] = {
                "pre_troca": metricas_fase(ivs, pts, (mk["CARGA_ON"], t1)),
                "pos_troca": metricas_fase(ivs, pts, (t1 + 5.0, mk["CARGA_OFF"]))}
    if enviados:
        met["rtt_perda_pct"] = 100.0 * (enviados - len(pts)) / enviados
    invalida = any(c and crit for _, c0, crit, _ in ok for c in [not c0])
    met["valida"] = not invalida
    with open(os.path.join(rep, "metricas.json"), "w") as fp:
        json.dump(met, fp, indent=2)
    inv = os.path.join(rep, "INVALIDA")
    if invalida:
        open(inv, "w").write("; ".join(n for n, c, crit, _ in ok if not c and crit))
    elif os.path.exists(inv):
        os.remove(inv)
    for nome, c, crit, info in ok:
        print("[%s] %s %s" % ("OK" if c else ("INVALIDA" if crit else "ALERTA"), nome, info))
    for f, m in met["fases"].items():
        print("  fase %-7s vazão=%s perda=%s%% jitter=%s ms rtt=%s ms" % (
            f, _f(m["vazao_mbps"]), _f(m["perda_pct"]), _f(m["jitter_ms"]), _f(m["rtt_ms_med"])))
    c = met["controle"]
    print("  controle: trocas=%s convergência=%s s reversão=%s s  %s -> %s" % (
        c["n_trocas"], _f(c["convergencia_s"]), _f(c["reversao_s"]), c["caminho_inicial"], c["caminho_final"]))


def _f(x):
    return "NA" if x is None else "%.3f" % x


def agrega(valores):
    v = [x for x in valores if x is not None]
    if not v:
        return None
    return {"n": len(v), "media": st.mean(v), "dp": st.stdev(v) if len(v) > 1 else 0.0}


def metricas_do_cenario(base):
    reps = []
    for d in sorted(os.listdir(base)):
        p = os.path.join(base, d)
        mj = os.path.join(p, "metricas.json")
        if d.startswith("rep") and os.path.exists(mj):
            reps.append((d, _json(mj)))
    return reps


def resumo_cenario(base):
    reps = metricas_do_cenario(base)
    val = [m for _, m in reps if m and m.get("valida")]
    inval = [d for d, m in reps if not (m and m.get("valida"))]
    print("cenário %s: %d válidas, %d inválidas %s" % (os.path.basename(base), len(val), len(inval), inval))
    fases = sorted({f for m in val for f in m["fases"]})
    for f in fases:
        for campo in ("vazao_mbps", "perda_pct", "jitter_ms", "rtt_ms_med"):
            a = agrega([m["fases"][f][campo] for m in val if f in m["fases"]])
            if a:
                print("  %-7s %-11s %.3f ± %.3f (n=%d)" % (f, campo, a["media"], a["dp"], a["n"]))
    for campo in ("n_trocas", "convergencia_s", "reversao_s"):
        a = agrega([m["controle"][campo] for m in val])
        if a:
            print("  controle %-15s %.3f ± %.3f (n=%d)" % (campo, a["media"], a["dp"], a["n"]))


def pcap_bytes(caminho):
    """pcap clássico: (pacotes, bytes_orig, duração_s, maior_pacote)."""
    import struct
    d = open(caminho, "rb").read()
    if len(d) < 24:
        return 0, 0, 0.0, 0
    le = d[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
    e = "<" if le else ">"
    nano = d[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    pos, n, tot, mx, t_ini, t_fim = 24, 0, 0, 0, None, None
    while pos + 16 <= len(d):
        ts, tf, incl, orig = struct.unpack(e + "IIII", d[pos:pos + 16])
        t = ts + tf / (1e9 if nano else 1e6)
        t_ini = t if t_ini is None else t_ini
        t_fim = t
        n, tot, mx = n + 1, tot + orig, max(mx, orig)
        pos += 16 + incl
    return n, tot, ((t_fim - t_ini) if n > 1 else 0.0), mx


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "valida":
        rep = a[1]
        carga = a[a.index("--carga") + 1] == "1" if "--carga" in a else False
        cen = a[a.index("--cenario") + 1] if "--cenario" in a else ""
        valida(rep, carga, cen)
    elif a and a[0] == "pcap-bytes":
        n, tot, dur, mx = pcap_bytes(a[1])
        print("pacotes=%d bytes=%d duracao_s=%.3f maior_pacote=%d taxa_bps=%.1f" % (
            n, tot, dur, mx, (8.0 * tot / dur) if dur else 0.0))
    elif a and a[0] == "trocas":
        print(sum(int(l["trocou"]) for l in ctrl_csv(a[1])))
    elif a and a[0] == "resumo-cenario":
        resumo_cenario(a[1])
    else:
        print(__doc__)
        sys.exit(1)
