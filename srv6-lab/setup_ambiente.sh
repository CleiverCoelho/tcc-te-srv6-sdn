#!/usr/bin/env bash
# =============================================================================
#  setup_ambiente.sh — Preparação do ambiente para o laboratório SRv6
#
#  Este script NÃO cria um ambiente novo. Ele faz duas coisas sobre o ambiente
#  que o Tutorial-SDN-Mininet_RYU.pdf (LabCOM/UFMG) já mandou instalar:
#
#    (a) REPARA o RYU, que está quebrado nesta VM (eventlet 0.30.2 + Python 3.10).
#        Sem isso, as Práticas 11 a 19 do tutorial não executam.
#    (b) COMPLETA o que o tutorial pediu e não foi instalado (wireshark/tshark)
#        e as bibliotecas das fases futuras do TCC (networkx/pyroute2).
#
#  É idempotente: pode ser executado várias vezes sem efeito colateral.
#
#  Uso:   bash setup_ambiente.sh
# =============================================================================
set -u

VENV="${VENV:-$HOME/sdn-lab/ryu-venv}"
SITE="$VENV/lib/python3.10/site-packages"
PY="$VENV/bin/python3"

ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
skip() { printf '  \033[34m•\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
err()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
titulo() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# -----------------------------------------------------------------------------
titulo "1/4 · Reparo do RYU (4 correções de compatibilidade com Python 3.10)"
# -----------------------------------------------------------------------------
# Causa raiz: o `pip install ryu` traz eventlet 0.30.2, anterior ao suporte a
# Python 3.10. Não existe versão de eventlet que resolva sozinha, porque:
#   • eventlet <= 0.30.2 : tem ALREADY_HANDLED (que o Ryu importa) mas quebra no 3.10
#   • eventlet >= 0.31.0 : removeu ALREADY_HANDLED -> ImportError no Ryu
# As 4 correções abaixo são exatamente as que Debian/Ubuntu aplicam no pacote
# python3-eventlet ("Add upstream Python 3.10 patches").

if [ ! -d "$SITE" ]; then
    err "venv não encontrada em $VENV"
    exit 1
fi

"$PY" - "$SITE" <<'PYEOF'
import sys, pathlib, shutil, re

site = pathlib.Path(sys.argv[1])
OK, SKIP, FAIL = "  \033[32m✔\033[0m", "  \033[34m•\033[0m", "  \033[31m✗\033[0m"

def patch(rel, antes, depois, descricao):
    f = site / rel
    if not f.exists():
        print(f"{FAIL} {rel}: arquivo não encontrado")
        return False
    s = f.read_text()
    if depois in s:
        print(f"{SKIP} {descricao} (já aplicado)")
        return True
    if antes not in s:
        print(f"{FAIL} {descricao}: padrão esperado não encontrado")
        return False
    if not (f.with_suffix(f.suffix + ".orig")).exists():
        shutil.copy2(f, str(f) + ".orig")          # backup antes da 1ª alteração
    f.write_text(s.replace(antes, depois, 1))
    print(f"{OK} {descricao}")
    return True

todos = []

# (1) eventlet/greenio/base.py — no Python 3.10 socket.timeout virou um alias de
#     TimeoutError, que é imutável; wrap_is_timeout() tenta escrever um atributo
#     nele e explode. Guarda idêntica à do upstream eventlet 0.33.3.
todos.append(patch(
    "eventlet/greenio/base.py",
    "socket_timeout = eventlet.timeout.wrap_is_timeout(socket.timeout)",
    "if sys.version_info >= (3, 10):\n"
    "    socket_timeout = socket.timeout  # Really, TimeoutError\n"
    "else:\n"
    "    socket_timeout = eventlet.timeout.wrap_is_timeout(socket.timeout)",
    "eventlet/greenio/base.py  · socket.timeout imutável no 3.10",
))

# (2) eventlet/greenio/py3.py — no Python 3.10 _pyio.open passou a ser um
#     staticmethod, que não expõe __code__ diretamente.
todos.append(patch(
    "eventlet/greenio/py3.py",
    "_open = FunctionType(\n"
    "    six.get_function_code(_original_pyio.open),\n"
    "    _open_environment,\n"
    ")",
    "if hasattr(_original_pyio, 'text_encoding'):\n"
    "    _open_environment['text_encoding'] = _original_pyio.text_encoding\n\n"
    "_pyio_open = getattr(_original_pyio.open, '__wrapped__', _original_pyio.open)\n"
    "_open = FunctionType(\n"
    "    six.get_function_code(_pyio_open),\n"
    "    _open_environment,\n"
    ")",
    "eventlet/greenio/py3.py   · _pyio.open virou staticmethod",
))

# (3) e (4) — as ABCs saíram de `collections` para `collections.abc` no 3.10.
todos.append(patch(
    "eventlet/green/http/client.py",
    "collections.Iterable",
    "collections.abc.Iterable",
    "eventlet/green/http/client.py · collections.abc",
))
todos.append(patch(
    "dns/namedict.py",
    "collections.MutableMapping",
    "collections.abc.MutableMapping",
    "dns/namedict.py (dnspython) · collections.abc",
))

sys.exit(0 if all(todos) else 1)
PYEOF
PATCH_RC=$?

titulo "Validação: o RYU inicia?"
if RYUV=$("$VENV/bin/ryu-manager" --version 2>&1); then
    ok "ryu-manager operante → $RYUV"
else
    err "ryu-manager ainda falha:"
    echo "$RYUV" | tail -3
fi

# -----------------------------------------------------------------------------
titulo "2/4 · Bibliotecas Python das fases seguintes do TCC"
# -----------------------------------------------------------------------------
# networkx  -> gerenciador de topologia do controlador (TCC, Seção 3.5.1)
# pyroute2  -> injeção programática da política SRv6  (TCC, Listagem 3.3)
# psutil    -> telemetria de CPU/memória por nó       (TCC, Seção 3.8) [já presente]
for pkg in networkx pyroute2 psutil; do
    if "$PY" -c "import $pkg" 2>/dev/null; then
        skip "$pkg já instalado"
    else
        if "$VENV/bin/pip" install -q "$pkg" 2>/dev/null; then
            ok "$pkg instalado"
        else
            err "falha ao instalar $pkg"
        fi
    fi
done

# -----------------------------------------------------------------------------
titulo "3/4 · Wireshark e tshark (Tutorial LabCOM, Seções 8 e 9)"
# -----------------------------------------------------------------------------
if command -v tshark >/dev/null 2>&1; then
    skip "wireshark/tshark já instalados ($(tshark --version 2>/dev/null | head -1))"
else
    warn "wireshark/tshark ausentes — instalando (requer sudo)"
    # Responde previamente à pergunta "não-administradores podem capturar?" = Sim
    echo "wireshark-common wireshark-common/install-setuid boolean true" \
        | sudo debconf-set-selections
    if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y wireshark tshark; then
        ok "wireshark e tshark instalados"
    else
        err "falha no apt-get — verifique a conexão e tente novamente"
    fi
fi

# Grupo wireshark: permite capturar sem rodar a GUI como root — importante
# nesta VM, que usa sessão Wayland (onde `sudo wireshark` costuma não abrir).
if getent group wireshark >/dev/null 2>&1; then
    if id -nG "$USER" | tr ' ' '\n' | grep -qx wireshark; then
        skip "usuário $USER já pertence ao grupo wireshark"
    else
        sudo usermod -aG wireshark "$USER" \
            && warn "usuário adicionado ao grupo wireshark — FAÇA LOGOUT/LOGIN para valer"
    fi
else
    skip "grupo wireshark inexistente (só é criado com o wireshark instalado)"
fi

# -----------------------------------------------------------------------------
titulo "4/4 · Inventário final do ambiente"
# -----------------------------------------------------------------------------
printf '  %-34s %s\n' "Sistema:"      "$(lsb_release -ds 2>/dev/null)"
printf '  %-34s %s\n' "Kernel:"       "$(uname -r)  (SRv6 exige >= 5.0)"
printf '  %-34s %s\n' "Mininet:"      "$(mn --version 2>&1 | head -1)"
printf '  %-34s %s\n' "Open vSwitch:" "$(ovs-vsctl --version 2>/dev/null | head -1)"
printf '  %-34s %s\n' "iproute2:"     "$(ip -V 2>&1)"
printf '  %-34s %s\n' "RYU:"          "$("$VENV/bin/ryu-manager" --version 2>&1 | head -1)"
printf '  %-34s %s\n' "tshark:"       "$(tshark --version 2>/dev/null | head -1 || echo 'AUSENTE')"

echo
echo "  Suporte a SRv6 no kernel:"
grep -q "CONFIG_IPV6_SEG6_LWTUNNEL=y" "/boot/config-$(uname -r)" 2>/dev/null \
    && ok "CONFIG_IPV6_SEG6_LWTUNNEL=y (subsistema seg6 embutido)" \
    || err "subsistema seg6 indisponível neste kernel"
ip route add help 2>&1 | grep -q "seg6local" \
    && ok "iproute2 suporta 'encap seg6' e 'seg6local action End'" \
    || err "iproute2 sem suporte a seg6"

echo
if [ "$PATCH_RC" -eq 0 ] && "$VENV/bin/ryu-manager" --version >/dev/null 2>&1; then
    printf '\033[1;32m  Ambiente pronto.\033[0m Próximo passo:\n'
    echo   "    Terminal 1:  source ~/sdn-lab/ryu-venv/bin/activate && ryu-manager ryu.app.simple_switch_13"
    echo   "    Terminal 2:  sudo python3 ~/sdn-lab/srv6-lab/topo_srv6.py"
else
    printf '\033[1;31m  Ambiente incompleto\033[0m — reveja as mensagens acima.\n'
fi
echo
