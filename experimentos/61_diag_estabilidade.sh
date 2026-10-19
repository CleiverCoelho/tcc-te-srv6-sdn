#!/usr/bin/env bash
# =============================================================================
#  61_diag_estabilidade.sh — o que faz o plano de dados "travar em rajadas" a 5 Mbit/s?
#
#  Mesmo fluxo UDP de 5 Mbit/s h1->h2 (IPv6 puro, 30 s) sob cargas de apoio
#  decrescentes, na mesma sessão do Mininet:
#     C3 = tcpdump em r1 + agentes de telemetria + poll REST 1 Hz
#     C2 = agentes + poll          C1 = só agentes          C0 = nada além do iperf3
#  A sessão roda 2 vezes (S1 e S2) para separar efeito da carga de efeito do momento.
#  Métrica: Mbit/s por segundo no SERVIDOR (média, desvio, mínimo e nº de segundos
#  abaixo de 70 % do alvo = "travadas"), mais RTT ocioso antes de cada fluxo.
#
#  Uso: sudo -E bash experimentos/61_diag_estabilidade.sh        (cerca de 8 min)
# =============================================================================
source "$(dirname "$0")/lib/comum.sh"; exige_root; trap 'para_tudo; dono' EXIT
export TE_METRICA=util TE_MODO=ipv6_estatico TE_INTERVALO_S=100000
BASE="$SAIDA/DIAG_ESTABILIDADE"; rm -rf "$BASE"; mkdir -p "$BASE"
DUR=30
for S in S1 S2; do
  D="$BASE/$S"; mkdir -p "$D"; limpa; meta "$D"
  sobe_ctrl "$D" || exit 1; sobe_poll "$D"
  python3 "$TOPO" --porta-controlador 6653 --modo ipv6 > "$D/sessao.log" 2>&1 <<CLI
sh sleep 6
r1 tcpdump -i r1-eth1 -s 128 -w $D/c3.pcap -c 1000000 > /dev/null 2>&1 &
sh sleep 1
h1 ping -6 -c 5 -i 0.2 2001:db8:b::1 > $D/rtt_c3.txt 2>&1
fluxo $DUR $D/c3_cli.json 5M $D/c3_srv.json
sh sleep $((DUR + 8))
r1 pkill tcpdump
sh sleep 2
h1 ping -6 -c 5 -i 0.2 2001:db8:b::1 > $D/rtt_c2.txt 2>&1
fluxo $DUR $D/c2_cli.json 5M $D/c2_srv.json
sh sleep $((DUR + 8))
sh pkill -f "eq31_apoio.py poll"
sh sleep 2
h1 ping -6 -c 5 -i 0.2 2001:db8:b::1 > $D/rtt_c1.txt 2>&1
fluxo $DUR $D/c1_cli.json 5M $D/c1_srv.json
sh sleep $((DUR + 8))
sh pkill -f telemetry_agent.py
sh sleep 2
h1 ping -6 -c 5 -i 0.2 2001:db8:b::1 > $D/rtt_c0.txt 2>&1
fluxo $DUR $D/c0_cli.json 5M $D/c0_srv.json
sh sleep $((DUR + 8))
fim
exit
CLI
  para_tudo
done
python3 - "$BASE" <<'PY'
import json, os, statistics as st, sys, re
base = sys.argv[1]
print("%-4s %-4s %7s %6s %6s %5s %9s %s" % ("sess", "cond", "media", "dp", "min", "trav", "rtt_ms", "descrição"))
desc = {"c3": "tcpdump+agentes+poll", "c2": "agentes+poll", "c1": "só agentes", "c0": "nada além do iperf3"}
for s in ("S1", "S2"):
    for c in ("c3", "c2", "c1", "c0"):
        try:
            j = json.load(open(os.path.join(base, s, c + "_srv.json")))
            v = [i["sum"]["bits_per_second"] / 1e6 for i in j["intervals"]][3:-1]
            rtt = re.findall(r"= [\d.]+/([\d.]+)/", open(os.path.join(base, s, "rtt_%s.txt" % c)).read())
            trav = sum(1 for x in v if x < 0.7 * 5.0)
            print("%-4s %-4s %7.2f %6.2f %6.2f %5d %9s %s" % (s, c, st.mean(v), st.pstdev(v), min(v), trav, rtt[0] if rtt else "NA", desc[c]))
        except Exception as e:
            print("%-4s %-4s sem dados (%s)" % (s, c, e))
print("\nLeitura: 'trav' = segundos com menos de 3,5 Mbit/s entregues (alvo 5). Se c0 também trava, a causa é a VM/host,")
print("não o nosso código; se só c3/c2 travam, a carga de apoio (tcpdump/poll) é a causa.")
PY
