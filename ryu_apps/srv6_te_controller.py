# -*- coding: utf-8 -*-
"""
=============================================================================
 srv6_te_controller.py — Controlador SDN de engenharia de tráfego com SRv6.

 TCC: "Engenharia de Tráfego em Redes de Computadores utilizando Segment
       Routing e SDN". Esta app é o embrião das fases F1 e F4 do roadmap.

 O PROBLEMA ARQUITETURAL QUE ESTA APP RESOLVE
 -------------------------------------------------------------------------
 O OpenFlow 1.3 não sabe falar de SRv6. Não é limitação do RYU nem do OVS: a
 tabela de campos OXM do OF 1.3 tem 122 campos e NENHUM descreve o Segment
 Routing Header (verificável com preflight.sh, checagem B3). Logo, o
 controlador NÃO pode instalar uma política SRv6 por OpenFlow.

 A consequência é uma arquitetura de DOIS canais descendentes, e é isso que a
 PoC precisa validar:

     RYU --[canal 1: OpenFlow 1.3]--> OVS (s1, s2)   regras L2, aprendizado MAC
     RYU --[canal 2: fora de banda]-> nó de ingresso  rota `encap seg6`

 O canal 2 aqui é `mnexec -a <pid>`, porque Mininet e RYU dividem a máquina.
 TODO o acoplamento com o emulador está confinado a `_instala_politica()`:
 numa rede real, aquela única função vira uma chamada a um agente rodando no
 nó (a fase F4 do roadmap já prevê agentes psutil por nó). Nada mais na app
 sabe que existe um emulador.

 O laço de controle demonstrado:
     grafo ponderado (networkx) -> Dijkstra -> lista de segmentos -> instalação
     -> o RTT medido no plano de dados muda em consequência da decisão.

 Uso:
   ryu-manager --observe-links ryu_apps/srv6_te_controller.py
 REST:
   GET  /srv6/estado      estado do grafo e da política em vigor
   POST /srv6/recalcula   {}                     recalcula e instala
   POST /srv6/peso        {"no":"r1","peso":100} muda o peso e recalcula
=============================================================================
"""

import json
import os
import subprocess
import time

import networkx as nx
from ryu.app.wsgi import ControllerBase, Response, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ether_types, ethernet
from ryu.ofproto import ofproto_v1_3

MANIFESTO = "/tmp/srv6-poc/manifesto.json"
NOME_INSTANCIA = "srv6_te_app"

# Constantes da Eq. 3.1 do TCC (Seção 3.5.3): peso de aresta ponderado pela
# CPU do nó mais carregado, com penalidade Λ quando o uso ultrapassa o
# limiar τ — este é o mecanismo que torna o recálculo periódico sensível a
# congestionamento real, em vez de depender só de override manual via REST.
# Os valores abaixo podem ser sobrescritos por variável de ambiente para os
# experimentos (experimentos/*.sh); sem variável, valem os da metodologia.
INTERVALO_RECALCULO_S = float(os.environ.get("TE_INTERVALO_S", 10))
LIMIAR_TAU_PCT = float(os.environ.get("TE_TAU", 80.0))
PENALIDADE_LAMBDA = 1000.0

# Modo do plano de dados (cenários da Tabela 3.9.2):
#   srv6           E4/E5  rota `encap seg6` + Dijkstra dinâmico (Eq. 3.1)
#   ipv6_dinamico  E3     rota IPv6 comum (via next-hop) + Dijkstra dinâmico
#   ipv6_estatico  E2/E2c rota IPv6 comum, pesos FIXOS do manifesto (sem TE)
TE_MODO = os.environ.get("TE_MODO", "srv6")
MODOS_VALIDOS = ("srv6", "ipv6_dinamico", "ipv6_estatico")

# Histerese (Seção 3.5.5). N = ciclos consecutivos; 0 desliga o mecanismo
# (usado só no experimento X1, para medir o que a histerese evita).
# Λ só é aplicado ao nó após N ciclos com CPU >= τ (limiar superior) e só é
# removido após N ciclos com CPU < τ_inf (limiar inferior); entre os dois há
# a banda morta.
HISTERESE_N = int(os.environ.get("HISTERESE_N", 0))
LIMIAR_TAU_INF_PCT = float(os.environ.get("TE_TAU_INF", 70.0))

