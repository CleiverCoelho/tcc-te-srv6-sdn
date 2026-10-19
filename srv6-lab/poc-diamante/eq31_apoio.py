#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eq31_apoio.py — apoio da rodada de revalidação da Eq. 3.1 (rodada2_eq31.sh).

Dois subcomandos, só stdlib (roda no python3 do sistema, sem venv):

  poll   <url> <intervalo_s>
      Consulta GET /srv6/estado a cada intervalo e imprime uma linha TSV por
      amostra, com timestamp epoch. É a série temporal que o log do RYU não
      tem (as linhas [SRv6-TE] não têm timestamp; ver R5 em RISCOS-CENARIOS).

  resumo <timeline.tsv> <marcadores.txt>
      Lê a série e os marcadores de fase e imprime verificações OK/ALERTA/INFO
      contra o comportamento esperado (Eq. 3.1 corrigida: peso == Λ acima de τ).
"""
import json
import sys
import time
import urllib.request

COLUNAS = ["t_epoch", "instalado", "melhor_agora", "custo",
           "cpu_h1", "cpu_r1", "cpu_r2", "cpu_h2",
           "w_s1_r1", "w_s1_r2", "w_r1_s2", "w_r2_s2", "rc_h1",
           "util_r1", "util_r2"]
TAU = 80.0
LAMBDA = 1000.0


def _aresta(enlaces, a, b):
    for x, y, w in enlaces or []:
        if {x, y} == {a, b}:
            return w
    return None


def _fmt(v):
    return "NA" if v is None else str(v)


def poll(url, intervalo):
    print("\t".join(COLUNAS), flush=True)
    while True:
        t = time.time()
        linha = {c: None for c in COLUNAS}
        linha["t_epoch"] = "%.3f" % t
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                e = json.load(r)
            ud = e.get("ultima_decisao") or {}
            mb = e.get("melhor_caminho_agora") or {}
            tel = e.get("telemetria") or {}
            en = e.get("enlaces_ponderados")
            linha["instalado"] = ud.get("escolhido")
            linha["melhor_agora"] = mb.get("escolhido")
            linha["custo"] = mb.get("custo")
            for n in ("h1", "r1", "r2", "h2"):
                linha["cpu_" + n] = (tel.get(n) or {}).get("cpu_percent")
            for n in ("r1", "r2"):
                linha["util_" + n] = (tel.get(n) or {}).get("util_pct")
            linha["w_s1_r1"] = _aresta(en, "s1", "r1")
            linha["w_s1_r2"] = _aresta(en, "s1", "r2")
            linha["w_r1_s2"] = _aresta(en, "r1", "s2")
            linha["w_r2_s2"] = _aresta(en, "r2", "s2")
            inst = ud.get("instalacao") or []
            rc = [i.get("rc") for i in inst if i.get("no") == "h1"]
            linha["rc_h1"] = rc[0] if rc else None
        except Exception:
            pass                       # linha com NA: controlador ainda subindo/sem resposta
        print("\t".join(_fmt(linha[c]) for c in COLUNAS), flush=True)
        time.sleep(max(0.0, intervalo - (time.time() - t)))


# --------------------------------------------------------------------------- #
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _le_timeline(caminho):
    linhas = []
    with open(caminho, encoding="utf-8") as fp:
        cab = fp.readline().rstrip("\n").split("\t")
        for ln in fp:
            v = ln.rstrip("\n").split("\t")
            if len(v) != len(cab):
                continue
            d = dict(zip(cab, v))
            d["t"] = _f(d["t_epoch"])
            if d["t"] is not None:
                linhas.append(d)
    return linhas


def _le_marcadores(caminho):
    m = {}
    with open(caminho, encoding="utf-8") as fp:
        for ln in fp:
            p = ln.split()
            if len(p) >= 2 and _f(p[0]) is not None:
                m[p[1]] = float(p[0])
    return m


def resumo(timeline, marcadores):
    rows = _le_timeline(timeline)
    m = _le_marcadores(marcadores)
    out = []

    def lin(nivel, msg):
        out.append("[%-6s] %s" % (nivel, msg))

    faltam = [k for k in ("WARMUP_INICIO", "CARGA_ON", "CARGA_OFF", "REP_FIM") if k not in m]
    if faltam:
        lin("ALERTA", "marcadores ausentes (%s): a sessão não chegou ao fim" % ", ".join(faltam))
        return "\n".join(out)

    t0, ton, toff, tfim = m["WARMUP_INICIO"], m["CARGA_ON"], m["CARGA_OFF"], m["REP_FIM"]
    validas = [r for r in rows if r["instalado"] not in ("NA", "None")]
    lin("INFO", "amostras: %d (válidas, com política instalada: %d)" % (len(rows), len(validas)))
    lin("INFO", "fases (s): warmup=%.0f  carga=%.0f  relaxamento=%.0f" %
        (ton - t0, toff - ton, tfim - toff))

    base = [r for r in validas if t0 <= r["t"] < ton]
    carga = [r for r in validas if ton <= r["t"] < toff]
    relax = [r for r in validas if toff <= r["t"] <= tfim]
    if not base or not carga:
        lin("ALERTA", "sem amostras válidas em baseline ou carga (controlador não respondeu?)")
        return "\n".join(out)

    # C0 — instalação sem erro em h1
    rcs = {r["rc_h1"] for r in validas}
    lin("OK" if rcs <= {"0", "NA"} else "ALERTA",
        "C0 rc da instalação em h1: %s (esperado só 0)" % sorted(rcs))

    # C1 — baseline estável
    caminhos_base = [r["instalado"] for r in base]
    trocas_base = sum(1 for a, b in zip(caminhos_base, caminhos_base[1:]) if a != b)
    p0 = caminhos_base[-1]
    lin("OK" if trocas_base == 0 else "ALERTA",
        "C1 baseline: caminho instalado=%s, trocas no warmup=%d (esperado 0)" % (p0, trocas_base))

    # C2 — pesos em repouso
    w_rep = [_f(r["w_s1_r1"]) for r in base if _f(r["cpu_r1"]) is not None and _f(r["cpu_r1"]) < TAU]
    w_rep = [w for w in w_rep if w is not None]
    if w_rep:
        ok = all(1.0 <= w < 2.0 for w in w_rep)
        lin("OK" if ok else "ALERTA",
            "C2 peso s1-r1 em repouso: min=%.3f max=%.3f (esperado 1,0 a <2,0)" % (min(w_rep), max(w_rep)))
    else:
        lin("ALERTA", "C2 sem amostras de peso em repouso")

    # C3 — telemetria detecta a carga
    cpu_r1 = [_f(r["cpu_r1"]) for r in carga if _f(r["cpu_r1"]) is not None]
    det = next((r for r in carga if (_f(r["cpu_r1"]) or 0) >= TAU), None)
    if det:
        lin("OK", "C3 CPU de r1 cruzou τ=%.0f%% em %.1f s após o comando (máx=%.1f%%)" %
            (TAU, det["t"] - ton, max(cpu_r1)))
    else:
        lin("ALERTA", "C3 CPU de r1 nunca atingiu τ=%.0f%% (máx=%s): telemetria não viu a carga" %
            (TAU, max(cpu_r1) if cpu_r1 else "NA"))

    # C4 — Eq. 3.1 corrigida: peso == Λ exatamente acima de τ
    w_sat = [_f(r["w_s1_r1"]) for r in carga if (_f(r["cpu_r1"]) or 0) >= TAU]
    w_sat = [w for w in w_sat if w is not None]
    if w_sat:
        if all(w == LAMBDA for w in w_sat):
            lin("OK", "C4 peso s1-r1 saturada == %.1f em todas as %d amostras (Eq. 3.1 corrigida)" %
                (LAMBDA, len(w_sat)))
        elif any(w > LAMBDA for w in w_sat):
            lin("ALERTA", "C4 peso saturado > Λ (ex.: %.3f): ainda é a versão ADITIVA antiga" % max(w_sat))
        else:
            lin("ALERTA", "C4 pesos saturados inesperados: %s" % sorted(set(w_sat)))
    else:
        lin("ALERTA", "C4 sem amostras com r1 ≥ τ para checar o peso")

    # C5 — reroteamento automático e convergência
    if p0 == "r2":
        lin("INFO", "C5 INCONCLUSIVO: caminho inicial já era r2 (empate arbitrário, R7); "
                    "a carga em r1 não exercita a troca nesta repetição")
    else:
        troca = next((r for r in carga if r["instalado"] != p0), None)
        if troca:
            conv = troca["t"] - ton
            lin("OK" if conv <= 25 else "ALERTA",
                "C5 reroteamento %s -> %s em %.1f s após a carga (esperado <= ~25 s: "
                "detecção 1 s + até 1 ciclo de 10 s + margem)" % (p0, troca["instalado"], conv))
        else:
            lin("ALERTA", "C5 NÃO houve troca de caminho durante os %.0f s de carga" % (toff - ton))

    # C6 — estabilidade durante a carga (flapping)
    seq = [r["instalado"] for r in carga]
    trocas_carga = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    lin("OK" if trocas_carga <= 1 else "ALERTA",
        "C6 trocas de caminho durante a carga: %d (esperado <= 1; sem histerese pode oscilar)" % trocas_carga)

    # C7 — relaxamento (informativo: o desempate é arbitrário, R7)
    if relax:
        w_fim = _f(relax[-1]["w_s1_r1"])
        cpu_fim = _f(relax[-1]["cpu_r1"])
        pf = relax[-1]["instalado"]
        lin("INFO", "C7 relaxamento: caminho final=%s (inicial=%s, %s); CPU r1 final=%s%%; peso s1-r1 final=%s" %
            (pf, p0, "reverteu" if pf == p0 else "NÃO reverteu — empate de custo, R7",
             _fmt(cpu_fim), _fmt(w_fim)))
        if w_fim is not None and w_fim >= LAMBDA:
            lin("ALERTA", "C7 peso de s1-r1 continua penalizado após remover a carga")
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "poll":
        try:
            poll(sys.argv[2], float(sys.argv[3]))
        except KeyboardInterrupt:
            pass
    elif len(sys.argv) >= 4 and sys.argv[1] == "resumo":
        print(resumo(sys.argv[2], sys.argv[3]))
    else:
        print(__doc__)
        sys.exit(2)
