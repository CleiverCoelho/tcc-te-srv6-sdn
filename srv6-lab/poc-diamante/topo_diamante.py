#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
 topo_diamante.py — Prova de conceito da topologia em diamante do TCC.

 "Engenharia de Tráfego em Redes de Computadores utilizando Segment Routing
  e SDN" — a topologia em diamante da Seção 3.4 é a metodologia principal de
  teste. Este script prova, com medição, que ela é executável neste ambiente.

 O QUE ESTE LABORATÓRIO ACRESCENTA À FASE F0 (../topo_srv6.py)
 -------------------------------------------------------------------------
 A F0 tem UM nó de trânsito. Com um só caminho, é impossível distinguir
 "o SRv6 dirigiu o tráfego" de "o tráfego foi por onde já iria de qualquer
 forma". A F0 prova o MECANISMO (o SRH existe, a função End executa); ela não
 prova a PREMISSA do TCC — que a lista de segmentos ESCOLHE o caminho.

 Aqui há DOIS nós de trânsito com atrasos diferentes. Isso torna a afirmação
 falseável: se a lista de segmentos não dirigisse nada, os contadores dos dois
 roteadores subiriam juntos e o RTT não mudaria ao trocar a lista.

                r1  SID 2001:db8:1::1  (End)   netem  5 ms
               /                          \\
   h1 -- s1 --                              -- s2 -- h2
               \\                          /
                r2  SID 2001:db8:2::1  (End)   netem 10 ms

 IMPORTANTE — este script depende do Mininet, que existe SOMENTE no Python do
 sistema (/usr/lib/python3/dist-packages). O venv do RYU tem
 include-system-site-packages=false e NÃO o enxerga. Execute com
 /usr/bin/python3, nunca com o python do venv ativo.

 Uso:  sudo /usr/bin/python3 topo_diamante.py --auto-teste
