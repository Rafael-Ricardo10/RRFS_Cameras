#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  RRFS Cameras - Instalador Automatizado v2.7
#  Sistema de Monitoramento de Câmeras IP
#
#  v2.7: suporte a câmeras iCSee/XMeye sem RTSP (solar a bateria) via
#        go2rtc. O binário fica em dados/bin/go2rtc (pasta persistente,
#        preservada nas atualizações).
# ═══════════════════════════════════════════════════════════════════════════

set -e

# ── Configurações ──────────────────────────────────────────────────────────
INSTALL_DIR="/DATA/AppData/RRFS_Cameras"
SERVICE_NAME="rrfs-cameras"
env_DIR="$INSTALL_DIR/venv"
PYTHON_BIN="python3"
PORT="2901"

# ── go2rtc (ponte para câmeras iCSee/XMeye) ────────────────────────────────
GO2RTC_BIN_DIR="$INSTALL_DIR/dados/bin"
GO2RTC_BIN="$GO2RTC_BIN_DIR/go2rtc"

# ── Configurações de resiliência de rede para pip ──────────────────────────
PIP_CACHE_DIR="$INSTALL_DIR/.pip_cache"
PIP_TIMEOUT=120
PIP_RESUME_RETRIES=20
PIP_BASH_RETRY_ATTEMPTS=8
PIP_SUPPORTS_RESUME=0
TORCH_CPU_INDEX="https://download.pytorch.org/whl/cpu"
MIN_FREE_SPACE_MB=4096   # exige pelo menos 4GB livres antes de instalar pacotes grandes

export PIP_CACHE_DIR

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

# ── Verifica espaço em disco disponível ────────────────────────────────────
check_disk_space() {
    local required_mb="${1:-$MIN_FREE_SPACE_MB}"
    local avail_mb
    avail_mb=$(df --output=avail -m "$INSTALL_DIR" 2>/dev/null | tail -1 | tr -d ' ')

    if [ -z "$avail_mb" ]; then
        print_warning "Não foi possível verificar espaço em disco."
        return 0
    fi

    if [ "$avail_mb" -lt "$required_mb" ]; then
        print_error "Espaço em disco insuficiente!"
        print_error "Disponível: ${avail_mb}MB | Necessário: ${required_mb}MB"
        echo ""
        print_info "Sugestões para liberar espaço:"
        echo "    • Limpar cache do pip:  rm -rf $PIP_CACHE_DIR/*"
        echo "    • Verificar gravações antigas em: $INSTALL_DIR/dados/gravacoes"
        echo "    • Rodar: df -h $INSTALL_DIR"
        echo ""
        read -p "Continuar mesmo assim (pode falhar)? (s/N): " -n 1 -r
        echo ""
        [[ $REPLY =~ ^[Ss]$ ]] || return 1
    else
        print_success "Espaço em disco OK (${avail_mb}MB disponíveis)"
    fi
    return 0
}

# ── Verificação individual de pacotes do sistema ───────────────────────────
check_and_install_packages() {
    local packages=("$@")
    local missing=()

    for pkg in "${packages[@]}"; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            print_success "$pkg já instalado"
        else
            missing+=("$pkg")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        print_step "Instalando pacote(s) faltante(s): ${missing[*]}"
        apt-get install -y "${missing[@]}" 2>&1 | tail -10
        print_success "Pacote(s) instalado(s): ${missing[*]}"
    fi
}

# ── Detecta nome correto do pacote glib (varia entre versões do Debian) ────
install_glib() {
    if apt-cache show libglib2.0-0t64 >/dev/null 2>&1; then
        check_and_install_packages "libglib2.0-0t64"
    elif apt-cache show libglib2.0-0 >/dev/null 2>&1; then
        check_and_install_packages "libglib2.0-0"
    else
        print_warning "Pacote libglib2.0 não encontrado nos repositórios (pode já estar incluso no sistema)"
    fi
}