# Telemetria sem amostra nova há mais de TTL segundos é tratada como nó
# SATURADO (conservador: não prefere um nó mudo). 0 desliga (comportamento
# anterior: último valor para sempre; sem amostra = 0 %). Ver R8 / C4.
TELEMETRIA_TTL_S = float(os.environ.get("TELEMETRIA_TTL_S", 0))

# CSV com um registro por recálculo (Seção 3.10: histórico de caminhos e
# tempo de convergência). Vazio = não grava.
TE_CSV = os.environ.get("TE_CSV", "")

# Métrica de saturação do nó (v2). `util`: utilização do enlace do nó, em %
# da banda (bytes/s das interfaces ÷ capacidade; ver telemetry_agent.py,
# calcula_util) — enxerga o tráfego encaminhado pelo kernel. `cpu`: CPU dos
# PROCESSOS do nó (v1; só vê carga sintética). A Eq. 3.1 é a mesma nos dois.
# TE_JANELA_S = nº de amostras (1 por segundo) da média móvel que alimenta o
# peso; suaviza a rajada de um UDP a taxa constante sem atrasar a detecção.
TE_METRICA = os.environ.get("TE_METRICA", "util")
METRICAS_VALIDAS = ("util", "cpu")
TE_JANELA = max(int(os.environ.get("TE_JANELA_S", 3)), 1)

# Porta do coletor TCP de telemetria (Seção 3.8). Confirmado livre neste
# laboratório: REST usa 8080, OpenFlow 6653, iperf3 5201.
PORTA_TELEMETRIA = 9999


