#!/bin/bash

# =============================================================================
# COMPLETE MANAGEMENT SCRIPT
# Handles all operations for the optimized Docker setup
# =============================================================================

set -e

# Configuration
COMPOSE_FILE="docker-compose.yml"
ENV_FILE=".env"
DOCKERFILE="Dockerfile"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Logging functions
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_debug() { echo -e "${PURPLE}[DEBUG]${NC} $1"; }
log_step() { echo -e "${CYAN}[STEP]${NC} $1"; }

# Print usage information
print_usage() {
    echo -e "${CYAN}Complete Docker Management Script${NC}"
    echo ""
    echo "Usage: $0 [COMMAND] [OPTIONS]"
    echo ""
    echo -e "${YELLOW}Environment Commands:${NC}"
    echo "  setup           Initial setup (copy .env, build images)"
    echo "  dev             Start development environment"
    echo "  prod            Start production environment"
    echo "  stop            Stop all services"
    echo "  restart         Restart all services"
    echo "  status          Show service status"
    echo ""
    echo -e "${YELLOW}Build Commands:${NC}"
    echo "  build           Build all images"
    echo "  build-dev       Build development image"
    echo "  build-prod      Build production image"
    echo "  rebuild         Force rebuild all images"
    echo ""
    echo -e "${YELLOW}Database Commands:${NC}"
    echo "  db-reset        Reset database (WARNING: destructive)"
    echo "  db-migrate      Run database migrations"
    echo "  db-seed         Seed database with sample data"
    echo "  db-backup       Backup database"
    echo "  db-restore [file] Restore database from backup"
    echo ""
    echo -e "${YELLOW}Testing Commands:${NC}"
    echo "  test            Run all tests"
    echo "  test-unit       Run unit tests"
    echo "  test-integration Run integration tests"
    echo "  lint            Run code linting"
    echo "  format          Format code"
    echo ""
    echo -e "${YELLOW}Monitoring Commands:${NC}"
    echo "  monitoring      Start monitoring stack"
    echo "  logs            Show service logs"
    echo "  logs [service]  Show logs for specific service"
    echo "  health          Check service health"
    echo ""
    echo -e "${YELLOW}Utility Commands:${NC}"
    echo "  clean           Clean up containers and images"
    echo "  reset           Reset entire environment"
    echo "  shell [service] Open shell in service container"
    echo "  ps              Show running containers"
    echo "  config          Validate docker-compose configuration"
    echo ""
    echo -e "${YELLOW}Examples:${NC}"
    echo "  $0 setup          # Initial setup"
    echo "  $0 dev            # Start development"
    echo "  $0 test           # Run tests"
    echo "  $0 logs user-service  # Show user service logs"
    echo "  $0 shell postgres     # Open PostgreSQL shell"
}

# Check prerequisites
check_prerequisites() {
    log_step "Checking prerequisites..."
    
    # Check Docker
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed"
        exit 1
    fi
    
    # Check Docker Compose
    if ! command -v docker-compose &> /dev/null; then
        log_error "Docker Compose is not installed"
        exit 1
    fi
    
    # Check if Docker is running
    if ! docker info &> /dev/null; then
        log_error "Docker daemon is not running"
        exit 1
    fi
    
    log_success "Prerequisites check passed"
}

# Setup environment
setup_environment() {
    log_step "Setting up environment..."
    
    # Copy environment file if it doesn't exist
    if [ ! -f "$ENV_FILE" ]; then
        if [ -f ".env.example" ]; then
            cp .env.example "$ENV_FILE"
            log_success "Created $ENV_FILE from .env.example"
            log_warning "Please review and customize $ENV_FILE"
        else
            log_error ".env.example not found"
            exit 1
        fi
    else
        log_info "$ENV_FILE already exists"
    fi
    
    # Create necessary directories
    mkdir -p logs sql monitoring/prometheus monitoring/grafana/dashboards monitoring/grafana/datasources
    
    # Build images
    build_images
    
    log_success "Environment setup complete"
}

# Build images
build_images() {
    log_step "Building Docker images..."
    docker-compose build --parallel
    log_success "Images built successfully"
}

