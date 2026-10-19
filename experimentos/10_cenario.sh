#!/usr/bin/env bash
# =============================================================================
#  10_cenario.sh — executor formal dos cenários da Tabela 3.9.2 (+ E2c)
#
#  Uso: sudo bash experimentos/10_cenario.sh <E1|E2|E2c|E3|E4|E5> [REPS=5]
#
#  Cada repetição sobe tudo do zero e segue a Seção 3.9.3:
#      WARMUP 30 s  ->  CARGA 40 s (cenários com carga)  ->  RELAX 50 s
#  com iperf3 UDP 5 Mbit/s (-i 1 -J, cliente em h1 e servidor em h2, este
#  último grava perda/jitter por intervalo) e ping6 paralelo (RTT).
#
#  Cenário      controlador            TE_MODO         topologia  carga
#  E1           simple_switch_13       —               ipv6       não
#  E2           srv6_te_controller     ipv6_estatico   ipv6       não
#  E2c (novo)   srv6_te_controller     ipv6_estatico   ipv6       sim
#  E3           srv6_te_controller     ipv6_dinamico   ipv6       sim
#  E4           srv6_te_controller     srv6            srv6       sim
#  E2u/E3u/E4u  (v2) = E2c/E3/E4 com a saturação de r1 por TRÁFEGO REAL (iperf3 de
#               fundo, FUNDO_TAXA, preso em r1) medida por utilização de enlace
#               (TE_METRICA=util), sem stress-ng. CARGA_TIPO=trafego. O instante
#               de CARGA_ON recebe um offset aleatório de 0–10 s (SEMENTE+rep),
#               para que a convergência amostre a fase do laço de 10 s.
#  E5           (exige topo_triplo.py — pendência D9; não implementado)
#
#  O fundo (iperf3 -t CARGA) começa ~1 s após CARGA_ON e o servidor ainda drena a fila de r1 ao fim:
#  por isso 'fimfundo' só roda 6 s após CARGA_OFF (senão a perda do fundo sai inflada).
#  Cada rep grava drops.tsv (descartes de fila tc em r1/r2 nos marcadores INI/CARGA_ON/CARGA_OFF/FIM).
#  Variáveis: FUNDO_TAXA(8.5M) SEMENTE(20261018) TE_METRICA TE_JANELA_S WARMUP CARGA RELAX CARGA_NO CARGA_PCT CARGA_PIN TAXA HISTERESE_N TE_* DATA_EXP
#             TAG (nome da pasta de saída; padrão = ID do cenário)
#  Saída: relatorios/<data>/experimentos/<ID>/rep<N>/  (+ INVALIDA se reprovar)
# =============================================================================
source "$(dirname "$0")/lib/comum.sh"
exige_root
ID="${1:-}"; REPS="${2:-5}"
# cenários herdados da v1 (E1–E4, E2c) continuam por CPU sintética; os *u forçam util
export TE_METRICA=cpu; unset CARGA_TIPO
case "$ID" in
  E1)  APP="$SW13"; MODO_TOPO=ipv6; COM_CARGA=0; export TE_MODO=ipv6_estatico ;;
  E2)  APP="$CTRL"; MODO_TOPO=ipv6; COM_CARGA=0; export TE_MODO=ipv6_estatico ;;
  E2c) APP="$CTRL"; MODO_TOPO=ipv6; COM_CARGA=1; export TE_MODO=ipv6_estatico ;;
  E3)  APP="$CTRL"; MODO_TOPO=ipv6; COM_CARGA=1; export TE_MODO=ipv6_dinamico ;;
  E4)  APP="$CTRL"; MODO_TOPO=srv6; COM_CARGA=1; export TE_MODO=srv6 ;;
  E2u) APP="$CTRL"; MODO_TOPO=ipv6; COM_CARGA=1; export TE_MODO=ipv6_estatico TE_METRICA=util CARGA_TIPO=trafego ;;
  E3u) APP="$CTRL"; MODO_TOPO=ipv6; COM_CARGA=1; export TE_MODO=ipv6_dinamico TE_METRICA=util CARGA_TIPO=trafego ;;
  E4u) APP="$CTRL"; MODO_TOPO=srv6; COM_CARGA=1; export TE_MODO=srv6 TE_METRICA=util CARGA_TIPO=trafego ;;
  E5)  echo "E5 exige a topologia de 3 nós de trânsito (topo_triplo.py, D9) — ainda não implementada."; exit 3 ;;
  *)   echo "Uso: sudo bash $0 <E1|E2|E2c|E3|E4|E5|E2u|E3u|E4u> [REPS]"; exit 1 ;;