# ── Instalar dependências do sistema ──────────────────────────────────────
install_system_deps() {
    print_header "Instalando Dependências do Sistema"

    print_step "Atualizando lista de pacotes..."
    apt-get update -qq 2>&1 | grep -v "^Aviso\|^Warning\|^Hit\|^Get\|^Ign" || true

    print_step "Verificando pacotes base Python..."
    check_and_install_packages python3 python3-pip python3-venv python3-dev

    print_step "Verificando curl (download do go2rtc)..."
    check_and_install_packages curl ca-certificates

    print_step "Verificando ferramentas de build (necessárias p/ dlib/face_recognition)..."
    check_and_install_packages build-essential cmake libopenblas-dev liblapack-dev libx11-dev

    print_step "Verificando bibliotecas de vídeo/imagem (ffmpeg, OpenCV, GUI libs)..."
    check_and_install_packages ffmpeg libgl1 libsm6 libxext6 libxrender1

    print_step "Verificando libglib2.0..."
    install_glib

    print_step "Tentando instalar OpenCV do sistema (mais rápido que via pip)..."
    if dpkg -s python3-opencv >/dev/null 2>&1; then
        print_success "OpenCV já instalado via apt"
    else
        apt-get install -y python3-opencv 2>/dev/null && \
            print_success "OpenCV instalado via apt" || \
            print_warning "OpenCV será instalado via pip (pode demorar)"
    fi

    print_success "Dependências do sistema verificadas/instaladas"
}

# ── Copiar arquivos ────────────────────────────────────────────────────────
copy_files() {
    print_header "Copiando Arquivos do Sistema"

    SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
    print_info "Origem: $SCRIPT_DIR"
    print_info "Destino: $INSTALL_DIR"

    print_step "Criando estrutura de diretórios..."
    mkdir -p "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR/dados/config"
    mkdir -p "$INSTALL_DIR/dados/logs"
    mkdir -p "$INSTALL_DIR/dados/gravacoes"
    mkdir -p "$INSTALL_DIR/dados/thumbs"
    mkdir -p "$INSTALL_DIR/dados/bin"
    mkdir -p "$INSTALL_DIR/modulos"
    mkdir -p "$INSTALL_DIR/templates"
    mkdir -p "$INSTALL_DIR/static"
    mkdir -p "$PIP_CACHE_DIR"
    print_success "Estrutura de diretórios criada"

    if [ -d "$SCRIPT_DIR/modulos" ]; then
        print_step "Copiando módulos Python..."
        cp -r "$SCRIPT_DIR/modulos/." "$INSTALL_DIR/modulos/"
        print_success "Módulos copiados"
    else
        print_error "Pasta 'modulos' não encontrada em $SCRIPT_DIR"
        exit 1
    fi

    if [ -d "$SCRIPT_DIR/templates" ]; then
        print_step "Copiando templates HTML..."
        cp -r "$SCRIPT_DIR/templates/." "$INSTALL_DIR/templates/"
        print_success "Templates copiados"
    else
        print_error "Pasta 'templates' não encontrada em $SCRIPT_DIR"
        exit 1
    fi

    if [ -d "$SCRIPT_DIR/static" ]; then
        print_step "Copiando arquivos estáticos..."
        cp -r "$SCRIPT_DIR/static/." "$INSTALL_DIR/static/"
        print_success "Arquivos estáticos copiados"
    fi

    print_step "Copiando servidor principal..."
    if [ -f "$SCRIPT_DIR/servidor.py" ]; then
        cp "$SCRIPT_DIR/servidor.py" "$INSTALL_DIR/servidor.py"
        print_success "servidor.py copiado"
    else
        print_error "servidor.py não encontrado!"
        exit 1
    fi

    print_step "Verificando requirements.txt..."
    if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
        cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
        print_success "requirements.txt copiado"
    else
        print_warning "requirements.txt não encontrado. Criando padrão..."
        cat > "$INSTALL_DIR/requirements.txt" << 'REQEOF'
Flask==3.0.3
Flask-Login==0.6.3
Werkzeug==3.0.3
opencv-python-headless==4.10.0.84
numpy==1.26.4
Pillow
dlib==19.24.6
face_recognition==1.3.0
torch
torchvision
ultralytics
REQEOF
        print_success "requirements.txt criado"
    fi

    print_success "Todos os arquivos copiados para $INSTALL_DIR"
}

