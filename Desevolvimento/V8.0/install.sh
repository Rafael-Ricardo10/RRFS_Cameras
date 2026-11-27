#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  RRFS Cameras - Instalador Automatizado v2.2
#  Sistema de Monitoramento de Câmeras IP
# ═══════════════════════════════════════════════════════════════════════════

set -e

# ── Configurações ──────────────────────────────────────────────────────────
INSTALL_DIR="/DATA/AppData/RRFS_Cameras"
SERVICE_NAME="rrfs-cameras"
env_DIR="$INSTALL_DIR/venv"
PYTHON_BIN="python3"
PORT="2901"

# ── Cores ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Funções de output ──────────────────────────────────────────────────────
print_header() {
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
}

print_info()    { echo -e "${CYAN}[INFO]${NC}  $1"; }
print_success() { echo -e "${GREEN}[OK]${NC}    $1"; }
print_warning() { echo -e "${YELLOW}[AVISO]${NC} $1"; }
print_error()   { echo -e "${RED}[ERRO]${NC}  $1"; }
print_step()    { echo -e "${BLUE}  →${NC} $1"; }

# ── Verificações iniciais ──────────────────────────────────────────────────
check_root() {
    if [ "$EUID" -ne 0 ]; then
        print_error "Execute como root: sudo bash instalar.sh"
        exit 1
    fi
}

check_os() {
    print_info "Verificando sistema operacional..."
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        print_success "Detectado: $PRETTY_NAME"
    else
        print_error "Não foi possível detectar o sistema operacional."
        exit 1
    fi
}

check_internet() {
    print_info "Verificando conexão com a internet..."
    if ping -c 1 -W 3 8.8.8.8 > /dev/null 2>&1; then
        print_success "Conexão com internet OK"
        return 0
    else
        print_warning "Sem conexão com a internet!"
        print_warning "A instalação de pacotes Python pode falhar."
        echo ""
        read -p "  Continuar mesmo assim? (s/N): " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Ss]$ ]]; then
            print_info "Instalação cancelada. Verifique sua conexão."
            exit 1
        fi
        return 1
    fi
}

# ── Instalar dependências do sistema ──────────────────────────────────────
install_system_deps() {
    print_header "Instalando Dependências do Sistema"

    print_step "Atualizando lista de pacotes..."
    apt-get update -qq 2>&1 | grep -v "^Aviso\|^Warning\|^Hit\|^Get\|^Ign" || true

    print_step "Instalando Python3, pip e ferramentas de build..."
    apt-get install -y \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        build-essential \
        ffmpeg \
        libgl1 \
        libglib2.0-0t64 \
        2>&1 | tail -5

    # Tenta instalar python3-opencv do sistema (mais rápido que pip)
    print_step "Tentando instalar OpenCV do sistema..."
    apt-get install -y python3-opencv 2>/dev/null && \
        print_success "OpenCV instalado via apt" || \
        print_warning "OpenCV será instalado via pip (pode demorar)"

    print_success "Dependências do sistema instaladas"
}

