# Clean Architecture Implementation

This project has been refactored to follow **Clean Architecture** principles, ensuring better maintainability, testability, and separation of concerns.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Presentation Layer                   │
│              (FastAPI Controllers)                      │
│  src/api/auth.py, main_clean.py                  │
└─────────────────────┬───────────────────────────────────┘
                      │ depends on
┌─────────────────────▼───────────────────────────────────┐
│                  Application Layer                      │
│        (Use Cases & Application Services)               │
│  src/application/use_cases/, src/application/services/  │
└─────────────────────┬───────────────────────────────────┘
                      │ depends on
┌─────────────────────▼───────────────────────────────────┐
│                    Domain Layer                         │
│          (Entities, Repositories, Services)             │
│  src/domain/entities/, src/domain/repositories/,        │
│  src/domain/services/                                   │
└─────────────────────▲───────────────────────────────────┘
                      │ implemented by
┌─────────────────────┴───────────────────────────────────┐
│                Infrastructure Layer                     │
│    (Database, External Services, Implementations)       │
│  src/infrastructure/repositories/,                      │
│  src/infrastructure/services/, src/infrastructure/di/   │
└─────────────────────────────────────────────────────────┘
```

## Layer Responsibilities

### 1. Domain Layer (Core Business Logic)
- **Entities**: Pure business objects (`src/domain/entities/user.py`)
- **Repository Interfaces**: Contracts for data access (`src/domain/repositories/`)
- **Service Interfaces**: Contracts for external services (`src/domain/services/`)
- **No framework dependencies** - pure Python business logic

### 2. Application Layer (Use Cases)
- **Use Cases**: Specific business scenarios (`src/application/use_cases/`)
- **Application Services**: Orchestrate use cases (`src/application/services/`)
- **Depends only on domain abstractions**

### 3. Infrastructure Layer (External Concerns)
- **Repository Implementations**: Database access (`src/infrastructure/repositories/`)
- **Service Implementations**: External services (`src/infrastructure/services/`)
- **Database Models**: SQLAlchemy models (`src/infrastructure/persistence/`)
- **Dependency Injection**: Wiring (`src/infrastructure/di/`)

### 4. Presentation Layer (API/Web)
- **API Controllers**: HTTP endpoints (`src/api/auth.py`)
- **Request/Response Models**: DTOs (`src/core/schemas.py`)
- **Framework-specific code** (FastAPI)

## Key Clean Architecture Principles Implemented

### ✅ Dependency Inversion
- High-level modules don't depend on low-level modules
- Both depend on abstractions (interfaces)
- Example: `AuthApplicationService` depends on `UserRepository` interface, not SQLAlchemy implementation

### ✅ Separation of Concerns
- Each layer has a single responsibility
- Domain logic is separate from infrastructure concerns
- Business rules are not coupled to frameworks

### ✅ Independent of Frameworks
- Domain layer has no framework dependencies
- Can swap out FastAPI for Django without changing business logic
- Can switch from SQLAlchemy to MongoDB without affecting domain

### ✅ Independent of Database
- Domain entities are not SQLAlchemy models
- Repository pattern abstracts data access
- Easy to switch databases or add caching

### ✅ Testable
- Business logic can be unit tested without database
- Infrastructure can be mocked easily
- Clear boundaries make testing simpler

## File Structure

```
src/
├── domain/                     # Domain Layer (Core)
│   ├── entities/
│   │   └── user.py            # Pure User entity
│   ├── repositories/
│   │   └── user_repository.py # Repository interface
│   └── services/
│       ├── password_service.py # Password service interface
│       └── token_service.py   # Token service interface
│
├── application/               # Application Layer
│   ├── use_cases/
│   │   └── auth_use_cases.py  # Authentication use cases
│   └── services/
│       └── auth_application_service.py # Main application service
│
├── infrastructure/            # Infrastructure Layer
│   ├── persistence/
│   │   ├── models.py         # SQLAlchemy models
│   │   └── mappers.py        # Domain ↔ Model mappers
│   ├── repositories/
│   │   └── sqlalchemy_user_repository.py # Repository implementation
│   ├── services/
│   │   ├── bcrypt_password_service.py     # Password implementation
│   │   └── jwt_token_service.py           # Token implementation
│   ├── database/
│   │   └── database_initializer.py        # DB setup
│   └── di/
│       └── container.py      # Dependency injection
│
├── api/                      # Presentation Layer
│   ├── auth.py              # Legacy endpoints
│   └── auth.py        # Clean architecture endpoints
│
├── core/                    # Shared (transitional)
│   ├── schemas.py          # Request/Response DTOs
│   └── database.py         # Database connection
│
└── shared/                  # Shared utilities
    ├── config.py           # Configuration
    ├── handlers.py         # Exception handlers
    ├── jwt.py             # JWT utilities (legacy)
    └── redis_whitelist.py # Redis integration
```

## Running the Clean Architecture Version

### Start the Clean Architecture API:
```bash
# From the user service directory
python main_clean.py
```

### API Endpoints:
- **Clean Architecture**: `http://localhost:8001/api/v1/auth/`
- **Legacy**: `http://localhost:8001/api/v1/auth/` (still available)
- **Documentation**: `http://localhost:8001/docs`

## Benefits Achieved

### 1. **Maintainability**
- Clear separation of concerns
- Easy to locate and modify business logic
- Changes in one layer don't affect others

### 2. **Testability**
- Pure domain logic can be unit tested easily
- Infrastructure can be mocked
- No need for databases in unit tests

### 3. **Flexibility**
- Easy to swap implementations
- Add new features without modifying existing code
- Support multiple persistence strategies

### 4. **Scalability**
- Clear boundaries for team responsibilities
- Easy to add new use cases
- Infrastructure can be scaled independently

## Example Usage

```python
# Business logic is pure and testable
user = User(
    username="testuser",
    email="test@example.com",
    password_hash="hashed_password"
)

# Dependencies are injected
auth_service = container.get_auth_application_service(session)
result = auth_service.register_user(request)
```

## Migration Strategy

Both legacy and clean architecture implementations run side by side:
1. **Legacy endpoints**: Use existing `src/api/auth.py`
2. **Clean endpoints**: Use new `src/api/auth.py`
3. **Gradual migration**: Move clients to clean endpoints over time
4. **Eventually**: Remove legacy code

This allows for safe, gradual adoption of clean architecture principles.