# ── go2rtc: ponte para câmeras iCSee/XMeye (protocolo DVRIP, sem RTSP) ────
# O binário fica em dados/bin/ (pasta persistente): a atualização do
# sistema nunca apaga nem precisa baixar de novo.
# Chamada tanto na INSTALAÇÃO quanto na ATUALIZAÇÃO.
install_go2rtc() {
    print_header "Configurando go2rtc (câmeras iCSee/XMeye)"

    mkdir -p "$GO2RTC_BIN_DIR"

    # Já instalado e íntegro? Mantém (use GO2RTC_FORCAR=1 para reinstalar)
    if [ -x "$GO2RTC_BIN" ] && [ "${GO2RTC_FORCAR:-0}" != "1" ]; then
        local sz
        sz=$(stat -c%s "$GO2RTC_BIN" 2>/dev/null || echo 0)
        if [ "$sz" -gt 1048576 ]; then
            print_success "go2rtc já instalado em $GO2RTC_BIN (mantido)"
            chmod 755 "$GO2RTC_BIN"
            return 0
        fi
        print_warning "go2rtc existente parece corrompido. Reinstalando..."
        rm -f "$GO2RTC_BIN"
    fi

    local arch
    case "$(uname -m)" in
        x86_64|amd64)  arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        armv7*|armv6*|arm*) arch="arm" ;;
        *) arch="amd64" ;;
    esac
    local url="https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_${arch}"
    local tmp="$GO2RTC_BIN.tmp"

    print_step "Baixando go2rtc (${arch})..."
    local ok=0 i
    for i in 1 2 3 4 5; do
        if curl -fL --retry 3 --connect-timeout 20 --max-time 300 \
                -o "$tmp" "$url" 2>/dev/null; then
            local sz
            sz=$(stat -c%s "$tmp" 2>/dev/null || echo 0)
            if [ "$sz" -gt 1048576 ]; then ok=1; break; fi
        fi
        print_warning "Tentativa $i/5 falhou. Aguardando 5s..."
        rm -f "$tmp"
        sleep 5
    done

    if [ "$ok" -eq 1 ]; then
        mv -f "$tmp" "$GO2RTC_BIN"
        chmod 755 "$GO2RTC_BIN"
        print_success "go2rtc instalado em $GO2RTC_BIN"
        return 0
    fi
    rm -f "$tmp"

    # Sem internet: procura um binário já baixado ao lado do instalador
    print_warning "Download falhou. Procurando binário local..."
    local cand
    for cand in \
        "$SCRIPT_DIR/go2rtc" \
        "$SCRIPT_DIR/go2rtc_linux_${arch}" \
        "$SCRIPT_DIR/bin/go2rtc" \
        /home/*/go2rtc_linux_${arch} \
        /root/go2rtc_linux_${arch}; do
        if [ -f "$cand" ] && [ "$(stat -c%s "$cand" 2>/dev/null || echo 0)" -gt 1048576 ]; then
            cp -f "$cand" "$GO2RTC_BIN"
            chmod 755 "$GO2RTC_BIN"
            print_success "go2rtc copiado de $cand"
            return 0
        fi
    done

    print_warning "go2rtc NÃO foi instalado (só afeta câmeras iCSee/XMeye)."
    print_info "Para instalar manualmente, copie o binário para:"
    echo "      $GO2RTC_BIN   (nome exato: go2rtc, com chmod 755)"
    print_info "O sistema também tenta baixá-lo sozinho ao subir, se houver internet."
    return 0
}

# ── Garantir opencv no requirements ───────────────────────────────────────
ensure_requirements() {
    REQ="$INSTALL_DIR/requirements.txt"
    if ! grep -qi "opencv" "$REQ" 2>/dev/null; then
        print_warning "Adicionando opencv ao requirements.txt..."
        echo "opencv-python-headless==4.10.0.84" >> "$REQ"
    fi
}

# ── Verifica se o venv está saudável, recria se estiver quebrado ──────────
ensure_venv_ready() {
    local needs_create=0

    if [ ! -d "$env_DIR" ]; then
        print_info "Ambiente virtual não existe. Será criado."
        needs_create=1
    else
        print_step "Verificando integridade do ambiente virtual..."
        if [ ! -x "$env_DIR/bin/python3" ]; then
            print_warning "venv/bin/python3 não existe ou não é executável!"
            needs_create=1
        elif ! "$env_DIR/bin/python3" --version >/dev/null 2>&1; then
            print_warning "venv/bin/python3 está QUEBRADO!"
            needs_create=1
        elif ! "$env_DIR/bin/pip" --version >/dev/null 2>&1; then
            print_warning "venv/bin/pip está quebrado!"
            needs_create=1
        else
            print_success "Ambiente virtual íntegro"
        fi
    fi

    if [ "$needs_create" -eq 1 ]; then
        if [ -d "$env_DIR" ]; then
            print_step "Removendo venv corrompido/antigo..."
            rm -rf "$env_DIR"
        fi
        print_step "Criando novo ambiente virtual Python..."
        $PYTHON_BIN -m venv "$env_DIR" || {
            print_error "Falha ao criar ambiente virtual!"
            print_info "Tentando com --without-pip..."
            $PYTHON_BIN -m venv --without-pip "$env_DIR"
            curl -s https://bootstrap.pypa.io/get-pip.py | "$env_DIR/bin/python"
        }
        print_success "Ambiente virtual criado com sucesso"

        print_step "Atualizando pip..."
        "$env_DIR/bin/pip" install --upgrade pip setuptools wheel || {
            print_warning "Falha ao atualizar pip. Continuando com versão atual..."
        }
    fi

    mkdir -p "$PIP_CACHE_DIR"
    check_pip_resume_support
}

check_pip_resume_support() {
    if "$env_DIR/bin/pip" install --help 2>/dev/null | grep -q -- "--resume-retries"; then
        PIP_SUPPORTS_RESUME=1
        print_success "pip suporta retomar downloads (--resume-retries) ✓"
    else
        PIP_SUPPORTS_RESUME=0
        print_warning "pip NÃO suporta --resume-retries (versão antiga)."
    fi
}

# ── Detecta se é um pacote da família torch (torch/torchvision/torchaudio) ─
is_torch_package() {
    local pkg="$1"
    local base
    base=$(echo "$pkg" | sed -E 's/[<>=!~ ].*//' | tr '[:upper:]' '[:lower:]')
    case "$base" in
        torch|torchvision|torchaudio) return 0 ;;
        *) return 1 ;;
    esac
}