# ── Copiar arquivos ────────────────────────────────────────────────────────
copy_files() {
    print_header "Copiando Arquivos do Sistema"

    SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
    print_info "Origem: $SCRIPT_DIR"
    print_info "Destino: $INSTALL_DIR"

    # Cria estrutura completa de diretórios
    print_step "Criando estrutura de diretórios..."
    mkdir -p "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR/dados/config"
    mkdir -p "$INSTALL_DIR/dados/logs"
    mkdir -p "$INSTALL_DIR/dados/gravacoes"
    mkdir -p "$INSTALL_DIR/dados/thumbs"
    mkdir -p "$INSTALL_DIR/modulos"
    mkdir -p "$INSTALL_DIR/templates"
    mkdir -p "$INSTALL_DIR/static"
    print_success "Estrutura de diretórios criada"

    # Módulos
    if [ -d "$SCRIPT_DIR/modulos" ]; then
        print_step "Copiando módulos Python..."
        cp -r "$SCRIPT_DIR/modulos/." "$INSTALL_DIR/modulos/"
        print_success "Módulos copiados"
    else
        print_error "Pasta 'modulos' não encontrada em $SCRIPT_DIR"
        exit 1
    fi

    # Templates
    if [ -d "$SCRIPT_DIR/templates" ]; then
        print_step "Copiando templates HTML..."
        cp -r "$SCRIPT_DIR/templates/." "$INSTALL_DIR/templates/"
        print_success "Templates copiados"
    else
        print_error "Pasta 'templates' não encontrada em $SCRIPT_DIR"
        exit 1
    fi

    # Static (opcional)
    if [ -d "$SCRIPT_DIR/static" ]; then
        print_step "Copiando arquivos estáticos..."
        cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
        print_success "Arquivos estáticos copiados"
    fi

    # servidor.py
    print_step "Copiando servidor principal..."
    if [ -f "$SCRIPT_DIR/servidor.py" ]; then
        cp "$SCRIPT_DIR/servidor.py" "$INSTALL_DIR/servidor.py"
        print_success "servidor.py copiado"
    else
        print_error "servidor.py não encontrado!"
        exit 1
    fi

    # requirements.txt
    print_step "Verificando requirements.txt..."
    if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
        cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
        print_success "requirements.txt copiado"
    else
        print_warning "requirements.txt não encontrado. Criando padrão..."
        cat > "$INSTALL_DIR/requirements.txt" << 'REQEOF'
Flask==3.0.3
Flask-Login==0.6.3
opencv-python-headless==4.10.0.84
numpy==1.26.4
Werkzeug==3.0.3
REQEOF
        print_success "requirements.txt criado"
    fi

    print_success "Todos os arquivos copiados para $INSTALL_DIR"
}

# ── Garantir opencv no requirements ───────────────────────────────────────
ensure_requirements() {
    REQ="$INSTALL_DIR/requirements.txt"
    if ! grep -q "opencv" "$REQ" 2>/dev/null; then
        print_warning "Adicionando opencv ao requirements.txt..."
        echo "opencv-python-headless==4.10.0.84" >> "$REQ"
    fi
}

# ── Ambiente virtual Python ────────────────────────────────────────────────
setup_venv() {
    print_header "Configurando Ambiente Virtual Python"

    # Cria venv se não existe
    if [ -d "$env_DIR" ]; then
        print_info "Ambiente virtual já existe em $env_DIR"
    else
        print_step "Criando ambiente virtual Python..."
        $PYTHON_BIN -m venv "$env_DIR" || {
            print_error "Falha ao criar ambiente virtual!"
            print_info "Tentando com --without-pip..."
            $PYTHON_BIN -m venv --without-pip "$env_DIR"
            curl -s https://bootstrap.pypa.io/get-pip.py | "$env_DIR/bin/python"
        }
        print_success "Ambiente virtual criado"
    fi

    # Atualiza pip
    print_step "Atualizando pip..."
    "$env_DIR/bin/pip" install --upgrade pip setuptools wheel || {
        print_warning "Falha ao atualizar pip. Continuando com versão atual..."
    }

    # Instala dependências
    print_step "Instalando dependências Python (pode demorar alguns minutos)..."
    echo ""

    ensure_requirements

    "$env_DIR/bin/pip" install \
        --no-cache-dir \
        -r "$INSTALL_DIR/requirements.txt" 2>&1 | \
        while IFS= read -r line; do
            echo "    $line"
        done

    # Verifica módulos instalados
    print_step "Verificando instalação das dependências..."

    ERRORS=0

    check_module() {
        local mod="$1"
        local name="$2"
        if "$env_DIR/bin/python" -c "import $mod" 2>/dev/null; then
            print_success "$name instalado"
        else
            print_error "$name NÃO foi instalado!"
            ERRORS=$((ERRORS + 1))
        fi
    }

    check_module "flask"       "Flask"
    check_module "flask_login" "Flask-Login"
    check_module "cv2"         "OpenCV"
    check_module "numpy"       "NumPy"

    if [ $ERRORS -gt 0 ]; then
        echo ""
        print_error "$ERRORS dependência(s) não instalada(s)!"
        print_info "Tente instalar manualmente:"
        print_info "  $env_DIR/bin/pip install Flask Flask-Login opencv-python-headless numpy"
        echo ""
        read -p "Continuar mesmo assim? (s/N): " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Ss]$ ]]; then
            exit 1
        fi
    else
        print_success "Todas as dependências verificadas!"
    fi
}

