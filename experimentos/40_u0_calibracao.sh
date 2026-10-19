#!/usr/bin/env bash
# =============================================================================
#  U0 — calibração do sensor de utilização de enlace (v2)
#
#  Pergunta: o util_pct que o agente lê em /proc/<pid>/net/dev de r1 confere com
#  o que o iperf3 realmente injetou? E quanto o SRH acrescenta no enlace?
#
#  Para cada taxa em TAXAS e cada plano de dados (ipv6 puro, srv6 via r1):
#    iperf3 UDP h1->h2 por DUR s, tcpdump em r1-eth1 (bytes de camada 2 do enlace
#    de saída), timeline do util_r1 a 1 amostra/s. O controlador roda só como
#    coletor (TE_INTERVALO_S enorme: nunca reinstala rota), então o caminho é o
#    que a CLI fixa. Análise: python3 experimentos/92_analisa_u0.py <pasta>
#
#  Uso: sudo bash experimentos/40_u0_calibracao.sh [REPS=3]
#  Variáveis: TAXAS ("1M 3M 5M 8M 9.5M") DUR (12) DATA_EXP
# =============================================================================
source "$(dirname "$0")/lib/comum.sh"; exige_root; trap 'para_tudo; dono' EXIT
REPS="${1:-3}"; TAXAS="${TAXAS:-1M 3M 5M 8M 9.5M}"; DUR="${DUR:-12}"
export TE_METRICA=util TE_MODO=ipv6_estatico TE_INTERVALO_S=100000
BASE="$SAIDA/U0_calibracao"; mkdir -p "$BASE"
for REP in $(seq 1 "$REPS"); do
  R="$BASE/rep$REP"; rm -rf "$R"; mkdir -p "$R"; MARC="$R/marcadores.txt"; : > "$MARC"
  echo "--- U0 rep $REP/$REPS  $(date +%T)"
  limpa; meta "$R"
  sobe_ctrl "$R" || continue; sobe_poll "$R"
  {
    echo "sh sleep 8"
    for PLANO in ipv6 srv6; do
      if [ "$PLANO" = srv6 ]; then echo "via r1"; else echo "srv6off"; fi
      for T in $TAXAS; do
        TAG_RUN="${PLANO}_${T}"
        echo "r1 tcpdump -i r1-eth1 -w $R/${TAG_RUN}.pcap -c 100000 > /dev/null 2>&1 &"
        echo "sh sleep 1"
        echo "sh echo \"\$(date +%s.%N) INI_${TAG_RUN}\" >> $MARC"
        echo "fluxo $DUR $R/iperf_cli_${TAG_RUN}.json $T $R/iperf_srv_${TAG_RUN}.json"
        echo "sh sleep $((DUR + 3))"
        echo "sh echo \"\$(date +%s.%N) FIM_${TAG_RUN}\" >> $MARC"
        echo "r1 pkill tcpdump"
        echo "sh sleep 2"
      done
    done
    echo "fim"; echo "exit"
  } | python3 "$TOPO" --porta-controlador 6653 --modo ipv6 > "$R/sessao.log" 2>&1
  para_tudo
done
python3 "$EXP/92_analisa_u0.py" "$BASE" | tee "$BASE/resumo.txt"
echo "U0 concluído: $BASE"