# ── Instala um pacote com resume automático em caso de queda de conexão ───
pip_install_resilient() {
    local pkg_spec="$1"
    local attempt=1

    local pip_args=(install --progress-bar on --timeout "$PIP_TIMEOUT" --retries 10 --no-input)

    if [ "$PIP_SUPPORTS_RESUME" -eq 1 ]; then
        pip_args+=(--resume-retries "$PIP_RESUME_RETRIES")
    fi

    if is_torch_package "$pkg_spec"; then
        print_info "Pacote '$pkg_spec' é da família PyTorch."
        print_info "Usando build CPU-only (muito menor, sem CUDA/NVIDIA) de: $TORCH_CPU_INDEX"
        pip_args+=(--index-url "$TORCH_CPU_INDEX")
    fi

    while [ $attempt -le $PIP_BASH_RETRY_ATTEMPTS ]; do
        if [ $attempt -gt 1 ]; then
            print_warning "Tentativa $attempt/$PIP_BASH_RETRY_ATTEMPTS para: $pkg_spec"
        fi

        # Verifica espaço antes de cada tentativa (pode ter mudado)
        local avail_mb
        avail_mb=$(df --output=avail -m "$INSTALL_DIR" 2>/dev/null | tail -1 | tr -d ' ')
        if [ -n "$avail_mb" ] && [ "$avail_mb" -lt 500 ]; then
            print_error "Espaço em disco crítico (${avail_mb}MB)! Abortando instalação deste pacote."
            print_error "Libere espaço antes de tentar novamente (ex: rm -rf $PIP_CACHE_DIR/*)"
            return 1
        fi

        if "$env_DIR/bin/pip" "${pip_args[@]}" "$pkg_spec"; then
            return 0
        fi

        print_warning "Falhou. Aguardando 8s antes de tentar novamente..."
        sleep 8
        attempt=$((attempt + 1))
    done

    return 1
}

# ── Normaliza nome de pacote (minúsculo, underscore -> hífen) ─────────────
normalize_name() {
    echo "$1" | tr '[:upper:]' '[:lower:]' | tr '_' '-'
}

