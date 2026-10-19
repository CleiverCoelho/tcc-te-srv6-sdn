#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telemetry_agent.py — agente de telemetria de CPU por nó (TCC, Seção 3.8).

Roda FORA do processo do Ryu, um processo por nó do Mininet, usando
OBRIGATORIAMENTE o interpretador do venv do RYU: o Python do sistema, que é
quem executa o Mininet, não tem `psutil` instalado (só o venv tem — ver
setup_ambiente.sh). Por isso este script não pode ser importado direto de
dentro de topo_diamante.py; ele precisa ser lançado como processo separado
com o python do venv.

O QUE MEDE E COMO
------------------
Amostra a CPU da árvore de processos enraizada no PID do shell persistente
que o Mininet já cria por nó (publicado em manifesto.json como nos.<nome>.pid
por escreve_manifesto()) — tudo que o nó rodar depois (iperf3, `yes`, etc.)
nasce como filho desse shell, então monitorar essa árvore cobre a carga real
do nó sem precisar de isolamento de namespace de PID (que o Mininet não usa).

O agente é lançado por topo_diamante.py NO NAMESPACE RAIZ (subprocess.Popen,
não `no.cmd()`): o Mininet isola namespace de REDE por nó, mas não isola
namespace de PID, então psutil.Process(pid) enxerga qualquer nó igualmente
bem a partir da raiz — e só a partir da raiz o '127.0.0.1' do --coletor
aponta para o mesmo loopback onde o coletor do controlador escuta (dentro
do namespace de um nó, seria um loopback isolado e diferente). Isso já foi
testado das duas formas; só a partir da raiz conecta.

IMPORTANTE sobre cpu_percent(interval=None): ele mede o delta desde a
ÚLTIMA CHAMADA NAQUELE MESMO OBJETO psutil.Process — não por PID, e não
globalmente. Por isso este script mantém um registro persistente
{pid: psutil.Process} entre ciclos (ver _atualiza_registro), em vez de
recriar os objetos a cada amostra: recriar objetos a cada chamada faz
cpu_percent devolver 0.0 SEMPRE, porque cada objeto novo acha que é sua
primeira chamada. Isso foi um bug real deste script, encontrado ao validar
com carga sintética real (yes > /dev/null) que a CPU nunca subia — via
n_processos subindo, mas cpu_percent teimosamente em 0.0.

PROTOCOLO
---------
NDJSON sobre um socket TCP persistente para o coletor embutido no
controlador (ryu_apps/srv6_te_controller.py, hub.StreamServer): uma conexão
por agente, uma linha JSON por amostra. Deliberadamente o formato mais
simples possível — sem framing binário, sem dependências além da stdlib +
psutil — fácil de inspecionar com `nc`/`tcpdump` para evidência.

Uso:
    python3 telemetry_agent.py --no r1 --pid 1234 --coletor 127.0.0.1:9999
