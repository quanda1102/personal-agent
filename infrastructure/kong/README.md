# Kong API Gateway Configuration

This directory contains Kong API Gateway configuration files and management scripts for the platform backend API.

## 📁 File Structure

```
kong/
├── kong.yml              # Main Kong declarative configuration
├── kong-dev.yml          # Development-specific configuration
├── kong.conf             # Kong configuration file
├── manage-kong.sh        # Kong management script (Unix/Linux/macOS)
├── plugins/              # Custom Kong plugins
│   └── kong-auth-validator/
│       ├── handler.lua   # Plugin logic
│       └── schema.lua    # Plugin schema
└── README.md            # This file
```

## 🚀 Quick Start

### 1. Start the Environment

```bash
# Navigate to docker directory
cd ../docker

# Start all services including Kong
docker-compose up -d

# Check if services are running
docker-compose ps
```

### 2. Verify Kong is Running

```bash
# Check Kong admin API
curl http://localhost:8001/status

# Check Kong proxy
curl http://localhost:8000/
```

### 3. Test Authentication Endpoints

```bash
# Register a new user (via Kong proxy)
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "email": "test@example.com", 
    "password": "password123"
  }'

# Login to get token
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "password123"
  }'

# Test token validation (Kong internal)
curl -X POST http://localhost:8000/api/v1/auth/validate \
  -H "Content-Type: application/json" \
  -d '{
    "token": "your-jwt-token-here"
  }'
```

## 🔧 Configuration Management

### Using Kong Admin API

```bash
# Reload configuration
curl -X POST http://localhost:8001/config \
  -F "config=@kong.yml"

# Check services
curl http://localhost:8001/services

# Check routes  
curl http://localhost:8001/routes

# Check plugins
curl http://localhost:8001/plugins
```

### Using Management Script (Unix/Linux/macOS)

```bash
# Make script executable (Unix/Linux/macOS only)
chmod +x manage-kong.sh

# Check Kong status
./manage-kong.sh status

# Reload configuration
./manage-kong.sh reload

# Reload with development config
./manage-kong.sh reload-dev

# Validate configuration
./manage-kong.sh validate

# List services
./manage-kong.sh services

# List routes
./manage-kong.sh routes

# Show help
./manage-kong.sh help
```

### Windows PowerShell Commands

```powershell
# Check Kong status
Invoke-RestMethod -Uri "http://localhost:8001/status"

# Reload configuration
$form = @{
    config = Get-Item -Path "kong.yml"
}
Invoke-RestMethod -Uri "http://localhost:8001/config" -Method Post -Form $form

# List services
Invoke-RestMethod -Uri "http://localhost:8001/services"

# List routes
Invoke-RestMethod -Uri "http://localhost:8001/routes"
```

## 🔐 Authentication Flow

### 1. Public Endpoints (No Auth Required)
- `POST /api/v1/auth/register` - User registration
- `POST /api/v1/auth/login` - User login
- `POST /api/v1/auth/introspect` - OAuth2 token introspection

### 2. Protected Endpoints (Auth Required)
- `GET /api/v1/auth/refresh` - Refresh tokens
- `GET /api/v1/auth/logout` - User logout
- `GET /api/v1/auth/user-info` - Get user information
- `GET /api/v1/auth/permissions` - Get user permissions

### 3. Internal Endpoints (Kong Only)
- `POST /api/v1/auth/validate` - Token validation for Kong

## 🔌 Kong Plugins

### Built-in Plugins Used
- **CORS**: Cross-origin resource sharing
- **Rate Limiting**: API rate limiting
- **Request ID**: Request tracing
- **File Log**: Request logging
- **IP Restriction**: IP-based access control
- **Request Size Limiting**: Payload size limits

### Custom Plugins
- **kong-auth-validator**: Custom authentication plugin that validates JWT tokens against the auth service and adds user context headers

## 🌐 Port Configuration

| Service | Port | Description |
|---------|------|-------------|
| Kong Proxy | 8000 | Main API gateway endpoint |
| Kong Admin API | 8001 | Kong administration |
| Kong Manager | 8002 | Kong GUI management |
| Kong Dev Portal | 8003 | Developer portal |
| User Service | 8080 | Direct access to user service |
| PostgreSQL | 5432 | Database |
| Redis | 6379 | Cache |

## 🏗️ Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Client App    │────▶│   Kong Gateway  │────▶│  User Service   │
│                 │     │   (Port 8000)   │     │  (Port 8080)    │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │                          │
                               ▼                          ▼
                        ┌─────────────────┐     ┌─────────────────┐
                        │ Kong Admin API  │     │   PostgreSQL    │
                        │  (Port 8001)    │     │   (Port 5432)   │
                        └─────────────────┘     └─────────────────┘
                               │                          │
                               ▼                          ▼
                        ┌─────────────────┐     ┌─────────────────┐
                        │ Kong Manager    │     │     Redis       │
                        │  (Port 8002)    │     │   (Port 6379)   │
                        └─────────────────┘     └─────────────────┘
```

## 🔍 Troubleshooting

### Common Issues

1. **Kong won't start**
   ```bash
   # Check database migration
   docker logs kong-migrations
   
   # Check Kong logs
   docker logs kong-gateway
   ```

2. **Configuration errors**
   ```bash
   # Validate configuration
   docker exec kong-gateway kong config parse /etc/kong/kong.yml
   ```

3. **Plugin not loading**
   ```bash
   # Check plugin path and permissions
   docker exec kong-gateway ls -la /usr/local/share/lua/5.1/kong/plugins/custom/
   ```

4. **Database connection issues**
   ```bash
   # Test database connection
   docker exec kong-gateway kong config db_import /etc/kong/kong.yml
   ```

### Log Files

```bash
# Kong access logs
docker logs kong-gateway

# Kong error logs  
docker exec kong-gateway tail -f /usr/local/kong/logs/error.log

# User service logs
docker logs user-service
```

## 🚀 Production Deployment

### Environment Variables

For production deployment, set these environment variables:

```bash
# Kong
KONG_ADMIN_LISTEN=127.0.0.1:8001  # Restrict admin access
KONG_ADMIN_GUI_LISTEN=off          # Disable GUI in production
KONG_ANONYMOUS_REPORTS=off         # Disable telemetry

# SSL
KONG_SSL_CERT=/path/to/cert.pem
KONG_SSL_CERT_KEY=/path/to/key.pem

# Database
KONG_PG_HOST=your-prod-db-host
KONG_PG_PASSWORD=your-secure-password

# Security
KONG_TRUSTED_IPS=your-load-balancer-ips
```

### Security Checklist

- [ ] Enable SSL/TLS certificates
- [ ] Restrict Kong Admin API access
- [ ] Configure proper CORS origins
- [ ] Set up rate limiting
- [ ] Configure IP restrictions
- [ ] Enable request logging
- [ ] Set up monitoring and alerting
- [ ] Regular security updates

## 📚 Additional Resources

- [Kong Documentation](https://docs.konghq.com/)
- [Kong Admin API](https://docs.konghq.com/gateway/api/admin-ee/)
- [Kong Plugin Development](https://docs.konghq.com/gateway/latest/plugin-development/)
- [Declarative Configuration](https://docs.konghq.com/gateway/latest/reference/db-less-and-declarative-config/)