esac
case "$REPS" in ''|*[!0-9]*) echo "REPS inválido"; exit 1;; esac
[ -f "$TOPO" ] && [ -f "$APP" ] || { echo "arquivo não encontrado"; exit 1; }
trap 'para_tudo; dono' EXIT

BASE="$SAIDA/${TAG:-$ID}"; mkdir -p "$BASE"
echo "=== $ID  reps=$REPS  modo_topo=$MODO_TOPO  TE_MODO=$TE_MODO  carga=$COM_CARGA($CARGA_NO ${CARGA_PCT}%) ==="

for REP in $(seq 1 "$REPS"); do
    R="$BASE/rep$REP"; rm -rf "$R"; mkdir -p "$R"; MARC="$R/marcadores.txt"; : > "$MARC"
    echo "--- $ID rep $REP/$REPS  $(date +%T)"
    limpa; meta "$R"
    sobe_ctrl "$R" "$APP" || { echo "RYU falhou"; continue; }
    [ "$ID" = "E1" ] || sobe_poll "$R"

    OFF=0
    if [ "${CARGA_TIPO:-cpu}" = "trafego" ]; then
        OFF=$(python3 -c "import random; random.seed(${SEMENTE:-20261018}+$REP); print(round(random.uniform(0,10),1))")
        echo "$OFF" > "$R/offset_carga_s.txt"
        BLOCO_CARGA="sh echo \"\$(date +%s.%N) CARGA_ON\" >> $MARC
drops CARGA_ON $R/drops.tsv
fundo $CARGA ${FUNDO_TAXA:-8.5M} $R/iperf_fundo_cliente.json $R/iperf_fundo_servidor.json
sh sleep $CARGA
sh echo \"\$(date +%s.%N) CARGA_OFF\" >> $MARC
drops CARGA_OFF $R/drops.tsv
sh sleep 6
fimfundo"
    elif [ "$COM_CARGA" -eq 1 ]; then
        BLOCO_CARGA="sh echo \"\$(date +%s.%N) CARGA_ON\" >> $MARC
drops CARGA_ON $R/drops.tsv
carga $CARGA_NO $CARGA_PCT ${CARGA_PIN:+pin}
sh sleep $CARGA
sh echo \"\$(date +%s.%N) CARGA_OFF\" >> $MARC
drops CARGA_OFF $R/drops.tsv
descarga $CARGA_NO"
    else
        BLOCO_CARGA="sh sleep $CARGA"
    fi
    TOTAL=$(python3 -c "print(int(round($WARMUP + $OFF + $CARGA + $RELAX)))")

    marca REP_INICIO "$MARC"
    python3 "$TOPO" --porta-controlador 6653 --modo "$MODO_TOPO" <<CLI > "$R/sessao.log" 2>&1
sh echo "\$(date +%s.%N) WARMUP_INICIO" >> $MARC
contadores
drops INI $R/drops.tsv
fluxo $TOTAL $R/iperf_cliente.json $TAXA $R/iperf_servidor.json
rtt $TOTAL $R/ping.txt
sh sleep $WARMUP
sh sleep $OFF
$BLOCO_CARGA
sh sleep $RELAX
sh echo "\$(date +%s.%N) FIM_TRAFEGO" >> $MARC
sh sleep 4
drops FIM $R/drops.tsv
contadores
politica
fim
exit
CLI
    marca REP_FIM "$MARC"
    para_tudo
    python3 "$EXP/exp_apoio.py" valida "$R" --carga "$COM_CARGA" --cenario "$ID" | tee "$R/validacao.txt"
done
python3 "$EXP/exp_apoio.py" resumo-cenario "$BASE" | tee "$BASE/resumo.txt"
echo "$ID concluído: $BASE"
