#!/usr/bin/env bash
# 00_preflight.sh — portões de ambiente. Aborta (rc=1) se algo obrigatório faltar.
# Uso: sudo bash experimentos/00_preflight.sh
source "$(dirname "$0")/lib/comum.sh"
exige_root
falha=0; ok(){ echo "[OK]     $*"; }; no(){ echo "[FALHA]  $*"; falha=1; }; al(){ echo "[ALERTA] $*"; }

for c in mn iperf3 jq mnexec ip tcpdump python3 git; do
    command -v $c >/dev/null && ok "$c" || no "$c ausente"
done
command -v stress-ng >/dev/null && ok "stress-ng" || no "stress-ng ausente (sudo apt install stress-ng); sem ele a carga cai para 'yes' (~99%, R3)"
[ -x "$RYU" ] && ok "ryu-manager" || no "ryu-manager ausente: $RYU"
[ -x "$RAIZ/ryu-venv/bin/python3" ] && ok "python do venv (agentes de telemetria, B3)" || no "ryu-venv/bin/python3 ausente: agentes não subiriam e a rede pareceria 'sem carga'"
[ -f "$SW13" ] && ok "simple_switch_13 (E1)" || no "simple_switch_13 não encontrado: $SW13"
[ "$(nproc)" -ge 4 ] && ok "nproc=$(nproc)" || al "nproc=$(nproc) < 4: resultados sob carga medem a contenção da VM (R1/B6); X4 quantifica"
[ -z "$(git -C "$RAIZ" status --porcelain)" ] && ok "git limpo" || al "git com alterações não commitadas: resultados não ligam a uma versão (3.10). Commit + tag antes da rodada formal"
git -C "$RAIZ" describe --tags --exact-match >/dev/null 2>&1 && ok "HEAD com tag" || al "HEAD sem tag"
for p in 6653 8080 9999 5201; do
    ss -ltn | grep -q ":$p " && no "porta $p ocupada (rode: sudo bash -c 'source experimentos/lib/comum.sh; limpa')" || ok "porta $p livre"
done
python3 -c "import mininet" 2>/dev/null && ok "mininet (python do sistema)" || no "mininet não importa no python3 do sistema"
"$RAIZ/ryu-venv/bin/python3" -c "import psutil, networkx" 2>/dev/null && ok "psutil+networkx no venv" || no "psutil/networkx ausentes no venv"
sysctl -n net.ipv6.conf.all.disable_ipv6 | grep -q 0 && ok "IPv6 habilitado" || al "IPv6 desabilitado no host"
echo; [ $falha -eq 0 ] && echo "PREFLIGHT APROVADO" || echo "PREFLIGHT REPROVADO"
exit $falha
