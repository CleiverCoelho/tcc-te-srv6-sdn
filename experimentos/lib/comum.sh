#!/usr/bin/env bash
# lib/comum.sh — funções compartilhadas pelos scripts de experimento (TCC).
# Uso: source "$(dirname "$0")/lib/comum.sh"      (sempre via sudo bash)

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXP="$RAIZ/experimentos"
RYU="$RAIZ/ryu-venv/bin/ryu-manager"
CTRL="$RAIZ/ryu_apps/srv6_te_controller.py"
SW13="$RAIZ/ryu-venv/lib/python3.10/site-packages/ryu/app/simple_switch_13.py"
TOPO="$RAIZ/srv6-lab/poc-diamante/topo_diamante.py"
APOIO="$RAIZ/srv6-lab/poc-diamante/eq31_apoio.py"
DATA="${DATA_EXP:-$(date +%F)}"
SAIDA="$RAIZ/relatorios/$DATA/experimentos"

WARMUP="${WARMUP:-30}"; CARGA="${CARGA:-40}"; RELAX="${RELAX:-50}"
CARGA_NO="${CARGA_NO:-r1}"; CARGA_PCT="${CARGA_PCT:-90}"
TAXA="${TAXA:-5M}"
# v2: scripts herdados da v1 usam CPU sintética; os da v2 (U*) exportam TE_METRICA=util
export TE_METRICA="${TE_METRICA:-cpu}"

exige_root() {
    [ "$(id -u)" -eq 0 ] || { echo "Erro: rode com sudo: sudo bash $0 ..."; exit 1; }
}

limpa() {
    pkill -f "eq31_apoio.py poll" 2>/dev/null
    pkill -f ryu-manager 2>/dev/null
    pkill -f telemetry_agent.py 2>/dev/null
    pkill -x stress-ng 2>/dev/null
    pkill -x yes 2>/dev/null
    pkill -x iperf3 2>/dev/null
    mn -c >/dev/null 2>&1
    sleep 1
}

marca() { echo "$(date +%s.%N) $1" >> "$2"; }

# sobe_ctrl DIR [APP]   — sobe o RYU com TE_CSV em DIR; APP padrão = controlador de TE
sobe_ctrl() {
    local d="$1" app="${2:-$CTRL}"
    TE_CSV="$d/ctrl.csv" "$RYU" --ofp-tcp-listen-port 6653 --wsapi-port 8080 "$app" \
        > "$d/ryu.log" 2>&1 &
    RYU_PID=$!
    sleep 4
    if ! kill -0 "$RYU_PID" 2>/dev/null; then
        echo "RYU_FALHOU" > "$d/ERRO"; tail -20 "$d/ryu.log"; return 1
    fi
}

sobe_poll() { python3 "$APOIO" poll http://127.0.0.1:8080/srv6/estado 1 > "$1/timeline.tsv" 2>"$1/poll.err" & POLL_PID=$!; }

para_tudo() {
    kill "${POLL_PID:-0}" 2>/dev/null; wait "${POLL_PID:-0}" 2>/dev/null
    kill "${RYU_PID:-0}" 2>/dev/null;  wait "${RYU_PID:-0}" 2>/dev/null
    limpa
}

# meta DIR  — metadados da execução (rastreabilidade, Seção 3.10)
meta() {
    local d="$1"
    {
        echo "data: $(date -Iseconds)"
        echo "host: $(hostname)  kernel: $(uname -r)  nproc: $(nproc)"
        [ "$(nproc)" -ge 4 ] || echo "ALERTA: nproc < 4 (TCC 3.3.1 exige 4 vCPU; ver R1)"
        echo "git: $(git -C "$RAIZ" rev-parse --short HEAD 2>/dev/null) tag: $(git -C "$RAIZ" describe --tags --exact-match 2>/dev/null || echo SEM_TAG)"
        echo "alteracoes_nao_commitadas: $(git -C "$RAIZ" status --porcelain 2>/dev/null | wc -l)"
        echo "md5_controlador: $(md5sum "$CTRL" | cut -d' ' -f1)"
        echo "md5_topologia: $(md5sum "$TOPO" | cut -d' ' -f1)"
        echo "ryu: $("$RYU" --version 2>&1 | head -1)  iperf3: $(iperf3 --version 2>&1 | head -1)"
        echo "stress-ng: $(stress-ng --version 2>&1 | head -1)"
        echo "env: $(env | grep -E '^(TE_|HISTERESE_|TELEMETRIA_|WARMUP|CARGA|RELAX|TAXA)' | tr '\n' ' ')"
    } > "$d/meta.txt"
}

dono() { [ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER":"$SUDO_USER" "$RAIZ/relatorios/$DATA" 2>/dev/null; true; }