class SRv6TEController(app_manager.RyuApp):
    """Aprendizado L2 por OpenFlow 1.3 + decisão de caminho SRv6 fora de banda."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(SRv6TEController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.pesos = {}          # sobrescreve o peso do manifesto, via REST
        self.ultima_decisao = {}
        self.datapaths = {}
        self.telemetria = {}     # nó -> {"cpu_percent":, "n_processos":, "ts":, "rx":}
        self.saturado = {}       # nó -> bool (estado da histerese)
        self._cont_sup = {}      # nó -> ciclos consecutivos >= τ
        self._cont_inf = {}      # nó -> ciclos consecutivos < τ_inf
        self._instalado_antes = None
        self._csv_pronto = False
        if TE_METRICA not in METRICAS_VALIDAS:
            raise ValueError("TE_METRICA invalida: %r (validas: %s)"
                             % (TE_METRICA, ", ".join(METRICAS_VALIDAS)))
        if TE_MODO not in MODOS_VALIDOS:
            raise ValueError("TE_MODO invalido: %r (validos: %s)"
                             % (TE_MODO, ", ".join(MODOS_VALIDOS)))
        kwargs["wsgi"].register(SRv6RestAPI, {NOME_INSTANCIA: self})
        self.logger.info("[SRv6-TE] app carregada; endpoints REST em /srv6/estado, "
                         "/srv6/recalcula e /srv6/peso")
        # Recálculo automático a cada INTERVALO_RECALCULO_S (TCC, Seção
        # 3.5.4): substitui o gatilho manual por POST /srv6/recalcula como
        # via PRINCIPAL de atualização; o REST continua funcionando como
        # override/observabilidade, não é removido.
        self._thread_recalculo = hub.spawn(self._loop_recalculo)

        # Coletor de telemetria (Seção 3.8): hub.StreamServer é a mesma
        # classe que o próprio Ryu usa internamente para aceitar conexões
        # OpenFlow (ryu/controller/controller.py) — reuso direto do
        # framework, não é dependência nova. Cada agente conectado ganha
        # sua própria greenthread de leitura automaticamente.
        try:
            self._servidor_telemetria = hub.StreamServer(
                ("0.0.0.0", PORTA_TELEMETRIA), self._trata_agente)
            hub.spawn(self._servidor_telemetria.serve_forever)
            self.logger.info("[SRv6-TE] coletor de telemetria escutando em :%s",
                             PORTA_TELEMETRIA)
        except OSError:
            self.logger.exception(
                "[SRv6-TE] nao foi possivel abrir o coletor de telemetria na "
                "porta %s (endereco em uso?) — telemetria real fica indisponivel, "
                "mas o resto da app continua funcionando com fallback 0%%",
                PORTA_TELEMETRIA)

    # ===================================================================== #
    # CANAL 1 — plano de controle OpenFlow 1.3 (comutação L2)
    # ===================================================================== #
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        # Regra table-miss: o que nenhuma regra casar sobe ao controlador.
        self.add_flow(dp, 0, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                              ofp.OFPCML_NO_BUFFER)])
        self.logger.info("[SRv6-TE] switch dpid=%s conectado; table-miss instalada", dp.id)

    def add_flow(self, dp, prio, match, actions, buffer_id=None):
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS, actions)]
        kw = dict(datapath=dp, priority=prio, match=match, instructions=inst)
        # `is not None` e não `if buffer_id:` — buffer_id 0 é válido, e o teste
        # por veracidade (o bug do simple_switch_13 do tutorial) o descartaria.
        if buffer_id is not None:
            kw["buffer_id"] = buffer_id
        dp.send_msg(parser.OFPFlowMod(**kw))

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        # =================================================================== #
        # NÃO troque isto por packet.Packet(msg.data) — ele QUEBRA com SRv6.
        # -------------------------------------------------------------------
        # O parser de IPv6 do RYU 4.34 só conhece os Routing Headers tipo 2
        # (RFC 6275) e tipo 3 (RFC 6554); veja ryu/lib/packet/ipv6.py, classe
        # `routing`, cuja própria tabela de docstring lista "4-252 Unassigned"
        # — ela é ANTERIOR à RFC 8754, que atribuiu o tipo 4 ao Segment Routing
        # Header. Para o tipo 4, routing.parser() devolve None, e o ipv6.parser()
        # logo adiante executa `offset += len(hdr)`, levantando
        #     TypeError: object of type 'NoneType' has no len()
        #
        # A exceção aborta o handler INTEIRO: nenhuma regra é instalada e nenhum
        # PacketOut é emitido, então o pacote some. O sintoma não parece um erro
        # de controlador — parece perda de conectividade só em um dos caminhos,
        # justamente aquele que ainda não tinha regra L2 aprendida por tráfego
        # sem SRH. Foi assim que este laboratório o descobriu.
        #
        # Um comutador de aprendizado L2 não precisa de nada além do cabeçalho
        # Ethernet. Parsear só ele é mais barato E imune ao problema: o
        # controlador nunca toca nos cabeçalhos de extensão do plano de dados.
        # =================================================================== #
        eth, _, _ = ethernet.ethernet.parser(msg.data)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        self.mac_to_port.setdefault(dp.id, {})[eth.src] = in_port
        out_port = self.mac_to_port[dp.id].get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst, eth_src=eth.src)
            # idle_timeout evita que a tabela acumule regras mortas entre
            # execuções — a F0 não tinha timeout e as regras ficavam para sempre.
            parser_mod = parser.OFPFlowMod(
                datapath=dp, priority=1, match=match, idle_timeout=60,
                instructions=[parser.OFPInstructionActions(
                    ofp.OFPIT_APPLY_ACTIONS, actions)],
                **({"buffer_id": msg.buffer_id}
                   if msg.buffer_id != ofp.OFP_NO_BUFFER else {}))
            dp.send_msg(parser_mod)
            if msg.buffer_id != ofp.OFP_NO_BUFFER:
                return

        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=in_port,
            actions=actions,
            data=msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None))

    # ===================================================================== #
    # Grafo e decisão de caminho
    # ===================================================================== #
    def _manifesto(self):
        """Lê o manifesto publicado pela topologia. Lido a cada chamada, de
        propósito: assim a ordem de partida entre RYU e Mininet não importa."""
        with open(MANIFESTO) as fp:
            return json.load(fp)

    def _amostra_expirada(self, no):
        if TELEMETRIA_TTL_S <= 0:
            return False
        am = self.telemetria.get(no)
        return am is not None and (time.time() - am.get("rx", 0)) > TELEMETRIA_TTL_S

    def _peso_no(self, no):
        """Saturação do nó em %, conforme os agentes de telemetria (Seção
        3.8), pela métrica TE_METRICA: utilização de enlace (`util`, média das
        últimas TE_JANELA amostras) ou CPU de processos (`cpu`). Sem amostra
        ainda, ou nó sem banda definida (host de borda: util_pct nulo), o
        valor é 0% — não penaliza um nó por falta de dado, só por carga
        observada. Com TELEMETRIA_TTL_S > 0, uma amostra velha demais conta
        como 100 % (nó tratado como saturado)."""
        if self._amostra_expirada(no):
            return 100.0
        am = self.telemetria.get(no, {})
        if TE_METRICA == "util":
            recentes = am.get("util_janela") or []
            return sum(recentes) / len(recentes) if recentes else 0.0
        return am.get("cpu_percent", 0.0)

    def _atualiza_histerese(self):
        """Avança os contadores da histerese UMA vez por ciclo do laço
        periódico (não nas chamadas por REST)."""
        if HISTERESE_N <= 0:
            return
        nos = set(self.telemetria) | set(self.saturado)
        for no in nos:
            cpu = self._peso_no(no)
            if cpu >= LIMIAR_TAU_PCT:
                self._cont_sup[no] = self._cont_sup.get(no, 0) + 1
                self._cont_inf[no] = 0
            elif cpu < LIMIAR_TAU_INF_PCT:
                self._cont_inf[no] = self._cont_inf.get(no, 0) + 1
                self._cont_sup[no] = 0
            else:                       # banda morta: nenhum contador avança
                self._cont_sup[no] = 0
                self._cont_inf[no] = 0
            if not self.saturado.get(no) and self._cont_sup[no] >= HISTERESE_N:
                self.saturado[no] = True
            elif self.saturado.get(no) and self._cont_inf[no] >= HISTERESE_N:
                self.saturado[no] = False

    def _peso_aresta(self, a, b):
        """Eq. 3.1 do TCC (Seção 3.5.3), função por partes:

            w(u,v) = 1 + max(CPU_u, CPU_v)/100   se max <  τ
            w(u,v) = Λ                           se max >= τ

        Acima do limiar o peso SUBSTITUI o termo de CPU (vale exatamente Λ);
        não é somado a ele. Até 25/09/2026 esta função somava Λ ao termo
        1 + CPU/100 (ex.: 1001,973 para CPU=97,3%), o que divergia da
        equação do texto. O caminho escolhido era o mesmo, mas o valor
        reportado em /srv6/estado não era o da Eq. 3.1.

        Usar o MÁXIMO (não a média) dos dois nós da aresta é deliberado: um
        enlace fica caro se QUALQUER um dos lados estiver congestionado, não
        só quando ambos estão.

        Com HISTERESE_N > 0 (Seção 3.5.5) o teste "max >= τ" é trocado pelo
        estado da histerese de cada nó (self.saturado)."""
        cpu_max = max(self._peso_no(a), self._peso_no(b))
        if HISTERESE_N > 0:
            if self.saturado.get(a) or self.saturado.get(b):
                return PENALIDADE_LAMBDA
            return 1.0 + min(cpu_max, 100.0) / 100.0
        if cpu_max >= LIMIAR_TAU_PCT:
            return PENALIDADE_LAMBDA
        return 1.0 + cpu_max / 100.0

    def _grafo(self, man):
        """Monta o grafo ponderado. O peso default vem da Eq. 3.1 (telemetria
        de CPU por nó — ver _peso_aresta); um valor sobrescrito via REST
        POST /srv6/peso tem precedência e simula congestionamento sem
        depender da telemetria real."""
        g = nx.Graph()
        for a, b, _atraso_manifesto in man["grafo"]["enlaces"]:
            if a in self.pesos:
                peso = self.pesos[a]
            elif b in self.pesos:
                peso = self.pesos[b]
            elif TE_MODO == "ipv6_estatico":
                peso = float(_atraso_manifesto)    # pesos fixos, sem telemetria
            else:
                peso = self._peso_aresta(a, b)
            g.add_edge(a, b, weight=peso)
        return g

    def decide_caminho(self, man=None):
        """Dijkstra ponderado sobre o grafo; devolve o nó de trânsito escolhido.

        Desempate determinístico (R7): avalia o menor caminho passando por
        CADA nó de trânsito e escolhe o de menor custo; em empate vale a ordem
        de man["grafo"]["transito"] (r1 antes de r2). Sem isto o networkx
        decide o empate por detalhe de implementação."""
        man = man or self._manifesto()
        g = self._grafo(man)
        transitos = man["grafo"]["transito"]
        melhor = None
        for t in transitos:
            outros = [n for n in transitos if n != t]
            gt = g.copy()
            gt.remove_nodes_from(outros)
            try:
                custo = nx.dijkstra_path_length(gt, "h1", "h2", weight="weight")
                caminho = nx.dijkstra_path(gt, "h1", "h2", weight="weight")
            except nx.NetworkXNoPath:
                continue
            carga = self._peso_no(t)
            # Empate de custo (ex.: os dois trânsitos acima de τ, peso Λ):
            # vence o de menor saturação bruta; persistindo o empate, a ordem
            # de man["grafo"]["transito"].
            if (melhor is None or custo < melhor[0] - 1e-9
                    or (abs(custo - melhor[0]) <= 1e-9 and carga < melhor[3] - 1e-9)):
                melhor = (custo, caminho, t, carga)
        if melhor is None:
            return {"escolhido": None, "caminho": [], "custo": None}
        return {"escolhido": melhor[2], "caminho": melhor[1], "custo": melhor[0]}

    # ===================================================================== #
    # CANAL 2 — instalação fora de banda (A COSTURA COM O EMULADOR)
    # ===================================================================== #
    def _instala_politica(self, man, escolhido):
        """Instala a rota `encap seg6` no kernel de cada nó de borda.

        ---------------------------------------------------------------------
        ESTE É O ÚNICO PONTO DA APP QUE SABE QUE EXISTE UM EMULADOR.
        `mnexec -a <pid>` entra no namespace de rede do host Mininet e roda o
        `ip -6 route` lá dentro. Num ambiente real não há PID a entrar: esta
        função vira um POST para um agente rodando no próprio nó, e todo o
        restante da app permanece idêntico. É por isso que a substituição da
        fase F4 é local, e não uma reescrita.
        ---------------------------------------------------------------------
        """
        sid = man["sids"][escolhido]
        aplicadas = []
        for nome, cfg in man["ingresso"].items():
            pid = man["nos"][nome]["pid"]
            if TE_MODO == "srv6":
                cmd = ("ip -6 route replace %s encap seg6 mode encap segs %s,%s dev %s"
                       % (cfg["destino"], sid, cfg["destino"], cfg["iface"]))
            else:   # IPv6 comum: mesma decisão, sem SRH (E2, E2c, E3)
                cmd = ("ip -6 route replace %s via %s dev %s"
                       % (cfg["destino"], man["nexthop"][escolhido][nome],
                          cfg["iface"]))
            proc = subprocess.run(["mnexec", "-a", str(pid)] + cmd.split(),
                                  capture_output=True, text=True)
            aplicadas.append({
                "no": nome, "pid": pid, "comando": cmd,
                "rc": proc.returncode,
                "erro": (proc.stderr or "").strip() or None,
            })
            if proc.returncode != 0:
                self.logger.error("[SRv6-TE] falha ao instalar em %s: %s",
                                  nome, proc.stderr.strip())
        return aplicadas

    def recalcula_e_instala(self, origem="rest"):
        man = self._manifesto()
        decisao = self.decide_caminho(man)
        if not decisao["escolhido"]:
            return {"erro": "nenhum nó de trânsito no caminho calculado",
                    **decisao}
        decisao["instalacao"] = self._instala_politica(man, decisao["escolhido"])
        decisao["sid_instalado"] = man["sids"][decisao["escolhido"]]
        decisao["pesos_em_vigor"] = dict(self.pesos) or "os do manifesto"
        self.ultima_decisao = decisao
        self.logger.info("[SRv6-TE] caminho %s (custo %s) -> SID %s instalado",
                         decisao["caminho"], decisao["custo"],
                         decisao["sid_instalado"])
        self._grava_csv(man, decisao, origem)
        return decisao

    def _grava_csv(self, man, decisao, origem):
        """Uma linha por recálculo, com timestamp (epoch). É a fonte do tempo
        de convergência e do número de trocas de caminho (Seção 3.10)."""
        if not TE_CSV:
            return
        nos = sorted(man["nos"])
        trocou = int(self._instalado_antes is not None
                     and decisao["escolhido"] != self._instalado_antes)
        self._instalado_antes = decisao["escolhido"]
        try:
            novo = not self._csv_pronto and not os.path.exists(TE_CSV)
            with open(TE_CSV, "a") as fp:
                if novo:
                    fp.write("t_epoch,origem,modo,escolhido,custo,trocou,"
                             + ",".join("cpu_" + n for n in nos) + ","
                             + ",".join("sat_" + n for n in nos) + ","
                             + ",".join("util_" + n for n in nos) + ","
                             + ",".join("met_" + n for n in nos) + "\n")
                self._csv_pronto = True
                fp.write("%.3f,%s,%s,%s,%s,%d,%s,%s,%s,%s\n" % (
                    time.time(), origem, TE_MODO, decisao["escolhido"],
                    decisao["custo"], trocou,
                    ",".join(str(self.telemetria.get(n, {}).get("cpu_percent", "")) for n in nos),
                    ",".join(str(int(bool(self.saturado.get(n)))) for n in nos),
                    ",".join(str(self.telemetria.get(n, {}).get("util_pct", "")) for n in nos),
                    ",".join("%.2f" % self._peso_no(n) for n in nos)))
        except OSError:
            self.logger.exception("[SRv6-TE] falha ao gravar %s", TE_CSV)

    def _loop_recalculo(self):
        """Greenthread de fundo (TCC, Seção 3.5.4): reavalia o caminho a cada
        INTERVALO_RECALCULO_S, incondicionalmente. A reinstalação via
        `ip -6 route replace` é idempotente (o kernel só substitui a mesma
        rota quando o caminho não muda), então rodar sempre em vez de só
        quando a decisão muda é o comportamento mais simples e literal do
        que a Seção 3.5.4 pede.

        O `except Exception` amplo é deliberado, não descuido: uma
        greenthread do Ryu (`hub.spawn`) que propaga uma exceção não tratada
        morre PARA SEMPRE, sem log nem retry — e isso mataria o recálculo
        automático na primeira falha transitória (ex.: manifesto ainda não
        publicado porque o Mininet subiu depois do RYU). Capturar amplo e
        logar é o que mantém o laço vivo entre ciclos.
        """
        self.logger.info("[SRv6-TE] loop de recalculo automatico iniciado (%ss)",
                         INTERVALO_RECALCULO_S)
        while True:
            hub.sleep(INTERVALO_RECALCULO_S)
            try:
                self._atualiza_histerese()
                self.recalcula_e_instala(origem="loop")
            except Exception:
                self.logger.exception("[SRv6-TE] recalculo periodico falhou; "
                                      "tentando novamente no proximo ciclo")

    # ===================================================================== #
    # Coletor de telemetria (Seção 3.8) — um agente por nó, ver
    # telemetry_agent.py. Popula self.telemetria, que _peso_no() lê.
    # ===================================================================== #
    def _trata_agente(self, sock, addr):
        """Uma greenthread por conexão de agente (hub.StreamServer já cuida
        disso). Lê linhas NDJSON até a conexão cair; não há necessidade de
        lock em self.telemetria — greenthreads do eventlet só trocam de
        contexto em pontos de bloqueio (aqui, a leitura do socket)."""
        f = sock.makefile("r")
        no_id = None
        try:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    msg = json.loads(linha)
                except ValueError:
                    continue
                if msg.get("tipo") == "hello":
                    no_id = msg.get("no")
                    self.logger.info("[SRv6-TE] agente de telemetria conectado: %s (%s)",
                                     no_id, addr)
                elif msg.get("tipo") == "amostra" and "no" in msg:
                    util = msg.get("util_pct")
                    janela = list(self.telemetria.get(msg["no"], {})
                                  .get("util_janela", []))
                    if util is not None:
                        janela = (janela + [float(util)])[-TE_JANELA:]
                    self.telemetria[msg["no"]] = {
                        "cpu_percent": msg.get("cpu_percent", 0.0),
                        "n_processos": msg.get("n_processos"),
                        "util_pct": util,
                        "util_janela": janela,
                        "taxas_bps": msg.get("taxas_bps"),
                        "ts": msg.get("ts"),
                        "rx": time.time(),
                    }
        except (IOError, OSError):
            pass
        finally:
            self.logger.info("[SRv6-TE] agente de telemetria desconectado: %s (%s)",
                             no_id, addr)
            try:
                sock.close()
            except OSError:
                pass


# ========================================================================= #
# API REST
# ========================================================================= #
class SRv6RestAPI(ControllerBase):

    def __init__(self, req, link, data, **config):
        super(SRv6RestAPI, self).__init__(req, link, data, **config)
        self.app = data[NOME_INSTANCIA]

    def _json(self, obj, status=200):
        return Response(content_type="application/json", status=status,
                        body=json.dumps(obj, ensure_ascii=False, default=str))

    @route("srv6", "/srv6/estado", methods=["GET"])
    def estado(self, req, **_):
        try:
            man = self.app._manifesto()
        except (IOError, ValueError) as e:
            return self._json({"erro": "manifesto indisponível: %s" % e,
                               "dica": "suba a topologia antes"}, 503)
        g = self.app._grafo(man)
        return self._json({
            "switches_conectados": sorted(self.app.datapaths.keys()),
            "enlaces_ponderados": [[a, b, d["weight"]] for a, b, d in g.edges(data=True)],
            "pesos_sobrescritos": self.app.pesos,
            "sids_conhecidos": man["sids"],
            "ultima_decisao": self.app.ultima_decisao or None,
            "melhor_caminho_agora": self.app.decide_caminho(man),
            "telemetria": self.app.telemetria,
            "formula": {"limiar_tau_pct": LIMIAR_TAU_PCT,
                       "penalidade_lambda": PENALIDADE_LAMBDA,
                       "intervalo_recalculo_s": INTERVALO_RECALCULO_S,
                       "modo": TE_MODO, "histerese_n": HISTERESE_N,
                       "metrica": TE_METRICA, "janela_amostras": TE_JANELA,
                       "tau_inf_pct": LIMIAR_TAU_INF_PCT,
                       "telemetria_ttl_s": TELEMETRIA_TTL_S},
            "saturado_histerese": self.app.saturado,
        })

    @route("srv6", "/srv6/recalcula", methods=["POST"])
    def recalcula(self, req, **_):
        try:
            return self._json(self.app.recalcula_e_instala())
        except (IOError, ValueError) as e:
            return self._json({"erro": str(e)}, 503)

    @route("srv6", "/srv6/peso", methods=["POST"])
    def peso(self, req, **_):
        """Simula congestionamento encarecendo um nó de trânsito.

        É o gatilho que a fase F4 vai receber da telemetria em vez de um POST
        manual: o efeito sobre o grafo e sobre a decisão é exatamente o mesmo.
        """
        try:
            corpo = json.loads(req.body.decode() or "{}")
            no, valor = corpo["no"], float(corpo["peso"])
        except (ValueError, KeyError) as e:
            return self._json({"erro": 'esperado {"no":"r1","peso":100}: %s' % e}, 400)
        self.app.pesos[no] = valor
        try:
            resultado = self.app.recalcula_e_instala()
        except (IOError, ValueError) as e:
            return self._json({"erro": str(e)}, 503)
        resultado["peso_aplicado"] = {no: valor}
        return self._json(resultado)