# ── Permissões ─────────────────────────────────────────────────────────────
apply_permissions() {
    print_header "Aplicando Permissões"

    print_step "Definindo proprietário como root..."
    chown -R root:root "$INSTALL_DIR"

    print_step "Permissões de diretórios (755)..."
    find "$INSTALL_DIR" -type d -exec chmod 755 {} \;

    print_step "Permissões de arquivos (644)..."
    find "$INSTALL_DIR" -type f -exec chmod 644 {} \;

    print_step "Permissão de execução para servidor.py..."
    chmod 755 "$INSTALL_DIR/servidor.py"

    print_step "Permissão de execução para binários do venv..."
    find "$env_DIR/bin" -type f -exec chmod 755 {} \;

    print_step "Permissões de escrita para dados e logs..."
    chmod -R 777 "$INSTALL_DIR/dados"

    print_success "Permissões aplicadas"
}

# ── Criar serviço systemd ──────────────────────────────────────────────────
create_service() {
    print_header "Criando Serviço Systemd"

    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

    print_step "Escrevendo arquivo de serviço..."
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=RRFS Cameras - Sistema de Monitoramento de Câmeras IP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$INSTALL_DIR

Environment="PATH=$env_DIR/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="OPENCV_LOG_LEVEL=ERROR"
Environment="OPENCV_FFMPEG_LOGLEVEL=0"

ExecStart=$env_DIR/bin/python $INSTALL_DIR/servidor.py

Restart=always
RestartSec=10
StartLimitInterval=200
StartLimitBurst=5

StandardOutput=append:$INSTALL_DIR/dados/logs/service.log
StandardError=append:$INSTALL_DIR/dados/logs/service_error.log

LimitNOFILE=65536
LimitNPROC=4096

[Install]
WantedBy=multi-user.target
EOF

    print_success "Arquivo de serviço criado: $SERVICE_FILE"

    print_step "Recarregando systemd..."
    systemctl daemon-reload

    print_step "Habilitando serviço na inicialização..."
    systemctl enable "$SERVICE_NAME"

    print_success "Serviço configurado e habilitado"
}

# ── Iniciar serviço ────────────────────────────────────────────────────────
start_service() {
    print_step "Iniciando serviço RRFS Cameras..."
    systemctl start "$SERVICE_NAME"

    for i in $(seq 1 15); do
        sleep 1
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            print_success "Serviço iniciado com sucesso! (${i}s)"
            return 0
        fi
        printf "."
    done
    echo ""

    print_error "Serviço não iniciou no tempo esperado!"
    echo ""
    print_info "Últimas linhas do log:"
    echo "─────────────────────────────────"
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null || true
    echo "─────────────────────────────────"
    echo ""
    print_info "Log de erros da aplicação:"
    if [ -f "$INSTALL_DIR/dados/logs/service_error.log" ]; then
        tail -20 "$INSTALL_DIR/dados/logs/service_error.log"
    fi
    echo ""
    read -p "Continuar mesmo assim? (s/N): " -n 1 -r
    echo ""
    [[ $REPLY =~ ^[Ss]$ ]] || exit 1
}

# ── Parar serviço ──────────────────────────────────────────────────────────
stop_service() {
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        print_step "Parando serviço..."
        systemctl stop "$SERVICE_NAME"
        sleep 2
        print_success "Serviço parado"
    else
        print_info "Serviço não está rodando"
    fi
}