=============================================================================
"""

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

# --------------------------------------------------------------------------- #
# Parâmetros (estendem o endereçamento já usado na fase F0)
# --------------------------------------------------------------------------- #
PREFIXO_A = "2001:db8:a::"       # domínio L2 do lado de h1 (switch s1)
PREFIXO_B = "2001:db8:b::"       # domínio L2 do lado de h2 (switch s2)

H1_IP = "2001:db8:a::1"
R1_IP_A, R1_IP_B = "2001:db8:a::2", "2001:db8:b::2"
R2_IP_A, R2_IP_B = "2001:db8:a::3", "2001:db8:b::3"
H2_IP = "2001:db8:b::1"
# v2: endereço alias de h2 para o tráfego de FUNDO. Fica dentro do /64 B, cuja
# rota em h1 é a linha de base via r1 (sem SRH): o controlador de TE só troca a
# rota /128 de H2_IP, então o fundo é "preso" em r1 e satura o nó sem ser
# redirecionado — é o estímulo real que a v2 usa no lugar de CPU sintética.
H2_FUNDO_IP = "2001:db8:b::100"
PORTA_FUNDO = 5202

# Um locator /64 por nó de trânsito, padrão 2001:db8:N::/64 (RFC 8986, §3.1).
LOCATOR = {"r1": "2001:db8:1::/64", "r2": "2001:db8:2::/64"}
SID = {"r1": "2001:db8:1::1", "r2": "2001:db8:2::1"}

# Atraso netem por enlace de trânsito. Aplicado nas DUAS pontas de cada enlace,
# portanto cada travessia custa este valor. Caminho = 2 enlaces, ida e volta:
#   RTT esperado = 4 x atraso  ->  r1 = 20 ms, r2 = 40 ms
ATRASO_MS = {"r1": 5, "r2": 10}
BANDA_MBIT = 10                  # TCC, Seção 3.4.2

def rtt_esperado(via):
    return 4.0 * ATRASO_MS[via]

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_CAPTURAS = os.path.join(AQUI, "capturas")
MANIFESTO = "/tmp/srv6-poc/manifesto.json"
REST_RYU = "http://127.0.0.1:8080"

# Telemetria real por nó (TCC, Seção 3.8) — ver ../ryu_apps/telemetry_agent.py
# e o coletor hub.StreamServer em ../ryu_apps/srv6_te_controller.py. O agente
# precisa do python do VENV do RYU (só lá tem psutil); topo_diamante.py em si
# roda no python do sistema (só ele enxerga o Mininet) — os dois interpretes
# coexistem neste laboratório, cada um usado onde é preciso.
RYU_VENV_PYTHON = os.path.join(AQUI, "..", "..", "ryu-venv", "bin", "python3")
AGENTE_TELEMETRIA = os.path.join(AQUI, "..", "..", "ryu_apps", "telemetry_agent.py")
PORTA_TELEMETRIA = 9999
NOS_COM_TELEMETRIA = ("h1", "r1", "r2", "h2")
# v2: nós cujos enlaces têm banda definida (shaper do TCLink) — só neles a
# utilização de enlace faz sentido; as bordas não são moldadas (util = null).
NOS_COM_BANDA = ("r1", "r2")

# Acumula tudo o que for medido; vira capturas/resultados.json no fim.
RESULTADOS = {"gerado_em": None, "parametros": {}, "blocos": {}}


# --------------------------------------------------------------------------- #
# Topologia
# --------------------------------------------------------------------------- #
class TopoDiamante(Topo):
    """Diamante de dois caminhos disjuntos em L3.

    A ordem de criação dos enlaces fixa o nome das interfaces:
        1) h1<->s1  => h1-eth0
        2) r1<->s1  => r1-eth0     5) r2<->s2  => r2-eth1
        3) r2<->s1  => r2-eth0     6) h2<->s2  => h2-eth0
        4) r1<->s2  => r1-eth1
    """

    def build(self):
        h1 = self.addHost("h1", ip=None)
        r1 = self.addHost("r1", ip=None)
        r2 = self.addHost("r2", ip=None)
        h2 = self.addHost("h2", ip=None)

        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")

        # As bordas não são moldadas: qualquer atraso aqui seria comum aos dois
        # caminhos e só somaria ruído à comparação.
        self.addLink(h1, s1)
        self.addLink(r1, s1, cls=TCLink, bw=BANDA_MBIT, delay="%dms" % ATRASO_MS["r1"])
        self.addLink(r2, s1, cls=TCLink, bw=BANDA_MBIT, delay="%dms" % ATRASO_MS["r2"])
        self.addLink(r1, s2, cls=TCLink, bw=BANDA_MBIT, delay="%dms" % ATRASO_MS["r1"])
        self.addLink(r2, s2, cls=TCLink, bw=BANDA_MBIT, delay="%dms" % ATRASO_MS["r2"])
        self.addLink(h2, s2)


# --------------------------------------------------------------------------- #
# Configuração da pilha IPv6/SRv6 dentro dos namespaces
# --------------------------------------------------------------------------- #
def _prepara_interface(no, iface):
    """Ajustes de pilha IPv6 exigidos por este laboratório.

    accept_dad=0   — evita o ~1 s de Duplicate Address Detection, que deixaria o
                     endereço "tentative" e faria os primeiros pings falharem.

    seg6_enabled=1 — o Linux DESCARTA silenciosamente pacotes com Segment Routing
                     Header por padrão: roteamento na origem é vetor de ataque
                     conhecido (o Routing Header Type 0 foi banido pela RFC 5095).
                     É a autorização explícita, e é POR INTERFACE — não herda de
                     'all'. Precisa estar ligada em toda interface onde um pacote
                     com SRH CHEGA. (TCC, Seção 3.6.1.)

    accept_redirects=0 — específico do diamante, e não existia na F0. Com DOIS
                     roteadores no mesmo domínio L2, um deles pode emitir um
                     ICMPv6 Redirect ensinando o host a usar o outro. O tráfego
                     chegaria ao destino por um caminho que a lista de segmentos
                     nunca nomeou: o teste PARECERIA aprovado sem provar nada.
                     Desligar redirects elimina essa explicação alternativa.

    accept_ra=0    — os roteadores não anunciam prefixo aqui; todo o roteamento é
                     estático e explícito. Evita rota default surpresa.
    """
    for chave, valor in (("disable_ipv6", 0), ("accept_dad", 0), ("seg6_enabled", 1),
                         ("accept_redirects", 0), ("accept_ra", 0)):
        no.cmd("sysctl -w net.ipv6.conf.%s.%s=%d" % (iface, chave, valor))


def configura_rede(net):
    """Endereçamento, roteamento estático e as duas funções End."""
    h1, r1, r2, h2 = (net.get(n) for n in ("h1", "r1", "r2", "h2"))

    # ------------------------------------------------------------ bordas ---- #
    for no, iface, addr in ((h1, "h1-eth0", H1_IP), (h2, "h2-eth0", H2_IP)):
        info("*** Configurando %s (borda)\n" % no.name)
        no.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=0")
        no.cmd("sysctl -w net.ipv6.conf.all.seg6_enabled=1")
        no.cmd("sysctl -w net.ipv6.conf.all.accept_redirects=0")
        _prepara_interface(no, iface)
        no.cmd("ip -6 addr add %s/64 dev %s" % (addr, iface))
    # preferred_lft 0 = endereço "deprecated": responde a quem o endereça, mas o
    # kernel NÃO o escolhe como origem. Sem isso, um servidor UDP em wildcard
    # (iperf3 -s do fluxo TE) respondia ao h1 com origem b::100 em vez de b::1; o
    # socket UDP do cliente, conectado a b::1, descartava a resposta e o
    # handshake do iperf3 nunca completava (achado do diagnóstico, 18/10).
    h2.cmd("ip -6 addr add %s/64 dev h2-eth0 valid_lft forever preferred_lft 0" % H2_FUNDO_IP)

    # h1 precisa saber alcançar CADA locator, para que o NDP resolva o endereço
    # REAL do roteador. O SID em si nunca é perguntado ao NDP — ninguém o
    # responderia, pois ele não é endereço de interface de ninguém.
    h1.cmd("ip -6 route add %s via %s dev h1-eth0" % (LOCATOR["r1"], R1_IP_A))
    h1.cmd("ip -6 route add %s via %s dev h1-eth0" % (LOCATOR["r2"], R2_IP_A))
    # Rota IPv6 comum — é a LINHA DE BASE, sem SRH, e passa por r1.
    h1.cmd("ip -6 route add %s/64 via %s dev h1-eth0" % (PREFIXO_B, R1_IP_A))

    h2.cmd("ip -6 route add %s via %s dev h2-eth0" % (LOCATOR["r1"], R1_IP_B))
    h2.cmd("ip -6 route add %s via %s dev h2-eth0" % (LOCATOR["r2"], R2_IP_B))
    h2.cmd("ip -6 route add %s/64 via %s dev h2-eth0" % (PREFIXO_A, R1_IP_B))

    # ---------------------------------------------------------- trânsito ---- #
    for no, ip_a, ip_b in ((r1, R1_IP_A, R1_IP_B), (r2, R2_IP_A, R2_IP_B)):
        info("*** Configurando %s (roteador SRv6 — função End)\n" % no.name)
        no.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=0")
        no.cmd("sysctl -w net.ipv6.conf.all.forwarding=1")
        no.cmd("sysctl -w net.ipv6.conf.all.seg6_enabled=1")
        no.cmd("sysctl -w net.ipv6.conf.all.accept_redirects=0")
        for iface in ("%s-eth0" % no.name, "%s-eth1" % no.name):
            _prepara_interface(no, iface)
            no.cmd("sysctl -w net.ipv6.conf.%s.forwarding=1" % iface)
        no.cmd("ip -6 addr add %s/64 dev %s-eth0" % (ip_a, no.name))
        no.cmd("ip -6 addr add %s/64 dev %s-eth1" % (ip_b, no.name))

        # A FUNÇÃO End (RFC 8986, §4.1). O SID NÃO é endereço de interface: se
        # fosse, a rota 'local' do kernel venceria esta rota seg6local e o pacote
        # seria ENTREGUE localmente em vez de processado pela função End.
        #
        # Uma única rota End serve aos DOIS sentidos: ela casa por destino, não
        # por interface de entrada. Chegando por eth0 ela encaminha para eth1, e
        # vice-versa, porque o End refaz a busca de rota após reescrever o DA.
        #
        # 'count' liga contadores por ação (pacotes/bytes/erros) — a prova DIRETA,
        # independente de captura, de que a função executou.
        saida = no.cmd("ip -6 route add %s encap seg6local action End count dev %s-eth1"
                       % (SID[no.name], no.name))
        if "rror" in saida or "invalid" in saida.lower():
            info("*** 'count' não suportado em %s; instalando End sem contadores\n" % no.name)
            no.cmd("ip -6 route add %s encap seg6local action End dev %s-eth1"
                   % (SID[no.name], no.name))


# --------------------------------------------------------------------------- #
# Política SRv6 de ingresso
# --------------------------------------------------------------------------- #
def aplica_srv6(net, via):
    """Instala a política SRv6 nos DOIS sentidos, forçando o caminho por `via`.

    A rota é /128 (host) e portanto vence a /64 da linha de base na decisão de
    encaminhamento. 'mode encap' acrescenta um cabeçalho IPv6 externo carregando
    o SRH com a lista de segmentos.

    Por que os dois sentidos: se só a ida fosse dirigida, a volta escolheria o
    caminho da rota comum e o RTT medido misturaria os dois atrasos, embaralhando
    exatamente a grandeza que este teste quer isolar.
    """
    net.get("h1").cmd("ip -6 route replace %s encap seg6 mode encap segs %s,%s dev h1-eth0"
                      % (H2_IP, SID[via], H2_IP))
    net.get("h2").cmd("ip -6 route replace %s encap seg6 mode encap segs %s,%s dev h2-eth0"
                      % (H1_IP, SID[via], H1_IP))
    info("*** SRv6 ativo via %s — lista [%s, destino]\n" % (via, SID[via]))


def remove_srv6(net):
    """Volta à linha de base: encaminhamento IPv6 comum, sem SRH."""
    net.get("h1").cmd("ip -6 route del %s dev h1-eth0 2>/dev/null" % H2_IP)
    net.get("h2").cmd("ip -6 route del %s dev h2-eth0 2>/dev/null" % H1_IP)
    info("*** SRv6 removido — linha de base IPv6\n")


def politica_atual(net):
    """Descobre por qual SID a política de h1 está passando, lendo o kernel."""
    saida = net.get("h1").cmd("ip -6 route show %s" % H2_IP)
    for nome, sid in SID.items():
        if sid in saida:
            return nome
    return "ipv6" if saida.strip() else "nenhuma"


# --------------------------------------------------------------------------- #
# Instrumentos de medida
# --------------------------------------------------------------------------- #
def _tamanhos_pcap(caminho):
    """Tamanho original de cada quadro de um pcap clássico, sem depender de tshark.

    Formato: cabeçalho global de 24 B; por pacote, cabeçalho de 16 B cujos
    últimos 4 B são orig_len. (Mesma rotina validada na fase F0.)
    """
    try:
        with open(caminho, "rb") as fp:
            dados = fp.read()
    except IOError:
        return []
    if len(dados) < 24:
        return []
    magic = dados[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        end = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        end = ">"
    else:
        return []
    tamanhos, pos = [], 24
    while pos + 16 <= len(dados):
        incl, orig = struct.unpack(end + "II", dados[pos + 8:pos + 16])
        tamanhos.append(orig)
        pos += 16 + incl
    return tamanhos


def _moda(valores):
    return max(set(valores), key=valores.count) if valores else None


def _contador_end(net, no):
    """Pacotes já processados pela função End do nó, lidos do kernel."""
    saida = net.get(no).cmd("ip -6 -s route show %s" % SID[no])
    m = re.search(r"packets (\d+)", saida)
    return int(m.group(1)) if m else 0


def _ping(net, origem, alvo, n=6, intervalo=0.3):
    """Executa ping6 e devolve (saida_bruta, perda_%, rtt_medio_ms)."""
    saida = net.get(origem).cmd("ping -6 -c %d -i %s -W 2 %s" % (n, intervalo, alvo))
    perda = None
    m = re.search(r"(\d+(?:\.\d+)?)% packet loss", saida)
    if m:
        perda = float(m.group(1))
    rtt = None
    m = re.search(r"= [\d.]+/([\d.]+)/", saida)
    if m:
        rtt = float(m.group(1))
    return saida, perda, rtt


def _captura(no, iface, arquivo, filtro="ip6"):
    """Sobe um tcpdump em background dentro do namespace do nó."""
    no.cmd("rm -f %s" % arquivo)
    no.cmd("tcpdump -i %s -s 0 -w %s '%s' > /dev/null 2>&1 &" % (iface, arquivo, filtro))


def _para_captura(no, iface):
    no.cmd("pkill -f 'tcpdump -i %s' 2>/dev/null" % iface)


def _le_proc_stat():
    """(total, ocioso) de jiffies acumulados, da primeira linha de /proc/stat."""
    with open("/proc/stat") as fp:
        campos = [int(x) for x in fp.readline().split()[1:]]
    # user nice system idle iowait irq softirq steal ...
    ocioso = campos[3] + (campos[4] if len(campos) > 4 else 0)
    return sum(campos), ocioso


def _uso_cpu(segundos=3):
    """Uso de CPU do host no intervalo, lido de /proc/stat.

    Deliberadamente SEM psutil: este script roda no Python do SISTEMA (é o único
    que enxerga o Mininet), e o psutil está instalado no venv do RYU. Importá-lo
    aqui devolveria ImportError e a medida viria vazia — foi exatamente o que
    aconteceu na primeira execução, que reportou "CPU sob carga: None%".
    /proc/stat não tem dependência alguma e serve aos dois interpretadores.
    """
    t0, i0 = _le_proc_stat()
    time.sleep(segundos)
    t1, i1 = _le_proc_stat()
    dt, di = t1 - t0, i1 - i0
    return {"cpu_percent": round(100.0 * (dt - di) / dt, 1) if dt else None,
            "janela_s": segundos, "nucleos": os.cpu_count(), "fonte": "/proc/stat"}


def gera_trafego(net, alvo=H2_IP, segundos=8, taxa="5M", json_saida=None):
    """Tráfego UDP de h1 para h2 com iperf3 sobre IPv6.

    NÃO se usa o atalho 'iperf h1 h2' do Mininet: ele monta a linha de comando
    com server.IP(), ou seja, o endereço IPv4. Aqui os hosts são exclusivamente
    IPv6 (ip=None), então o atalho falharia; e, mesmo que houvesse IPv4, tráfego
    IPv4 NÃO percorreria a política SRv6 — a medição nada diria sobre o SRv6.

    Parâmetros conforme o TCC, Seção 3.9.1: UDP, 5 Mbit/s, -i 1, -J.
    """
    h1, h2 = net.get("h1"), net.get("h2")
    h2.cmd("killall -9 iperf3 2>/dev/null")
    h2.cmd("iperf3 -s -1 -p 5201 > /dev/null 2>&1 &")
    time.sleep(1)
    cmd = "iperf3 -c %s -u -b %s -t %d -i 1 -p 5201 -J" % (alvo, taxa, segundos)
    saida = h1.cmd(cmd)
    h2.cmd("killall -9 iperf3 2>/dev/null")
    if json_saida:
        try:
            with open(json_saida, "w") as fp:
                fp.write(saida)
        except IOError:
            pass
    try:
        d = json.loads(saida)
        s = d["end"]["sum"]
        return {"mbits_por_s": round(s.get("bits_per_second", 0) / 1e6, 3),
                "jitter_ms": round(s.get("jitter_ms", 0), 4),
                "perda_pct": round(s.get("lost_percent", 0), 3),
                "pacotes": s.get("packets", 0)}
    except (ValueError, KeyError) as e:
        return {"erro": "iperf3 não devolveu JSON utilizável: %s" % e}


# --------------------------------------------------------------------------- #
# Manifesto — a costura entre o emulador e o controlador
# --------------------------------------------------------------------------- #
def escreve_manifesto(net):
    """Publica os PIDs dos namespaces para que a app RYU possa agir neles.

    ESTA É A COSTURA QUE A FASE F4 SUBSTITUI. Num ambiente real o controlador não
    tem PID de namespace para entrar; ele falaria com um agente rodando em cada
    nó. Aqui, como Mininet e RYU dividem a mesma máquina, o controlador usa
    `mnexec -a <pid>` — atalho legítimo para a PoC, e deliberadamente confinado a
    uma única função (`_instala_politica` em srv6_te_controller.py), de modo que
    trocá-lo por um agente REST mexa em um lugar só.
    """
    os.makedirs(os.path.dirname(MANIFESTO), exist_ok=True)
    dados = {
        "nos": {n: {"pid": net.get(n).pid} for n in ("h1", "r1", "r2", "h2")},
        "ingresso": {"h1": {"iface": "h1-eth0", "destino": H2_IP},
                     "h2": {"iface": "h2-eth0", "destino": H1_IP}},
        "sids": SID,
        # Next-hop IPv6 de cada nó de trânsito, por borda de ingresso: usado
        # quando o controlador instala rota IPv6 comum (TE_MODO=ipv6_*).
        "nexthop": {"r1": {"h1": R1_IP_A, "h2": R1_IP_B},
                    "r2": {"h1": R2_IP_A, "h2": R2_IP_B}},
        "atraso_ms": ATRASO_MS,
        "banda_mbit": {n: BANDA_MBIT for n in NOS_COM_BANDA},
        "fundo": {"destino": H2_FUNDO_IP, "porta": PORTA_FUNDO, "via": "r1"},
        # Grafo que a app RYU carrega no networkx. O peso é o atraso do enlace,
        # que é exatamente a métrica que a engenharia de tráfego quer minimizar.
        "grafo": {
            "enlaces": [["h1", "s1", 0], ["s1", "r1", ATRASO_MS["r1"]],
                        ["s1", "r2", ATRASO_MS["r2"]], ["r1", "s2", ATRASO_MS["r1"]],
                        ["r2", "s2", ATRASO_MS["r2"]], ["s2", "h2", 0]],
            "transito": ["r1", "r2"],
        },
    }
    with open(MANIFESTO, "w") as fp:
        json.dump(dados, fp, indent=2)
    info("*** Manifesto publicado em %s\n" % MANIFESTO)
    return dados


_PROCESSOS_TELEMETRIA = {}     # nó -> (subprocess.Popen, arquivo de log aberto)


def inicia_agentes_telemetria(net):
    """Lança um telemetry_agent.py por nó (TCC, Seção 3.8) — NO NAMESPACE
    RAIZ, não dentro do nó via `no.cmd()`.

    Isso parece contraintuitivo (o agente vai medir a CPU de r1, então por
    que não roda "dentro" de r1?), mas é a escolha certa por uma razão
    específica: o Mininet isola NAMESPACE DE REDE por host, mas NÃO isola
    namespace de PID — todo processo, em qualquer nó ou no namespace raiz,
    compartilha a mesma árvore de PIDs do kernel. Isso significa que
    `psutil.Process(pid)` enxerga o processo de r1 igualmente bem rodando
    do namespace raiz. Já o inverso NÃO vale para rede: `127.0.0.1` DENTRO
    do namespace de rede de r1 é um loopback diferente do `127.0.0.1` do
    namespace raiz, onde o coletor de telemetria do controlador escuta —
    um agente lançado via `no.cmd()` (dentro do namespace de r1) tentaria
    conectar nesse loopback isolado e cairia num "connection refused"
    silencioso para sempre. Isso foi testado e confirmado: com `no.cmd()`,
    nenhum agente conseguia conectar; rodando no namespace raiz, conecta.

    Consequência prática: usa subprocess.Popen (não no.cmd), e por isso
    também não depende de nenhum shell de nó estar de pé para funcionar.
    O agente precisa do python do venv (só ele tem psutil)."""
    if not os.path.exists(RYU_VENV_PYTHON):
        info("*** AVISO: python do venv nao encontrado em %s — telemetria "
             "real desligada, controlador usa fallback 0%%\n" % RYU_VENV_PYTHON)
        return
    for nome in NOS_COM_TELEMETRIA:
        no = net.get(nome)
        log_path = os.path.join(DIR_CAPTURAS, "telemetria_%s.log" % nome)
        log_fp = open(log_path, "wb")
        proc = subprocess.Popen(
            [RYU_VENV_PYTHON, AGENTE_TELEMETRIA,
             "--no", nome, "--pid", str(no.pid),
             "--coletor", "127.0.0.1:%d" % PORTA_TELEMETRIA,
             "--banda-mbit", str(BANDA_MBIT if nome in NOS_COM_BANDA else 0)],
            stdout=log_fp, stderr=subprocess.STDOUT)
        _PROCESSOS_TELEMETRIA[nome] = (proc, log_fp)
    info("*** Agentes de telemetria lançados (namespace raiz): %s\n"
         % ", ".join(NOS_COM_TELEMETRIA))


def para_agentes_telemetria(_net=None):
    """Encerra os agentes ANTES de net.stop(). Como agora rodam no namespace
    raiz (ver inicia_agentes_telemetria), são processos Python comuns deste
    mesmo script — encerra via Popen.terminate(), não via pkill dentro de
    um nó. Tolerante a já ter sido chamada (idempotente)."""
    for nome, (proc, log_fp) in list(_PROCESSOS_TELEMETRIA.items()):
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            log_fp.close()
        except Exception:
            pass
    _PROCESSOS_TELEMETRIA.clear()


def _rest(caminho, payload=None, timeout=8):
    """Chamada REST ao controlador RYU. Devolve (ok, objeto_ou_erro)."""
    import urllib.error
    import urllib.request
    url = REST_RYU + caminho
    dados = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=dados,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode())
    except Exception as e:            # urllib levanta famílias distintas
        return False, str(e)


# --------------------------------------------------------------------------- #
# BLOCO B — plano de controle SDN
# --------------------------------------------------------------------------- #
def bloco_b(net):
    print("\n[B] PLANO DE CONTROLE — OpenFlow 1.3 / RYU")
    r = {}
    for sw in ("s1", "s2"):
        ctrl = subprocess.getoutput("ovs-vsctl get-controller %s" % sw).strip()
        conectado = subprocess.getoutput(
            "ovs-vsctl get Controller %s is_connected" % sw).strip()
        fluxos = subprocess.getoutput(
            "ovs-ofctl -O OpenFlow13 dump-flows %s 2>/dev/null | grep -c cookie" % sw).strip()
        versao = subprocess.getoutput(
            "ovs-vsctl get bridge %s protocols" % sw).strip()
        r[sw] = {"controlador": ctrl, "conectado": conectado,
                 "regras_instaladas": int(fluxos or 0), "protocolos": versao}
        print("    %s  controlador=%-22s conectado=%-6s regras=%s  %s"
              % (sw, ctrl or "(nenhum)", conectado, fluxos, versao))
    r["veredito"] = ("OK" if all(r[s]["conectado"] == "true" for s in ("s1", "s2"))
                     else "FALHA")
    print("    Veredito: %s" % r["veredito"])
    RESULTADOS["blocos"]["B_plano_controle"] = r
    return r


# --------------------------------------------------------------------------- #
# BLOCO C — plano de dados SRv6
# --------------------------------------------------------------------------- #
def bloco_c(net):
    print("\n[C] PLANO DE DADOS SRv6 — SRH, custo de cabeçalho e função End")
    r = {}
    h1 = net.get("h1")

    # -- C1/C3 linha de base sem SRH -------------------------------------- #
    remove_srv6(net)
    pcap_base = os.path.join(DIR_CAPTURAS, "c_ipv6_base.pcap")
    _captura(h1, "h1-eth0", pcap_base)
    time.sleep(1.2)
    _, perda_base, rtt_base = _ping(net, "h1", H2_IP, n=6)
    time.sleep(0.6)
    _para_captura(h1, "h1-eth0")
    time.sleep(0.4)
    tam_base = _tamanhos_pcap(pcap_base)

    # -- C1/C3 com SRv6 via r1 -------------------------------------------- #
    aplica_srv6(net, "r1")
    pcap_srv6 = os.path.join(DIR_CAPTURAS, "c_srv6_r1.pcap")
    _captura(h1, "h1-eth0", pcap_srv6)
    time.sleep(1.2)
    _, perda_srv6, rtt_srv6 = _ping(net, "h1", H2_IP, n=6)
    time.sleep(0.6)
    _para_captura(h1, "h1-eth0")
    time.sleep(0.4)
    tam_srv6 = _tamanhos_pcap(pcap_srv6)

    m_base, m_srv6 = _moda(tam_base), _moda(tam_srv6)
    acrescimo = (m_srv6 - m_base) if (m_base and m_srv6) else None

    r["conectividade"] = {"ipv6_perda_pct": perda_base, "srv6_perda_pct": perda_srv6}
    r["custo_cabecalho"] = {
        "quadro_ipv6_bytes": m_base,
        "quadro_srv6_bytes": m_srv6,
        "acrescimo_medido_bytes": acrescimo,
        "acrescimo_esperado_bytes": 80,
        "composicao": "40 (IPv6 externo) + 8 (SRH base) + 16x2 (dois segmentos)",
        "veredito": "OK" if acrescimo == 80 else "DIVERGENTE",
    }
    print("    Conectividade IPv6 puro .... perda %s%%" % perda_base)
    print("    Conectividade SRv6 ......... perda %s%%" % perda_srv6)
    print("    Quadro IPv6 / SRv6 ......... %s / %s bytes" % (m_base, m_srv6))
    print("    Acréscimo do SRH ........... %s bytes (esperado 80) -> %s"
          % (acrescimo, r["custo_cabecalho"]["veredito"]))

    # -- C2 dissecação do SRH com tshark ---------------------------------- #
    if subprocess.getoutput("command -v tshark").strip():
        # ATENÇÃO ao -c do tshark: ele limita os pacotes LIDOS, não os exibidos.
        # Combinado com -Y, "-c 1" lê só o quadro 1 e, se ele não casar com o
        # filtro, a saída vem vazia — parecendo ausência de SRH onde há 18.
        # Por isso o corte é feito depois, com head.
        def _tsh(pcap, extra=""):
            return subprocess.getoutput(
                "tshark -r %s -Y 'ipv6.routing.type == 4' %s 2>/dev/null" % (pcap, extra))

        n_srh = len([l for l in _tsh(pcap_srv6).splitlines() if l.strip()])
        n_base = len([l for l in _tsh(pcap_base).splitlines() if l.strip()])
        campos_fmt = ("-T fields -e ipv6.routing.type -e ipv6.routing.segleft "
                      "-e ipv6.routing.srh.last_entry -e ipv6.routing.srh.addr")
        linhas = [l for l in _tsh(pcap_srv6, campos_fmt).splitlines() if l.strip()]
        primeiro = linhas[0] if linhas else ""

        # Segments Left observados no MESMO ponto de captura. Ver 1 e 0 juntos é
        # a prova de que a função End decrementou o SL em trânsito: sai de h1
        # com SL=1 e volta, após o processamento em r1, com SL=0.
        sl = sorted({l.split("\t")[1] for l in linhas if len(l.split("\t")) > 1})

        r["srh_tshark"] = {
            "filtro": "ipv6.routing.type == 4",
            "pacotes_com_srh_no_srv6": n_srh,
            "pacotes_com_srh_na_linha_de_base": n_base,
            "campos_type_segleft_lastentry_segmentos": primeiro,
            "segments_left_observados": sl,
            "decremento_do_sl_observado": len(sl) > 1,
            "veredito": "OK" if n_srh > 0 and n_base == 0 else "FALHA",
        }
        print("    SRH (ipv6.routing.type==4) . %d pacotes no SRv6, %d na linha de base"
              % (n_srh, n_base))
        print("    Campos type/SL/lastentry ... %s" % primeiro)
        print("    Segments Left observados ... %s  (dois valores = End decrementou)" % sl)
    else:
        r["srh_tshark"] = {"veredito": "INDISPONIVEL", "motivo": "tshark ausente"}
        print("    tshark ausente — dissecação campo-a-campo do SRH não executada")

    RESULTADOS["blocos"]["C_plano_dados_srv6"] = r
    return r


# --------------------------------------------------------------------------- #
# BLOCO D — engenharia de tráfego: o núcleo da PoC
# --------------------------------------------------------------------------- #
def bloco_d(net):
    print("\n[D] ENGENHARIA DE TRÁFEGO — a lista de segmentos escolhe o caminho?")
    r = {"caminhos": {}}
    r1, r2 = net.get("r1"), net.get("r2")

    for via in ("r1", "r2"):
        outro = "r2" if via == "r1" else "r1"
        print("\n    --- Forçando o caminho por %s (SID %s) ---" % (via, SID[via]))
        aplica_srv6(net, via)
        time.sleep(0.5)

        # Contadores ANTES: eles acumulam, então só o delta significa alguma coisa.
        antes = {"r1": _contador_end(net, "r1"), "r2": _contador_end(net, "r2")}

        # Prova independente por captura, nas duas saídas de trânsito ao mesmo
        # tempo. Filtro ip6[6]==43: o Next Header do IPv6 externo vale 43
        # (Routing Header), ou seja, conta SOMENTE pacotes que carregam SRH.
        pcap = {n: os.path.join(DIR_CAPTURAS, "d_%s_via_%s.pcap" % (n, via))
                for n in ("r1", "r2")}
        _captura(r1, "r1-eth1", pcap["r1"], "ip6[6] == 43")
        _captura(r2, "r2-eth1", pcap["r2"], "ip6[6] == 43")
        time.sleep(1.2)

        _, perda, rtt = _ping(net, "h1", H2_IP, n=8)

        time.sleep(0.6)
        _para_captura(r1, "r1-eth1")
        _para_captura(r2, "r2-eth1")
        time.sleep(0.4)

        depois = {"r1": _contador_end(net, "r1"), "r2": _contador_end(net, "r2")}
        delta = {n: depois[n] - antes[n] for n in ("r1", "r2")}
        srh_vistos = {n: len(_tamanhos_pcap(pcap[n])) for n in ("r1", "r2")}

        esperado = rtt_esperado(via)
        r["caminhos"][via] = {
            "sid": SID[via],
            "delta_contador_end": delta,
            "pacotes_com_srh_capturados": srh_vistos,
            "perda_pct": perda,
            "rtt_medio_ms": rtt,
            "rtt_esperado_ms": esperado,
            "desvio_pct": (round(abs(rtt - esperado) / esperado * 100, 1)
                           if rtt else None),
            # O teste é falseável: exige movimento no nó escolhido E imobilidade
            # no outro. Só uma das duas condições não provaria direcionamento.
            "veredito_steering": ("OK" if delta[via] > 0 and delta[outro] == 0
                                  else "FALHA"),
        }
        print("        Contador End (delta) ... r1=%d  r2=%d" % (delta["r1"], delta["r2"]))
        print("        Pacotes com SRH ........ r1=%d  r2=%d"
              % (srh_vistos["r1"], srh_vistos["r2"]))
        print("        Perda .................. %s%%" % perda)
        print("        RTT médio .............. %s ms (esperado ~%.0f ms)" % (rtt, esperado))
        print("        Steering ............... %s"
              % r["caminhos"][via]["veredito_steering"])

    # -- D2 diferenciação mensurável -------------------------------------- #
    rtt1 = r["caminhos"]["r1"]["rtt_medio_ms"]
    rtt2 = r["caminhos"]["r2"]["rtt_medio_ms"]
    dif = (rtt2 - rtt1) if (rtt1 and rtt2) else None
    dif_esp = rtt_esperado("r2") - rtt_esperado("r1")
    r["diferenciacao"] = {
        "rtt_via_r1_ms": rtt1, "rtt_via_r2_ms": rtt2,
        "diferenca_ms": round(dif, 3) if dif else None,
        "diferenca_esperada_ms": dif_esp,
        # Margem generosa (40%) porque 2 vCPU com netem introduzem jitter real.
        "veredito": ("OK" if dif and abs(dif - dif_esp) <= 0.4 * dif_esp else "DIVERGENTE"),
    }
    print("\n    Diferenciação: RTT(r2) - RTT(r1) = %s ms (esperado %s ms) -> %s"
          % (r["diferenciacao"]["diferenca_ms"], dif_esp, r["diferenciacao"]["veredito"]))

    # -- D3 vazão sob SRv6, com o plano de controle ativo ------------------ #
    aplica_srv6(net, "r1")
    print("    Medindo vazão com iperf3 (UDP 5 Mbit/s sobre SRv6 via r1)...")
    r["vazao_srv6_via_r1"] = gera_trafego(
        net, json_saida=os.path.join(DIR_CAPTURAS, "d_iperf3_srv6_r1.json"))
    print("        %s" % r["vazao_srv6_via_r1"])

    RESULTADOS["blocos"]["D_engenharia_trafego"] = r
    return r


# --------------------------------------------------------------------------- #
# BLOCO E — laço de controle fechado
# --------------------------------------------------------------------------- #
def _mede_decisao(net, esperado):
    """Verifica UMA decisão do controlador, do plano de controle ao de dados.

    A prova principal são os contadores da função End: eles contam pacotes de
    verdade, no kernel do nó de trânsito, e não dependem de temporização. O RTT
    entra como corroboração, não como critério — numa VM de 2 vCPU com netem ele
    é a grandeza mais ruidosa disponível, e uma execução anterior desta PoC
    reprovou o bloco por causa disso, com as duas decisões corretas.
    """
    outro = "r2" if esperado == "r1" else "r1"
    antes = {n: _contador_end(net, n) for n in ("r1", "r2")}
    _, perda, rtt = _ping(net, "h1", H2_IP, n=8)
    depois = {n: _contador_end(net, n) for n in ("r1", "r2")}
    delta = {n: depois[n] - antes[n] for n in ("r1", "r2")}
    return {
        "instalado_no_kernel": politica_atual(net),
        "delta_contador_end": delta,
        "trafego_apenas_no_escolhido": delta[esperado] > 0 and delta[outro] == 0,
        "perda_pct": perda,
        "rtt_ms": rtt,
    }


def bloco_e(net):
    print("\n[E] LAÇO DE CONTROLE FECHADO — o RYU decide e instala a política")
    r = {}
    # O bloco D termina com 8 s de iperf3; sem esta pausa a primeira medida de
    # RTT aqui herda o rescaldo (filas do netem, CPU ainda ocupada).
    time.sleep(3)

    ok, estado = _rest("/srv6/estado")
    if not ok:
        print("    REST do RYU indisponível (%s)" % estado)
        print("    -> a app srv6_te_controller.py não está carregada.")
        r["veredito"] = "FALHA"
        r["motivo"] = "REST indisponível: %s" % estado
        RESULTADOS["blocos"]["E_laco_controle"] = r
        return r
    print("    Controlador respondeu: %s" % json.dumps(estado, ensure_ascii=False))
    r["estado_inicial"] = estado

    # -- E1/E3 primeira decisão: o Dijkstra deve preferir r1 (peso 5 < 10) - #
    remove_srv6(net)
    ok, dec1 = _rest("/srv6/recalcula", {})
    if not ok:
        r["veredito"] = "FALHA"; r["motivo"] = dec1
        RESULTADOS["blocos"]["E_laco_controle"] = r
        return r
    time.sleep(0.6)
    m1 = _mede_decisao(net, "r1")
    r["decisao_1"] = {"escolhido_pelo_ryu": dec1.get("escolhido"),
                      "caminho": dec1.get("caminho"), "custo": dec1.get("custo"), **m1}
    print("    Decisão 1: RYU escolheu %s | kernel confirma '%s' | End r1=%d r2=%d | RTT %s ms"
          % (dec1.get("escolhido"), m1["instalado_no_kernel"],
             m1["delta_contador_end"]["r1"], m1["delta_contador_end"]["r2"], m1["rtt_ms"]))

    # -- E2/E4 congestionamento simulado: encarece r1 e recalcula ---------- #
    print("    Simulando congestionamento: peso de r1 -> 100 via REST")
    ok, dec2 = _rest("/srv6/peso", {"no": "r1", "peso": 100})
    if not ok:
        r["veredito"] = "FALHA"; r["motivo"] = dec2
        RESULTADOS["blocos"]["E_laco_controle"] = r
        return r
    time.sleep(0.6)
    m2 = _mede_decisao(net, "r2")
    r["decisao_2"] = {"escolhido_pelo_ryu": dec2.get("escolhido"),
                      "caminho": dec2.get("caminho"), "custo": dec2.get("custo"), **m2}
    print("    Decisão 2: RYU escolheu %s | kernel confirma '%s' | End r1=%d r2=%d | RTT %s ms"
          % (dec2.get("escolhido"), m2["instalado_no_kernel"],
             m2["delta_contador_end"]["r1"], m2["delta_contador_end"]["r2"], m2["rtt_ms"]))

    # A cadeia inteira tem de fechar, elo por elo:
    #   1. o RYU decidiu o nó certo a partir do grafo ponderado;
    #   2. a rota seg6 correspondente apareceu no kernel do nó de ingresso;
    #   3. os pacotes percorreram FISICAMENTE o nó escolhido, e só ele;
    #   4. o RTT medido subiu ao migrar para o caminho mais caro.
    # O elo 3 é o decisivo; o 4 corrobora. Testar direção do RTT (rtt2 > rtt1) e
    # não uma razão fixa: a razão depende de sobrecarga de emulação, que varia.
    r["rtt_diferenca_ms"] = (round(m2["rtt_ms"] - m1["rtt_ms"], 3)
                             if m1["rtt_ms"] and m2["rtt_ms"] else None)
    r["rtt_diferenca_esperada_ms"] = rtt_esperado("r2") - rtt_esperado("r1")
    cadeia_ok = (dec1.get("escolhido") == "r1" and m1["instalado_no_kernel"] == "r1"
                 and m1["trafego_apenas_no_escolhido"]
                 and dec2.get("escolhido") == "r2" and m2["instalado_no_kernel"] == "r2"
                 and m2["trafego_apenas_no_escolhido"]
                 and r["rtt_diferenca_ms"] is not None and r["rtt_diferenca_ms"] > 0)
    r["veredito"] = "OK" if cadeia_ok else "FALHA"
    r["cadeia_verificada"] = ("decisão do RYU (Dijkstra) -> rota seg6 no kernel do "
                              "ingresso -> pacotes contados APENAS no nó escolhido "
                              "-> RTT acompanha a troca de caminho")
    print("    Tráfego só no nó escolhido: decisão 1 %s | decisão 2 %s"
          % (m1["trafego_apenas_no_escolhido"], m2["trafego_apenas_no_escolhido"]))
    print("    RTT subiu %s ms ao migrar para o caminho mais caro (esperado ~%s ms)"
          % (r["rtt_diferenca_ms"], r["rtt_diferenca_esperada_ms"]))
    print("    Veredito do laço fechado: %s" % r["veredito"])

    # Restaura o peso para não contaminar execuções seguintes.
    _rest("/srv6/peso", {"no": "r1", "peso": ATRASO_MS["r1"]})
    RESULTADOS["blocos"]["E_laco_controle"] = r
    return r


# --------------------------------------------------------------------------- #
# BLOCO F — viabilidade operacional
# --------------------------------------------------------------------------- #
def bloco_f(net):
    print("\n[F] VIABILIDADE OPERACIONAL")
    r = {"cpu_em_repouso": _uso_cpu(2)}
    aplica_srv6(net, "r1")
    print("    Medindo CPU sob carga (iperf3 + netem + OVS + RYU)...")
    net.get("h2").cmd("killall -9 iperf3 2>/dev/null")
    net.get("h2").cmd("iperf3 -s -1 -p 5202 > /dev/null 2>&1 &")
    time.sleep(0.8)
    net.get("h1").cmd("iperf3 -c %s -u -b 5M -t 6 -p 5202 > /dev/null 2>&1 &" % H2_IP)
    r["cpu_sob_carga"] = _uso_cpu(4)
    time.sleep(2)
    net.get("h2").cmd("killall -9 iperf3 2>/dev/null")

    ncpu = os.cpu_count()
    pico = r["cpu_sob_carga"].get("cpu_percent")
    r["vcpu_disponiveis"] = ncpu
    r["vcpu_exigidos_pelo_tcc"] = 4
    r["veredito"] = "OK" if (pico is None or pico < 85) else "ATENCAO"
    r["observacao"] = (
        "A PoC roda folgada em %d vCPU. Os cenários E3-E5 do TCC, que carregam a "
        "CPU de propósito para disparar o recálculo de rota, precisam dos 4 vCPU "
        "especificados — com 2, a carga sintética competiria com o próprio netem "
        "e com o OVS, contaminando a medida." % ncpu)
    print("    vCPU: %d (TCC exige 4) | CPU sob carga: %s%%" % (ncpu, pico))
    print("    %s" % r["observacao"])
    RESULTADOS["blocos"]["F_viabilidade"] = r
    return r


# --------------------------------------------------------------------------- #
def auto_teste(net):
    linha = "=" * 74
    print("\n" + linha)
    print("PoC — TOPOLOGIA EM DIAMANTE SOBRE SRv6 + SDN")
    print(linha)

    RESULTADOS["gerado_em"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    RESULTADOS["parametros"] = {
        "sids": SID, "locators": LOCATOR, "atraso_ms_por_enlace": ATRASO_MS,
        "banda_mbit": BANDA_MBIT,
        "rtt_esperado_ms": {n: rtt_esperado(n) for n in ATRASO_MS},
        "enderecos": {"h1": H1_IP, "r1": [R1_IP_A, R1_IP_B],
                      "r2": [R2_IP_A, R2_IP_B], "h2": H2_IP},
    }

    bloco_b(net)
    bloco_c(net)
    bloco_d(net)
    bloco_e(net)
    bloco_f(net)

    # ------------------------------------------------------------ laudo ---- #
    vereditos = {
        "B_plano_controle": RESULTADOS["blocos"].get("B_plano_controle", {}).get("veredito"),
        "C_custo_srh": RESULTADOS["blocos"].get("C_plano_dados_srv6", {})
                                  .get("custo_cabecalho", {}).get("veredito"),
        "C_srh_presente": RESULTADOS["blocos"].get("C_plano_dados_srv6", {})
                                     .get("srh_tshark", {}).get("veredito"),
        "D_steering_r1": RESULTADOS["blocos"].get("D_engenharia_trafego", {})
                                    .get("caminhos", {}).get("r1", {}).get("veredito_steering"),
        "D_steering_r2": RESULTADOS["blocos"].get("D_engenharia_trafego", {})
                                    .get("caminhos", {}).get("r2", {}).get("veredito_steering"),
        "D_diferenciacao": RESULTADOS["blocos"].get("D_engenharia_trafego", {})
                                      .get("diferenciacao", {}).get("veredito"),
        "E_laco_controle": RESULTADOS["blocos"].get("E_laco_controle", {}).get("veredito"),
        "F_viabilidade": RESULTADOS["blocos"].get("F_viabilidade", {}).get("veredito"),
    }
    RESULTADOS["vereditos"] = vereditos
    # Critérios de aceite: sem D e E aprovados, a premissa central do TCC não
    # está demonstrada, por mais que o resto funcione.
    criticos = ("D_steering_r1", "D_steering_r2", "E_laco_controle")
    RESULTADOS["poc_aprovada"] = all(vereditos.get(k) == "OK" for k in criticos)

    print("\n" + linha)
    print("LAUDO")
    print(linha)
    for k, v in vereditos.items():
        print("   %-22s %s" % (k, v))
    print("\n   PoC APROVADA: %s" % ("SIM" if RESULTADOS["poc_aprovada"] else "NÃO"))
    print(linha)

    destino = os.path.join(DIR_CAPTURAS, "resultados.json")
    with open(destino, "w") as fp:
        json.dump(RESULTADOS, fp, indent=2, ensure_ascii=False)
    print("Resultados: %s\n" % destino)


# --------------------------------------------------------------------------- #
class CLIDiamante(CLI):
    """CLI do Mininet com os comandos deste laboratório."""

    def do_via(self, linha):
        "via r1|r2  — força o caminho pelo nó de trânsito indicado"
        alvo = linha.strip()
        if alvo in SID:
            aplica_srv6(self.mn, alvo)
        else:
            print("uso: via r1|r2")

    def do_srv6off(self, _l):
        "Volta ao encaminhamento IPv6 puro (linha de base)"
        remove_srv6(self.mn)

    def do_politica(self, _l):
        "Mostra a política SRv6 em vigor no ingresso"
        print("Caminho em vigor: %s" % politica_atual(self.mn))
        print(self.mn.get("h1").cmd("ip -6 route show %s" % H2_IP).strip())

    def do_contadores(self, _l):
        "Contadores da função End nos dois nós de trânsito"
        for n in ("r1", "r2"):
            print("%s: %d pacotes" % (n, _contador_end(self.mn, n)))

    def do_rotas(self, _l):
        "Tabelas de rotas IPv6 de todos os nós"
        for nome in ("h1", "r1", "r2", "h2"):
            print("\n===== %s =====" % nome)
            print(self.mn.get(nome).cmd("ip -6 route show").strip())

    def do_testar(self, _l):
        "Ping de h1 a h2 no caminho em vigor"
        _, perda, rtt = _ping(self.mn, "h1", H2_IP, n=6)
        print("caminho=%s  perda=%s%%  rtt=%s ms" % (politica_atual(self.mn), perda, rtt))

    def do_carga(self, linha):
        ("carga r1|r2|h1|h2 [pct] [pin]  — carga sintética de CPU no nó. Com pct usa "
         "`stress-ng --cpu 1 --cpu-load pct` (Tabela 3.9.2: ≈90%); sem pct usa "
         "`yes` (~99%). 'pin' prende a carga ao CPU 0 com taskset (experimento X4).")
        partes = linha.split()
        if not partes or partes[0] not in NOS_COM_TELEMETRIA:
            print("uso: carga r1|r2|h1|h2 [pct] [pin]")
            return
        no, pct = partes[0], (partes[1] if len(partes) > 1 and partes[1] != "pin" else None)
        pin = "pin" in partes[1:]
        prefixo = "taskset -c 0 " if pin else ""
        n = self.mn.get(no)
        if pct and n.cmd("which stress-ng").strip():
            n.cmd("%sstress-ng --cpu 1 --cpu-load %s -t 3600 > /dev/null 2>&1 &" % (prefixo, pct))
            print("carga stress-ng %s%% iniciada em %s%s" % (pct, no, " (pin CPU0)" if pin else ""))
        else:
            if pct:
                print("AVISO: stress-ng ausente; usando `yes` (~99%%) em vez de %s%%" % pct)
            n.cmd("%syes > /dev/null &" % prefixo)
            print("carga `yes` iniciada em %s" % no)

    def do_descarga(self, linha):
        "descarga r1|r2|h1|h2  — remove a carga sintética do nó"
        no = linha.strip()
        if no not in NOS_COM_TELEMETRIA:
            print("uso: descarga r1|r2|h1|h2")
            return
        self.mn.get(no).cmd("pkill -x stress-ng; pkill -x yes")
        print("carga removida de %s" % no)

    def do_fluxo(self, linha):
        ("fluxo SEGUNDOS ARQ_CLIENTE [TAXA] [ARQ_SERVIDOR]  — iperf3 UDP h1->h2 em "
         "BACKGROUND, -i 1 -J (Seção 3.9.1). Taxa padrão 5M. O JSON do SERVIDOR traz "
         "perda e jitter por intervalo (o do cliente não traz).")
        p = linha.split()
        if len(p) < 2:
            print("uso: fluxo SEGUNDOS ARQ_CLIENTE [TAXA] [ARQ_SERVIDOR]")
            return
        seg, arq = p[0], p[1]
        taxa = p[2] if len(p) > 2 else "5M"
        arq_srv = p[3] if len(p) > 3 else "/dev/null"
        h1, h2 = self.mn.get("h1"), self.mn.get("h2")
        h2.cmd("killall -9 iperf3 2>/dev/null")
        h2.cmd("iperf3 -s -1 -p 5201 -J --logfile %s > /dev/null 2>&1 &" % arq_srv)
        time.sleep(1)
        h1.cmd("iperf3 -c %s -u -b %s -t %s -i 1 -p 5201 -J > %s 2>&1 &" % (H2_IP, taxa, seg, arq))
        print("iperf3 iniciado (%ss, %s) -> %s | servidor -> %s" % (seg, taxa, arq, arq_srv))

    def do_fundo(self, linha):
        ("fundo SEGUNDOS TAXA ARQ_CLIENTE [ARQ_SERVIDOR]  — tráfego de FUNDO h1->alias de h2 "
         "(iperf3 UDP, porta %d) PRESO em r1 pela rota do /64 (v2). Satura o nó por "
         "tráfego real, sem CPU sintética. Ex.: fundo 40 8.5M /tmp/fundo.json" % PORTA_FUNDO)
        p = linha.split()
        if len(p) < 3:
            print("uso: fundo SEGUNDOS TAXA ARQ_CLIENTE [ARQ_SERVIDOR]")
            return
        seg, taxa, arq = p[0], p[1], p[2]
        arq_srv = p[3] if len(p) > 3 else "/dev/null"
        h1, h2 = self.mn.get("h1"), self.mn.get("h2")
        h2.cmd("pkill -f 'iperf3 -s.*-p %d' 2>/dev/null" % PORTA_FUNDO)
        h2.cmd("iperf3 -s -1 -B %s -p %d -J --logfile %s > /dev/null 2>&1 &"
               % (H2_FUNDO_IP, PORTA_FUNDO, arq_srv))
        time.sleep(1)
        h1.cmd("iperf3 -c %s -u -b %s -t %s -i 1 -p %d -J > %s 2>&1 &"
               % (H2_FUNDO_IP, taxa, seg, PORTA_FUNDO, arq))
        print("fundo iniciado (%ss, %s) em r1 -> %s | servidor -> %s" % (seg, taxa, arq, arq_srv))

    def do_fimfundo(self, _l):
        "fimfundo  — encerra o tráfego de fundo (iperf3 na porta %d)" % PORTA_FUNDO
        for n in ("h1", "h2"):
            self.mn.get(n).cmd("pkill -f 'iperf3.*-p %d' 2>/dev/null" % PORTA_FUNDO)
        print("fundo encerrado")

    def do_rtt(self, linha):
        "rtt SEGUNDOS ARQUIVO  — ping6 h1->h2 a cada 0,2 s em BACKGROUND (RTT, Seção 3.10)"
        p = linha.split()
        if len(p) < 2:
            print("uso: rtt SEGUNDOS ARQUIVO")
            return
        n = int(float(p[0]) / 0.2)
        self.mn.get("h1").cmd("ping -6 -i 0.2 -c %d -D %s > %s 2>&1 &" % (n, H2_IP, p[1]))
        print("ping6 iniciado (%d pacotes) -> %s" % (n, p[1]))

    def do_fim(self, _l):
        "Encerra iperf3 e cargas remanescentes"
        self.mn.get("h2").cmd("killall -9 iperf3 2>/dev/null")
        self.mn.get("h1").cmd("killall -9 iperf3 2>/dev/null")
        for n in NOS_COM_TELEMETRIA:
            self.mn.get(n).cmd("pkill -x stress-ng; pkill -x yes")

    def do_telemetria(self, _l):
        "Consulta a telemetria e a decisão de caminho em vigor no controlador"
        ok, resp = _rest("/srv6/estado")
        if not ok:
            print("erro consultando o controlador: %s" % resp)
            return
        print("telemetria por no:")
        for no, amostra in (resp.get("telemetria") or {}).items():
            print("  %-4s cpu=%s%%  n_processos=%s" %
                  (no, amostra.get("cpu_percent"), amostra.get("n_processos")))
        decisao = resp.get("melhor_caminho_agora") or {}
        print("caminho escolhido agora: %s (custo=%s)" %
              (decisao.get("escolhido"), decisao.get("custo")))


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="PoC SRv6 + SDN — topologia em diamante")
    ap.add_argument("--auto-teste", action="store_true",
                    help="roda a bateria completa e grava capturas/resultados.json")
    ap.add_argument("--ip-controlador", default="127.0.0.1")
    ap.add_argument("--porta-controlador", type=int, default=6653,
                    help="porta OpenFlow do RYU (padrão 6653; a F0 usava 6633)")
    ap.add_argument("--modo", default="srv6", choices=("srv6", "ipv6"),
                    help="srv6: política inicial `encap seg6`; ipv6: só rota IPv6 "
                         "comum (F2 / cenários E1-E3)")
    ap.add_argument("--via", default="r1", choices=("r1", "r2"),
                    help="caminho inicial da política SRv6")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("Erro: o Mininet exige root. Use: sudo /usr/bin/python3 %s"
              % os.path.basename(__file__))
        return 1
    try:
        import mininet  # noqa: F401
    except ImportError:
        print("Erro: Mininet não encontrado. Este script precisa do python do\n"
              "sistema (/usr/bin/python3), não do python do venv do RYU.")
        return 1

    os.makedirs(DIR_CAPTURAS, exist_ok=True)
    setLogLevel("info")

    net = Mininet(topo=TopoDiamante(), switch=OVSSwitch, link=TCLink,
                  controller=None, autoSetMacs=True, cleanup=True)
    net.addController(RemoteController("c0", ip=args.ip_controlador,
                                       port=args.porta_controlador))
    net.start()

    info("*** Aguardando os switches conectarem ao controlador\n")
    conectados = net.waitConnected(timeout=25)
    if not conectados:
        info("*** AVISO: switches não conectaram ao RYU em %s:%d — o bloco B vai "
             "reprovar, e é essa a informação que interessa registrar.\n"
             % (args.ip_controlador, args.porta_controlador))

    configura_rede(net)
    escreve_manifesto(net)
    inicia_agentes_telemetria(net)

    # Aquecimento do NDP. Resolve a vizinhança dos DOIS roteadores, nos DOIS
    # sentidos, antes de qualquer medição — senão a primeira amostra de RTT
    # carregaria o tempo de resolução.
    #
    # Aquecer os dois é também uma questão de honestidade experimental: aquecer
    # só um caminho o deixaria com regras L2 já instaladas nos switches enquanto
    # o outro ainda dependeria de packet_in. Os dois caminhos partem do mesmo
    # estado, e a comparação mede o SRv6 — não a ordem em que foram testados.
    info("*** Aquecendo o NDP dos dois caminhos\n")
    for alvo in (R1_IP_A, R2_IP_A, H2_IP):
        net.get("h1").cmd("ping -6 -c 2 -W 1 %s > /dev/null 2>&1" % alvo)
    for alvo in (R1_IP_B, R2_IP_B, H1_IP):
        net.get("h2").cmd("ping -6 -c 2 -W 1 %s > /dev/null 2>&1" % alvo)

    codigo = 0
    try:
        if args.auto_teste:
            try:
                auto_teste(net)
                codigo = 0 if RESULTADOS.get("poc_aprovada") else 2
            except Exception as e:
                import traceback
                traceback.print_exc()
                RESULTADOS["erro_fatal"] = str(e)
                with open(os.path.join(DIR_CAPTURAS, "resultados.json"), "w") as fp:
                    json.dump(RESULTADOS, fp, indent=2, ensure_ascii=False)
                codigo = 1
        else:
            if args.modo == "srv6":
                aplica_srv6(net, args.via)
            print("\nComandos próprios: via r1|r2, srv6off, politica, contadores, rotas, "
                  "testar, carga <no>, telemetria\n")
            CLIDiamante(net)
    finally:
        # Precisa rodar ANTES de net.stop(): depois que o Mininet derruba os
        # namespaces, não há mais como mandar um comando 'de dentro' do nó
        # para matar o agente — ele ficaria órfão tentando reconectar.
        para_agentes_telemetria(net)

    net.stop()
    return codigo


if __name__ == "__main__":
    sys.exit(main())
