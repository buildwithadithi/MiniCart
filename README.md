# MiniCart

MiniCart is the initial project setup for a Python microservices e-commerce application. It establishes service-owned SQLAlchemy models, shared PostgreSQL development infrastructure, and Protocol Buffer contracts without implementing service or API business logic.

## Technology stack

- Python
- gRPC and Protocol Buffers for internal service contracts
- FastAPI and Uvicorn for the future API gateway
- PostgreSQL, SQLAlchemy 2, and psycopg2-binary
- Docker Compose for local PostgreSQL

Install the complete development dependencies with `python -m pip install -r requirements.txt`. Each service and the API gateway also has its own `requirements.txt`.

## Project structure

```text
minicart/
├── api-gateway/            # Gateway dependencies (no endpoints yet)
├── product-service/        # Product/inventory models and DB access
├── order-service/          # Order/order-item models and DB access
├── payment-service/        # Payment model and DB access
├── protos/                 # Product, order, and payment RPC contracts
├── generated/              # Generated Python protobuf/gRPC package
├── docker-compose.yml      # Local PostgreSQL service and named volume
├── .env.example            # Placeholder local configuration
├── generate_protos.py      # Reproducible gRPC code generation
└── requirements.txt        # Complete development dependencies
```

Each service defines its own SQLAlchemy `Base` and owns its tables. The order and payment services intentionally do not declare foreign keys to tables owned by other services. The product inventory table references the product table within its own service.

## PostgreSQL

Copy `.env.example` to `.env` and set a non-placeholder `DB_PASSWORD`. The `.env` file is ignored by Git.

Start PostgreSQL and wait for its health check:

```powershell
docker compose up -d postgres
docker compose ps
```

Stop the container without removing persistent data:

```powershell
docker compose down
```

The database listens on local port `5432`; Compose stores its data in the `minicart_postgres_data` named volume. The Python database modules default to `DB_HOST=localhost` for local execution. When a service runs in a Compose network, configure `DB_HOST=postgres`.

The service tables can be created from each service directory after installing dependencies and configuring `.env`:

```powershell
python -c "from database import Base, engine; import models; Base.metadata.create_all(bind=engine)"
```

Run that command once from each of `product-service`, `order-service`, and `payment-service`. Use a service's `SessionLocal.begin()` context for transactional database work when business logic is added.

## gRPC code generation

Generate the Python protobuf and gRPC modules into the `generated` package:

```powershell
python generate_protos.py
```

The generated gRPC modules use package-relative imports so they can be imported as `generated.product_pb2`, `generated.product_pb2_grpc`, and similarly for order and payment.

## Setup verification

- PostgreSQL started with Compose and reported healthy on local port `5432`.
- SQLAlchemy connected from all three service configurations and created their owned tables.
- The database contains `products`, `inventory`, `orders`, `order_items`, and `payments`.
- gRPC code generation completed; all generated protobuf modules, service stubs, servicers, and RPC methods imported successfully.
- A temporary persistence marker remained after restarting PostgreSQL; the marker table was removed after the check.