# ── Instalação completa ────────────────────────────────────────────────────
install() {
    print_header "INSTALAÇÃO RRFS CAMERAS"

    check_root
    check_os
    check_internet
    install_system_deps
    copy_files
    setup_venv
    apply_permissions
    create_service
    start_service

    LOCAL_IP=$(hostname -I | awk '{print $1}' 2>/dev/null || echo "SEU_IP")

    print_header "INSTALAÇÃO CONCLUÍDA COM SUCESSO!"
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║${NC}                                                       ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   ✓  RRFS Cameras instalado e rodando!               ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}                                                       ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   Acesse pelo navegador:                              ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   ${CYAN}http://${LOCAL_IP}:${PORT}${NC}                       ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}                                                       ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   Comandos úteis:                                     ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   • Status:  systemctl status ${SERVICE_NAME}         ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   • Parar:   systemctl stop ${SERVICE_NAME}           ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   • Iniciar: systemctl start ${SERVICE_NAME}          ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   • Logs:    journalctl -u ${SERVICE_NAME} -f         ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}                                                       ${GREEN}║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════╝${NC}"
    echo ""
}

# ── Atualização ────────────────────────────────────────────────────────────
update() {
    print_header "ATUALIZAÇÃO RRFS CAMERAS"

    check_root

    if [ ! -d "$INSTALL_DIR" ]; then
        print_error "RRFS Cameras não está instalado em $INSTALL_DIR"
        print_info "Execute a instalação primeiro."
        exit 1
    fi

    stop_service

    BACKUP_DIR="${INSTALL_DIR}_backup_$(date +%Y%m%d_%H%M%S)"
    print_step "Fazendo backup em $BACKUP_DIR..."
    mkdir -p "$BACKUP_DIR"
    [ -d "$INSTALL_DIR/dados" ] && cp -r "$INSTALL_DIR/dados" "$BACKUP_DIR/"
    print_success "Backup criado"

    print_step "Removendo arquivos antigos (dados e venv preservados)..."
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
        ! -name 'dados' \
        ! -name 'venv' \
        -exec rm -rf {} + 2>/dev/null || true

    copy_files

    print_step "Atualizando dependências Python..."
    ensure_requirements
    "$env_DIR/bin/pip" install --upgrade --no-cache-dir \
        -r "$INSTALL_DIR/requirements.txt" 2>&1 | tail -5
    print_success "Dependências atualizadas"

    apply_permissions

    systemctl daemon-reload
    start_service

    print_header "ATUALIZAÇÃO CONCLUÍDA!"
    print_success "RRFS Cameras atualizado com sucesso!"
    print_info "Backup dos dados em: $BACKUP_DIR"
}

# ── Desinstalação ──────────────────────────────────────────────────────────
uninstall() {
    print_header "DESINSTALAÇÃO RRFS CAMERAS"

    check_root

    echo ""
    print_warning "Esta ação irá remover o RRFS Cameras do sistema."
    read -p "  Tem certeza? (s/N): " -n 1 -r
    echo ""
    [[ $REPLY =~ ^[Ss]$ ]] || { print_info "Cancelado."; exit 0; }

    stop_service

    print_step "Desabilitando serviço..."
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true

    print_step "Removendo arquivo de serviço..."
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload

    echo ""
    print_warning "Deseja remover também as gravações e logs?"
    read -p "  Remover dados? (s/N): " -n 1 -r
    echo ""

    if [[ $REPLY =~ ^[Ss]$ ]]; then
        print_step "Removendo instalação completa..."
        rm -rf "$INSTALL_DIR"
        print_success "Instalação e dados removidos"
    else
        print_step "Removendo sistema (mantendo dados)..."
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
            ! -name 'dados' \
            -exec rm -rf {} + 2>/dev/null || true
        print_success "Sistema removido"
        print_info "Dados mantidos em: $INSTALL_DIR/dados"
    fi

    print_header "DESINSTALAÇÃO CONCLUÍDA"
}

# ── Verificar status ───────────────────────────────────────────────────────
check_status() {
    print_header "STATUS DO SISTEMA"

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        print_success "Serviço: ATIVO"
    else
        print_error "Serviço: INATIVO"
    fi

    echo ""
    systemctl status "$SERVICE_NAME" --no-pager -l 2>/dev/null || true

    echo ""
    print_info "Espaço em disco:"
    if [ -d "$INSTALL_DIR/dados/gravacoes" ]; then
        du -sh "$INSTALL_DIR/dados/gravacoes" 2>/dev/null | \
            awk '{print "    Gravações: " $1}'
    fi
    df -h "$INSTALL_DIR" 2>/dev/null | tail -1 | \
        awk '{print "    Disco total: " $2 " | Usado: " $3 " | Livre: " $4}'

    echo ""
    echo "Pressione ENTER para voltar ao menu..."
    read
    show_menu
}

