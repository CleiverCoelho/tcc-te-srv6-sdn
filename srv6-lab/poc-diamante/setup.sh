#!/usr/bin/env bash
# =============================================================================
#  setup.sh — Prepara o ambiente para a PoC. Exige root.
#
#  Faz duas coisas, ambas idempotentes:
#    1. instala o tshark, necessário para o filtro ipv6.routing.type == 4 que a
#       Seção 3.10 do TCC define como validação formal do SRH (RFC 8754);
#    2. liga o seg6_enabled globalmente no host.
#
#  Sobre o item 2: a topologia já liga seg6_enabled POR INTERFACE dentro de cada
#  namespace, que é onde de fato importa. Ligar no host é conveniência para
#  experimentos manuais fora do Mininet — e é reversível com o comando impresso
#  no fim.
#
#  Uso:  sudo bash setup.sh
# =============================================================================
set -u

if [ "$(id -u)" -ne 0 ]; then
    echo "Erro: exige root. Use:  sudo bash setup.sh"
    exit 1
fi

echo "== 1/3 · Instalando tshark =="
if command -v tshark >/dev/null 2>&1; then
    echo "   já instalado: $(tshark --version 2>/dev/null | head -1)"
else
    export DEBIAN_FRONTEND=noninteractive
    # O tshark pergunta se não-root pode capturar; pré-respondemos para que o
    # apt não trave num diálogo interativo.
    echo "wireshark-common wireshark-common/install-setuid boolean true" \
        | debconf-set-selections
    apt-get install -y -q tshark >/tmp/setup-tshark.log 2>&1
    if command -v tshark >/dev/null 2>&1; then
        echo "   instalado: $(tshark --version 2>/dev/null | head -1)"
        [ -n "${SUDO_USER:-}" ] && usermod -aG wireshark "$SUDO_USER" 2>/dev/null
    else
        echo "   FALHOU — veja /tmp/setup-tshark.log (últimas linhas):"
        tail -5 /tmp/setup-tshark.log
    fi
fi

echo
echo "== 2/3 · Habilitando seg6 no host =="
sysctl -w net.ipv6.conf.all.seg6_enabled=1     >/dev/null
sysctl -w net.ipv6.conf.default.seg6_enabled=1 >/dev/null
sysctl -w net.ipv6.conf.lo.seg6_enabled=1      >/dev/null
sysctl -w net.ipv6.conf.all.forwarding=1       >/dev/null
sysctl -n net.ipv6.conf.all.seg6_enabled | sed 's/^/   all.seg6_enabled = /'

echo
echo "== 3/3 · Limpando resíduos do Mininet =="
mn -c >/dev/null 2>&1
pkill -f ryu-manager 2>/dev/null
echo "   pronto"

echo
echo "Para reverter o item 2:  sudo sysctl -w net.ipv6.conf.all.seg6_enabled=0"
echo "Próximo passo:           sudo bash valida_poc.sh"
