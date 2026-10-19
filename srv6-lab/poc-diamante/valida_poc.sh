#!/usr/bin/env bash
# =============================================================================
#  valida_poc.sh — Executa a PoC de ponta a ponta, sem interação.
#
#  Sequência: limpa resíduos -> sobe o RYU com a app de engenharia de tráfego ->
#  sobe a topologia em diamante e roda os blocos A-F -> encerra -> disseca o SRH
#  com tshark -> devolve os artefatos ao usuário comum.
#
#  Uso:  sudo bash valida_poc.sh
#  Saída: capturas/resultados.json, capturas/*.pcap, capturas/ryu.log
# =============================================================================
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(cd "$DIR/../.." && pwd)"
VENV="$RAIZ/ryu-venv"
RYU="$VENV/bin/ryu-manager"
APP="$RAIZ/ryu_apps/srv6_te_controller.py"
CAP="$DIR/capturas"
LOG_RYU="$CAP/ryu.log"

if [ "$(id -u)" -ne 0 ]; then
    echo "Erro: o Mininet exige root. Use:  sudo bash valida_poc.sh"
    exit 1
fi
for f in "$RYU" "$APP"; do
    [ -e "$f" ] || { echo "Erro: não encontrado: $f"; exit 1; }
done

mkdir -p "$CAP"

limpa() {
    pkill -f ryu-manager 2>/dev/null
    mn -c >/dev/null 2>&1
}
trap limpa EXIT

echo "== 1/5 · Limpando resíduos =="
limpa
rm -f /tmp/srv6-poc/manifesto.json
sleep 1

echo "== 2/5 · Subindo o RYU com a app de engenharia de tráfego =="
# O RYU roda como ROOT de propósito: o canal 2 (fora de banda) usa `mnexec -a`
# para entrar no namespace do nó de ingresso, e isso exige privilégio. Num
# ambiente real o controlador não precisaria de root — ele falaria com um agente.
"$RYU" --ofp-tcp-listen-port 6653 --wsapi-port 8080 "$APP" > "$LOG_RYU" 2>&1 &
RYU_PID=$!
sleep 5
if ! kill -0 "$RYU_PID" 2>/dev/null; then
    echo "Erro: o RYU não subiu. Últimas linhas do log:"
    tail -25 "$LOG_RYU"
    exit 1
fi
echo "   RYU no PID $RYU_PID — OpenFlow em 6653, REST em 8080"
grep -m1 "SRv6-TE" "$LOG_RYU" | sed 's/^/   /'

echo
echo "== 3/5 · Subindo a topologia em diamante e rodando os blocos A-F =="
# /usr/bin/python3 explícito: o Mininet só existe no Python do sistema. Se o
# venv do RYU estiver ativo no shell, um `python3` simples pegaria o do venv,
# que tem include-system-site-packages=false e não enxerga o Mininet.
/usr/bin/python3 "$DIR/topo_diamante.py" --auto-teste --porta-controlador 6653
RC=$?

echo
echo "== 4/5 · Encerrando o controlador =="
kill "$RYU_PID" 2>/dev/null
wait "$RYU_PID" 2>/dev/null
mn -c >/dev/null 2>&1
echo "   switches que completaram o handshake OpenFlow:"
grep -c "conectado; table-miss instalada" "$LOG_RYU" 2>/dev/null | sed 's/^/      /'
grep -E "SRv6-TE\] caminho" "$LOG_RYU" 2>/dev/null | sed 's/^/      /'

echo
echo "== 5/5 · Dissecação do SRH (RFC 8754) =="
PCAP="$CAP/c_srv6_r1.pcap"
if command -v tshark >/dev/null 2>&1 && [ -f "$PCAP" ]; then
    # Exatamente o filtro que a Seção 3.10 do TCC define para validar o SRH.
    N=$(tshark -r "$PCAP" -Y 'ipv6.routing.type == 4' 2>/dev/null | wc -l)
    echo "   pacotes com SRH (ipv6.routing.type == 4): $N"
    echo "   campos do SRH (type / segments-left / last-entry / lista):"
    # Sem "-c 1": o -c do tshark limita os pacotes LIDOS, não os exibidos, e
    # junto com -Y faria ele ler só o quadro 1 e não imprimir nada.
    tshark -r "$PCAP" -Y 'ipv6.routing.type == 4' -T fields \
        -e ipv6.routing.type -e ipv6.routing.segleft \
        -e ipv6.routing.srh.last_entry -e ipv6.routing.srh.addr 2>/dev/null \
        | head -6 | sed 's/^/      /'
    tshark -r "$PCAP" -Y 'ipv6.routing.type == 4' -V 2>/dev/null \
        > "$CAP/srh_dissecado.txt"
    echo "   dissecação completa em capturas/srh_dissecado.txt"
elif [ ! -f "$PCAP" ]; then
    echo "   captura $PCAP não encontrada — o bloco C não chegou a rodar"
else
    echo "   tshark ausente — rode antes:  sudo bash setup.sh"
fi

# Deixa tudo legível para o usuário comum abrir no Wireshark.
if [ -n "${SUDO_USER:-}" ]; then
    chown -R "$SUDO_USER":"$SUDO_USER" "$CAP" 2>/dev/null
fi

echo
echo "==============================================================="
if [ "$RC" -eq 0 ]; then
    echo " PoC APROVADA — todos os critérios críticos passaram"
elif [ "$RC" -eq 2 ]; then
    echo " PoC REPROVADA em ao menos um critério crítico."
    echo " Veja o campo 'vereditos' em capturas/resultados.json"
else
    echo " Execução interrompida por erro (código $RC)"
fi
echo "==============================================================="
echo "Artefatos:  $CAP"
echo "Resultados: $CAP/resultados.json"
echo "Wireshark:  wireshark $PCAP   (filtro: ipv6.routing.type == 4)"
exit $RC