# ── Ver logs ───────────────────────────────────────────────────────────────
view_logs() {
    print_header "LOGS DO SISTEMA"

    echo "  1) Logs do serviço (systemd - tempo real)"
    echo "  2) Logs da aplicação (tempo real)"
    echo "  3) Últimas 50 linhas - log do serviço"
    echo "  4) Últimas 50 linhas - log de erros"
    echo "  5) Voltar"
    echo ""
    read -p "Opção: " log_choice

    case $log_choice in
        1)
            print_info "Pressione Ctrl+C para sair"
            sleep 1
            journalctl -u "$SERVICE_NAME" -f
            ;;
        2)
            LOG="$INSTALL_DIR/dados/logs/sistema.log"
            if [ -f "$LOG" ]; then
                print_info "Pressione Ctrl+C para sair"
                sleep 1
                tail -f "$LOG"
            else
                print_error "Log não encontrado: $LOG"
                sleep 2
            fi
            ;;
        3)
            journalctl -u "$SERVICE_NAME" -n 50 --no-pager
            echo ""
            echo "Pressione ENTER para continuar..."
            read
            ;;
        4)
            ERR="$INSTALL_DIR/dados/logs/service_error.log"
            if [ -f "$ERR" ]; then
                tail -50 "$ERR"
            else
                print_info "Nenhum erro registrado"
            fi
            echo ""
            echo "Pressione ENTER para continuar..."
            read
            ;;
        5) show_menu ;;
        *)
            print_error "Opção inválida"
            sleep 1
            view_logs
            ;;
    esac

    show_menu
}

# ── Menu principal ─────────────────────────────────────────────────────────
show_menu() {
    clear
    echo -e "${BLUE}"
    cat << 'BANNER'
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║    ██████╗ ██████╗ ███████╗███████╗                          ║
║    ██╔══██╗██╔══██╗██╔════╝██╔════╝                          ║
║    ██████╔╝██████╔╝█████╗  ███████╗                          ║
║    ██╔══██╗██╔══██╗██╔══╝  ╚════██║                          ║
║    ██║  ██║██║  ██║██║     ███████║                          ║
║    ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚══════╝                          ║
║                                                               ║
║         Sistema de Monitoramento de Câmeras IP                ║
║                  Instalador v2.2                              ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
BANNER
    echo -e "${NC}"

    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        echo -e "  Status: ${GREEN}● RODANDO${NC} — http://$(hostname -I | awk '{print $1}'):${PORT}"
    elif [ -d "$INSTALL_DIR" ]; then
        echo -e "  Status: ${YELLOW}● INSTALADO / PARADO${NC}"
    else
        echo -e "  Status: ${RED}● NÃO INSTALADO${NC}"
    fi
    echo ""

    echo -e "${GREEN}  Escolha uma opção:${NC}"
    echo ""
    echo "  1) Instalar RRFS Cameras"
    echo "  2) Atualizar RRFS Cameras"
    echo "  3) Desinstalar RRFS Cameras"
    echo "  4) Verificar Status"
    echo "  5) Ver Logs"
    echo "  6) Sair"
    echo ""
    read -p "  Opção: " choice

    case $choice in
        1) install ;;
        2) update ;;
        3) uninstall ;;
        4) check_status ;;
        5) view_logs ;;
        6) exit 0 ;;
        *)
            print_error "Opção inválida"
            sleep 1
            show_menu
            ;;
    esac
}

# ── Entry point ────────────────────────────────────────────────────────────
main() {
    case "${1:-}" in
        install)   check_root; install ;;
        update)    check_root; update ;;
        uninstall) check_root; uninstall ;;
        *)         show_menu ;;
    esac
}

main "$@"