# ── Compara requirements.txt com pacotes já instalados no venv ────────────
sync_requirements() {
    local REQ="$INSTALL_DIR/requirements.txt"
    print_step "Comparando requirements.txt com bibliotecas instaladas no venv..."
    echo ""

    declare -A installed_versions
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local name="${line%%==*}"
        local version="${line##*==}"
        name=$(normalize_name "$name")
        installed_versions["$name"]="$version"
    done < <("$env_DIR/bin/pip" freeze 2>/dev/null)

    local TO_INSTALL=()
    local UP_TO_DATE=()

    while IFS= read -r raw_line; do
        local line
        line=$(echo "$raw_line" | sed 's/#.*//' | xargs 2>/dev/null)
        [[ -z "$line" ]] && continue

        local req_name req_version
        if [[ "$line" == *"=="* ]]; then
            req_name="${line%%==*}"
            req_version="${line##*==}"
        else
            req_name="$line"
            req_version=""
        fi

        local norm_name
        norm_name=$(normalize_name "$req_name")
        local inst_version="${installed_versions[$norm_name]:-}"

        if [ -z "$inst_version" ]; then
            TO_INSTALL+=("$line")
        elif [ -n "$req_version" ] && [ "$inst_version" != "$req_version" ]; then
            TO_INSTALL+=("$line (instalado: $inst_version → requerido: $req_version)")
        else
            UP_TO_DATE+=("$req_name ($inst_version)")
        fi
    done < "$REQ"

    if [ ${#UP_TO_DATE[@]} -gt 0 ]; then
        print_success "${#UP_TO_DATE[@]} biblioteca(s) já atualizada(s):"
        for p in "${UP_TO_DATE[@]}"; do
            echo -e "      ${GREEN}✓${NC} $p"
        done
        echo ""
    fi

    if [ ${#TO_INSTALL[@]} -eq 0 ]; then
        print_success "Todas as bibliotecas já estão na versão correta! Nada a fazer."
        return 0
    fi

    print_warning "${#TO_INSTALL[@]} biblioteca(s) serão instalada(s)/atualizada(s):"
    for p in "${TO_INSTALL[@]}"; do
        echo -e "      ${YELLOW}→${NC} $p"
    done

    # Verifica espaço necessário (torch CPU-only precisa bem menos, mas ainda assim)
    check_disk_space "$MIN_FREE_SPACE_MB" || {
        print_error "Instalação abortada por falta de espaço em disco."
        return 1
    }

    print_info "Cache de downloads em: $PIP_CACHE_DIR"
    echo ""

    local FAILED=()

    for p in "${TO_INSTALL[@]}"; do
        local pkg_line="${p%% (*}"
        print_header "Instalando: $pkg_line"

        if pip_install_resilient "$pkg_line"; then
            print_success "$pkg_line instalado com sucesso"
        else
            print_error "Falha definitiva ao instalar '$pkg_line'"
            FAILED+=("$pkg_line")
        fi
        echo ""
    done

    if [ ${#FAILED[@]} -gt 0 ]; then
        echo ""
        print_error "═══════════════════════════════════════════════════"
        print_error "As seguintes bibliotecas NÃO foram instaladas:"
        for f in "${FAILED[@]}"; do
            echo -e "      ${RED}✗${NC} $f"
        done
        print_error "═══════════════════════════════════════════════════"
        print_info "Verifique espaço em disco (df -h) e rode 'Atualizar' novamente."
        return 1
    fi

    return 0
}

# ── Ambiente virtual Python ────────────────────────────────────────────────
setup_venv() {
    print_header "Configurando Ambiente Virtual Python"

    ensure_venv_ready
    ensure_requirements

    print_warning "dlib/torch/ultralytics podem demorar. Torch usará build CPU-only"
    print_warning "(sem CUDA/NVIDIA) para economizar espaço e tempo de download."
    echo ""

    sync_requirements
    local SYNC_RESULT=$?

    print_step "Verificando instalação final das dependências..."

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

    check_module "flask"            "Flask"
    check_module "flask_login"      "Flask-Login"
    check_module "cv2"              "OpenCV"
    check_module "numpy"            "NumPy"
    check_module "dlib"             "dlib"
    check_module "face_recognition" "face_recognition"
    check_module "torch"            "PyTorch (CPU)"
    check_module "torchvision"      "TorchVision (CPU)"
    check_module "ultralytics"      "Ultralytics (YOLO)"

    if [ $ERRORS -gt 0 ] || [ $SYNC_RESULT -ne 0 ]; then
        echo ""
        print_error "$ERRORS dependência(s) não instalada(s)!"
        print_info "Verifique espaço em disco e rode a instalação/atualização novamente."
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

    print_step "Permissão de execução para o go2rtc..."
    if [ -f "$GO2RTC_BIN" ]; then
        chmod 755 "$GO2RTC_BIN"
    fi

    print_step "Permissões do cache do pip..."
    chmod -R 755 "$PIP_CACHE_DIR" 2>/dev/null || true

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
Environment="RRFS_DADOS_DIR=$INSTALL_DIR/dados"
Environment="RRFS_GO2RTC_BIN=$GO2RTC_BIN"

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
    check_disk_space
    install_system_deps
    copy_files
    install_go2rtc
    setup_venv
    apply_permissions
    create_service
    start_service

    LOCAL_IP=$(hostname -I | awk '{print $1}' 2>/dev/null || echo "SEU_IP")

    print_header "INSTALAÇÃO CONCLUÍDA COM SUCESSO!"
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║${NC}   ✓  RRFS Cameras instalado e rodando!               ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}   Acesse: ${CYAN}http://${LOCAL_IP}:${PORT}${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════╝${NC}"
    echo ""
}

# ── Atualização ────────────────────────────────────────────────────────────
update() {
    print_header "ATUALIZAÇÃO RRFS CAMERAS"

    check_root

    if [ ! -d "$INSTALL_DIR" ]; then
        print_error "RRFS Cameras não está instalado em $INSTALL_DIR"
        exit 1
    fi

    stop_service

    BACKUP_DIR="${INSTALL_DIR}_backup_$(date +%Y%m%d_%H%M%S)"
    print_step "Fazendo backup em $BACKUP_DIR..."
    mkdir -p "$BACKUP_DIR"
    [ -d "$INSTALL_DIR/dados" ] && cp -r "$INSTALL_DIR/dados" "$BACKUP_DIR/"
    print_success "Backup criado"

    print_step "Removendo arquivos antigos (dados, venv e cache pip preservados)..."
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
        ! -name 'dados' \
        ! -name 'venv' \
        ! -name '.pip_cache' \
        -exec rm -rf {} + 2>/dev/null || true

    copy_files
    install_go2rtc

    print_step "Verificando integridade do ambiente virtual..."
    ensure_venv_ready

    print_step "Verificando bibliotecas Python (comparando com requirements.txt novo)..."
    ensure_requirements
    sync_requirements
    local SYNC_RESULT=$?
    print_success "Verificação de dependências concluída"

    apply_permissions

    # Recria o serviço para incluir RRFS_DADOS_DIR / RRFS_GO2RTC_BIN
    # (instalações antigas não têm essas variáveis no arquivo .service)
    create_service
    start_service

    print_header "ATUALIZAÇÃO CONCLUÍDA!"
    if [ $SYNC_RESULT -ne 0 ]; then
        print_warning "Atualização concluída, MAS algumas bibliotecas falharam."
    else
        print_success "RRFS Cameras atualizado com sucesso!"
    fi
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
    print_info "Saúde do ambiente virtual:"
    if [ -x "$env_DIR/bin/python3" ] && "$env_DIR/bin/python3" --version >/dev/null 2>&1; then
        print_success "venv íntegro ($("$env_DIR/bin/python3" --version 2>&1))"
    else
        print_error "venv QUEBRADO! Rode a opção 2 (Atualizar) para corrigir automaticamente."
    fi

    echo ""
    print_info "go2rtc (ponte iCSee/XMeye):"
    if [ -x "$GO2RTC_BIN" ]; then
        print_success "binário presente: $GO2RTC_BIN"
    else
        print_warning "binário ausente em $GO2RTC_BIN (só necessário p/ câmeras iCSee/XMeye)"
    fi
    if curl -fs --max-time 3 http://127.0.0.1:1984/api >/dev/null 2>&1; then
        print_success "go2rtc respondendo na porta 1984 (ativo)"
    else
        print_info "go2rtc não está ativo agora (inicia sozinho ao cadastrar câmera iCSee)"
    fi

    echo ""
    print_info "Cache de downloads do pip:"
    if [ -d "$PIP_CACHE_DIR" ]; then
        du -sh "$PIP_CACHE_DIR" 2>/dev/null | awk -v dir="$PIP_CACHE_DIR" '{print "    " $1 " em " dir}'
    fi

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
    echo "  5) Log do go2rtc (câmeras iCSee/XMeye)"
    echo "  6) Voltar"
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
        5)
            G2LOG="$INSTALL_DIR/dados/logs/go2rtc.log"
            if [ -f "$G2LOG" ]; then
                tail -50 "$G2LOG"
            else
                print_info "Ainda não há log do go2rtc (nenhuma câmera iCSee/XMeye cadastrada?)"
            fi
            echo ""
            echo "Pressione ENTER para continuar..."
            read
            ;;
        6) show_menu ;;
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
║         Sistema de Monitoramento de Câmeras IP                ║
║                  Instalador v2.7                              ║
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