"""

import argparse
import json
import os
import socket
import time

import psutil


def _atualiza_registro(pid_raiz, pid_proprio, registro):
    """Descobre a árvore de processos atual (raiz + filhos recursivos,
    excluindo o próprio agente) e garante que `registro` (dict mutável
    pid -> psutil.Process) tenha um objeto para cada um.

    Reaproveitar o MESMO objeto entre ciclos é o que importa aqui: um
    processo já conhecido mantém seu objeto (preservando o estado interno
    que cpu_percent usa para calcular o delta); um processo novo ganha um
    objeto fresco e uma chamada de "aquecimento" imediata (cujo valor é
    descartado, só estabelece a referência de tempo); um processo que
    sumiu é removido do registro."""
    try:
        raiz = psutil.Process(pid_raiz)
        vivos = {raiz.pid: raiz}
        for p in raiz.children(recursive=True):
            if p.pid != pid_proprio:
                vivos[p.pid] = p
    except psutil.NoSuchProcess:
        vivos = {}

    for pid_antigo in list(registro):
        if pid_antigo not in vivos:
            del registro[pid_antigo]

    for pid_novo, proc in vivos.items():
        if pid_novo not in registro:
            registro[pid_novo] = proc
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


def _amostra_cpu(registro):
    total, vivos = 0.0, 0
    for pid, proc in list(registro.items()):
        try:
            total += proc.cpu_percent(interval=None)
            vivos += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            del registro[pid]
    return total, vivos


def le_netdev(pid, raiz="/proc"):
    """Contadores {iface: (rx_bytes, tx_bytes)} do namespace de rede do nó.

    /proc/<pid>/net/dev mostra a tabela do netns do PROCESSO <pid>, então o
    agente (no namespace raiz) lê as interfaces do nó sem entrar nele. Os
    contadores são do kernel e incluem o encaminhamento (seg6, netem, OVS),
    que a CPU de processos não enxerga. Contam bytes de camada 2 (com o
    cabeçalho Ethernet)."""
    contadores = {}
    try:
        with open("%s/%d/net/dev" % (raiz, pid)) as fp:
            linhas = fp.read().splitlines()[2:]
    except OSError:
        return contadores
    for linha in linhas:
        nome, _, resto = linha.partition(":")
        campos = resto.split()
        nome = nome.strip()
        if nome and nome != "lo" and len(campos) >= 9:
            contadores[nome] = (int(campos[0]), int(campos[8]))
    return contadores


def calcula_util(antes, depois, dt, banda_mbit):
    """Utilização do nó (Seção 3.5.3, variante v2): o MAIOR entre as taxas de
    entrada e de saída de qualquer interface, dividido pela banda do enlace.
    Devolve (util_pct, {iface: (rx_bps, tx_bps)}); util_pct é None quando o nó
    não tem banda definida (hosts de borda, sem shaper) ou dt <= 0."""
    taxas = {}
    if dt > 0:
        for iface, (rx1, tx1) in depois.items():
            if iface in antes:
                rx0, tx0 = antes[iface]
                taxas[iface] = (max(rx1 - rx0, 0) * 8.0 / dt, max(tx1 - tx0, 0) * 8.0 / dt)
    if banda_mbit <= 0 or not taxas:
        return None, taxas
    pico = max(max(rx, tx) for rx, tx in taxas.values())
    return round(100.0 * pico / (banda_mbit * 1e6), 2), taxas


def main():
    ap = argparse.ArgumentParser(description="Agente de telemetria por nó (TCC, Sec. 3.8)")
    ap.add_argument("--no", required=True, help="nome do nó (h1, r1, r2, h2, ...)")
    ap.add_argument("--pid", type=int, required=True,
                    help="PID raiz do nó (shell persistente criado pelo Mininet)")
    ap.add_argument("--coletor", required=True, help="host:porta do coletor TCP no controlador")
    ap.add_argument("--intervalo", type=float, default=1.0, help="segundos entre amostras (padrão 1.0)")
    ap.add_argument("--banda-mbit", type=float, default=0.0,
                    help="banda dos enlaces do nó em Mbit/s; >0 liga util_pct (v2). "
                         "0 = nó sem banda definida (host de borda): util_pct = null")
    args = ap.parse_args()

    host, porta_str = args.coletor.rsplit(":", 1)
    porta = int(porta_str)
    pid_proprio = os.getpid()

    # registro persiste OS MESMOS objetos psutil.Process entre ciclos —
    # ver _atualiza_registro. Esta chamada inicial já estabelece a
    # referência de tempo (aquecimento) para os processos que existem
    # desde o início, antes do primeiro socket.
    registro = {}
    _atualiza_registro(args.pid, pid_proprio, registro)

    falhas_seguidas = 0
    while True:
        sock = None
        try:
            sock = socket.create_connection((host, porta), timeout=5)
            sock.sendall((json.dumps({"tipo": "hello", "no": args.no, "pid": args.pid}) + "\n")
                         .encode("utf-8"))
            falhas_seguidas = 0
            ref_netdev, ref_t = le_netdev(args.pid), time.time()
            while True:
                time.sleep(args.intervalo)
                _atualiza_registro(args.pid, pid_proprio, registro)
                cpu, n = _amostra_cpu(registro)
                agora_netdev, agora_t = le_netdev(args.pid), time.time()
                util, taxas = calcula_util(ref_netdev, agora_netdev,
                                           agora_t - ref_t, args.banda_mbit)
                ref_netdev, ref_t = agora_netdev, agora_t
                msg = {
                    "tipo": "amostra", "no": args.no,
                    "cpu_percent": round(cpu, 1), "n_processos": n,
                    "util_pct": util,
                    "taxas_bps": {i: [round(rx), round(tx)] for i, (rx, tx) in taxas.items()},
                    "ts": time.time(),
                }
                sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        except (OSError, socket.error) as e:
            # Coletor ainda não subiu, caiu, ou a rede momentaneamente
            # falhou — reconecta depois do backoff abaixo. É esperado que
            # isso aconteça nos primeiros ciclos se a ordem de partida for
            # Mininet antes do RYU. Mas ficar mudo para sempre torna um
            # problema PERSISTENTE (ex.: endereço errado, namespace sem
            # rota até o coletor) indistinguível de "ainda não conectou" —
            # por isso loga, sem spam, a cada 5ª falha consecutiva.
            falhas_seguidas += 1
            if falhas_seguidas == 1 or falhas_seguidas % 5 == 0:
                print("[telemetry_agent:%s] falha ao conectar em %s:%s (tentativa %d): %s"
                     % (args.no, host, porta, falhas_seguidas, e), flush=True)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        time.sleep(2.0)


if __name__ == "__main__":
    main()