# Build development image
build_dev() {
    log_step "Building development image..."
    docker-compose build --build-arg BUILD_TARGET=development user-service
    log_success "Development image built"
}

# Build production image
build_prod() {
    log_step "Building production image..."
    docker-compose build --build-arg BUILD_TARGET=production user-service
    log_success "Production image built"
}

# Rebuild all images
rebuild_images() {
    log_step "Rebuilding all images..."
    docker-compose build --no-cache --parallel
    log_success "Images rebuilt successfully"
}

# Start development environment
start_dev() {
    log_step "Starting development environment..."
    
    # Set environment for development
    export BUILD_TARGET=development
    export APP_ENV=development
    
    # Start services
    docker-compose up -d postgres redis
    log_info "Waiting for database to be ready..."
    sleep 10
    
    docker-compose up -d kong-migrations
    docker-compose wait kong-migrations
    
    docker-compose up -d kong user-service
    
    log_success "Development environment started!"
    log_info "Services available at:"
    log_info "  - User Service: http://localhost:8080"
    log_info "  - Kong Proxy: http://localhost:8000"
    log_info "  - Kong Admin: http://localhost:8001"
    log_info "  - Kong Manager: http://localhost:8002"
}

# Start production environment
start_prod() {
    log_step "Starting production environment..."
    
    # Set environment for production
    export BUILD_TARGET=production
    export APP_ENV=production
    
    # Start services
    docker-compose up -d
    
    log_success "Production environment started!"
}

# Stop services
stop_services() {
    log_step "Stopping services..."
    docker-compose down
    log_success "Services stopped"
}

# Restart services
restart_services() {
    log_step "Restarting services..."
    docker-compose restart
    log_success "Services restarted"
}

# Show service status
show_status() {
    log_step "Service status:"
    docker-compose ps
    echo ""
    log_step "Health checks:"
    docker-compose exec -T postgres pg_isready -U kong || log_warning "PostgreSQL not ready"
    docker-compose exec -T redis redis-cli ping || log_warning "Redis not ready"
    curl -s http://localhost:8001/status > /dev/null && log_success "Kong is healthy" || log_warning "Kong not ready"
    curl -s http://localhost:8080/health > /dev/null && log_success "User service is healthy" || log_warning "User service not ready"
}

# Database operations
db_reset() {
    log_warning "This will completely reset the database!"
    read -p "Are you sure? (y/N): " -n 1 -r
    echo
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_step "Resetting database..."
        docker-compose down -v
        docker volume rm $(basename $PWD)_postgres_data 2>/dev/null || true
        start_dev
        log_success "Database reset complete"
    else
        log_info "Database reset cancelled"
    fi
}

db_migrate() {
    log_step "Running database migrations..."
    docker-compose up -d postgres
    sleep 5
    docker-compose run --rm kong-migrations
    log_success "Database migrations complete"
}

db_backup() {
    local backup_file="backup_$(date +%Y%m%d_%H%M%S).sql"
    log_step "Creating database backup: $backup_file"
    docker-compose exec -T postgres pg_dump -U kong kong > "$backup_file"
    log_success "Database backed up to $backup_file"
}

db_restore() {
    local backup_file="$1"
    if [ -z "$backup_file" ] || [ ! -f "$backup_file" ]; then
        log_error "Please specify a valid backup file"
        exit 1
    fi
    
    log_step "Restoring database from $backup_file"
    docker-compose exec -T postgres psql -U kong -d kong < "$backup_file"
    log_success "Database restored from $backup_file"
}

# Testing operations
run_tests() {
    log_step "Running all tests..."
    docker-compose run --rm --build user-service pytest tests/ -v
    log_success "Tests completed"
}

run_unit_tests() {
    log_step "Running unit tests..."
    docker-compose run --rm user-service pytest tests/unit/ -v
    log_success "Unit tests completed"
}

run_integration_tests() {
    log_step "Running integration tests..."
    docker-compose up -d postgres redis
    sleep 10
    docker-compose run --rm user-service pytest tests/integration/ -v
    log_success "Integration tests completed"
}

run_lint() {
    log_step "Running code linting..."
    docker-compose run --rm --build -e BUILD_TARGET=lint user-service
    log_success "Linting completed"
}

format_code() {
    log_step "Formatting code..."
    docker-compose run --rm user-service black src/
    docker-compose run --rm user-service isort src/
    log_success "Code formatting completed"
}

# Monitoring operations
start_monitoring() {
    log_step "Starting monitoring stack..."
    docker-compose --profile monitoring up -d prometheus grafana
    log_success "Monitoring stack started!"
    log_info "  - Prometheus: http://localhost:9091"
    log_info "  - Grafana: http://localhost:3001 (admin/admin)"
}

# Logging operations
show_logs() {
    local service="$1"
    if [ -n "$service" ]; then
        log_step "Showing logs for $service..."
        docker-compose logs -f "$service"
    else
        log_step "Showing logs for all services..."
        docker-compose logs -f
    fi
}

# Health check
health_check() {
    log_step "Checking service health..."
    
    # Check each service
    services=("postgres" "redis" "kong" "user-service")
    
    for service in "${services[@]}"; do
        if docker-compose ps "$service" | grep -q "Up.*healthy\|Up.*starting"; then
            log_success "$service is healthy"
        else
            log_warning "$service is not healthy"
        fi
    done
}

# Utility operations
clean_docker() {
    log_step "Cleaning up Docker resources..."
    
    # Stop services
    docker-compose down
    
    # Remove unused images
    docker image prune -f
    
    # Remove unused volumes
    docker volume prune -f
    
    # Remove unused networks
    docker network prune -f
    
    log_success "Docker cleanup complete"
}

reset_environment() {
    log_warning "This will reset the entire environment!"
    read -p "Are you sure? (y/N): " -n 1 -r
    echo
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_step "Resetting environment..."
        
        # Stop and remove everything
        docker-compose down -v --remove-orphans
        
        # Remove project images
        docker images | grep "$(basename $PWD)" | awk '{print $3}' | xargs docker rmi -f 2>/dev/null || true
        
        # Clean up
        clean_docker
        
        log_success "Environment reset complete"
    else
        log_info "Reset cancelled"
    fi
}

open_shell() {
    local service="$1"
    if [ -z "$service" ]; then
        log_error "Please specify a service name"
        exit 1
    fi
    
    log_step "Opening shell in $service..."
    docker-compose exec "$service" /bin/bash || docker-compose exec "$service" /bin/sh
}

show_containers() {
    log_step "Running containers:"
    docker-compose ps
}

validate_config() {
    log_step "Validating docker-compose configuration..."
    docker-compose config --quiet && log_success "Configuration is valid" || log_error "Configuration has errors"
}

# Main command handler
main() {
    case "${1:-}" in
        setup)
            check_prerequisites
            setup_environment
            ;;
        dev)
            check_prerequisites
            start_dev
            ;;
        prod)
            check_prerequisites
            start_prod
            ;;
        stop)
            stop_services
            ;;
        restart)
            restart_services
            ;;
        status)
            show_status
            ;;
        build)
            build_images
            ;;
        build-dev)
            build_dev
            ;;
        build-prod)
            build_prod
            ;;
        rebuild)
            rebuild_images
            ;;
        db-reset)
            db_reset
            ;;
        db-migrate)
            db_migrate
            ;;
        db-backup)
            db_backup
            ;;
        db-restore)
            db_restore "$2"
            ;;
        test)
            run_tests
            ;;
        test-unit)
            run_unit_tests
            ;;
        test-integration)
            run_integration_tests
            ;;
        lint)
            run_lint
            ;;
        format)
            format_code
            ;;
        monitoring)
            start_monitoring
            ;;
        logs)
            show_logs "$2"
            ;;
        health)
            health_check
            ;;
        clean)
            clean_docker
            ;;
        reset)
            reset_environment
            ;;
        shell)
            open_shell "$2"
            ;;
        ps)
            show_containers
            ;;
        config)
            validate_config
            ;;
        help|--help|-h)
            print_usage
            ;;
        "")
            print_usage
            ;;
        *)
            log_error "Unknown command: $1"
            print_usage
            exit 1
            ;;
    esac
}

# Run main function
main "$@"