# MiniCart

MiniCart is a Python microservices e-commerce application with service-owned SQLAlchemy models, shared PostgreSQL development infrastructure, Protocol Buffer contracts, and a FastAPI API Gateway.

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
├── api-gateway/            # FastAPI REST-to-gRPC API Gateway
│   ├── gateway/             # Gateway app, request/response schemas, Order gRPC client
│   └── tests/               # Isolated API Gateway tests
├── product-service/        # Product/inventory models and DB access
│   ├── server.py           # Product Service gRPC server
│   ├── servicer.py         # Product RPC handlers
│   └── tests/              # Live gRPC/PostgreSQL integration tests
├── order-service/          # Order service, database access, and gRPC clients
│   ├── clients/            # Product and Payment Service gRPC clients
│   ├── migrations/         # Additive Order schema migrations
│   ├── server.py           # Order Service gRPC server
│   ├── servicer.py         # Order RPC handlers and workflow
│   └── tests/              # Live gRPC/PostgreSQL integration tests
├── payment-service/        # Payment model and DB access
│   ├── server.py           # Payment Service gRPC server
│   ├── servicer.py         # Payment RPC handlers
│   └── tests/              # Live gRPC/PostgreSQL integration tests
├── tests/
│   └── e2e/                # Cross-service gRPC/PostgreSQL workflows
├── protos/                 # Product, order, and payment RPC contracts
├── generated/              # Generated Python protobuf/gRPC package
├── docker-compose.yml      # Local PostgreSQL service and named volume
├── .env.example            # Placeholder local configuration
├── generate_protos.py      # Reproducible gRPC code generation
└── requirements.txt        # Complete development dependencies
```

Each service defines its own SQLAlchemy `Base` and owns its tables. The order and payment services intentionally do not declare foreign keys to tables owned by other services. The product inventory table references the product table within its own service.

## FastAPI API Gateway

The API Gateway exposes REST endpoints and forwards order operations to the Order Service over gRPC. It does not connect to PostgreSQL or duplicate order workflow logic.

Start PostgreSQL and all three services in separate terminals from the project root:

```powershell
docker compose up -d postgres
python product-service/server.py
python order-service/server.py
python payment-service/server.py
```

In another terminal, install the Gateway dependencies and start it:

```powershell
python -m pip install -r api-gateway/requirements.txt
python -m uvicorn gateway.main:app --app-dir api-gateway --host 127.0.0.1 --port 8000
```

The Order Service target defaults to `localhost:50052`. Configure `ORDER_SERVICE_ADDRESS` to override it, or use `ORDER_SERVICE_HOST` and `ORDER_SERVICE_PORT`; `ORDER_SERVICE_TIMEOUT_SECONDS` configures the gRPC deadline.

REST endpoints:

- `GET /health` checks only that the Gateway is responding.
- `POST /orders` accepts `{"items":[{"product_id":1,"quantity":2}]}` and returns the Order Service result.
- `GET /orders/{order_id}` retrieves an order through the Order Service.

Open Swagger UI at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). The OpenAPI schema is at [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json).

Run the Gateway tests from the project root:

```powershell
python -m pytest api-gateway/tests/ -v
```

These tests use a fake Order Service client to verify request/response conversion, REST validation, HTTP error mapping, and Swagger/OpenAPI availability without requiring running microservices.

## Product Service

The Product Service implements the `AddProduct`, `GetProduct`, `CheckStock`, and `UpdateStock` gRPC methods from `protos/product.proto`. Add-product creates the product and its inventory in one transaction. Stock updates lock the inventory row and reject changes that would result in negative stock. `CheckStock` reports `product_exists=false` and zero stock for an unknown product; product retrieval and stock updates return gRPC `NOT_FOUND` for unknown IDs.

Start the server from the project root in Terminal 1:

```powershell
python product-service/server.py
```

In Terminal 2, install the project dependencies if needed and run the persistent gRPC integration tests:

```powershell
python -m pip install -r requirements.txt
python -m pytest product-service/tests/ -v
```

The tests require PostgreSQL (`docker compose up -d postgres`) and the Product Service server to be running. They connect using a real gRPC client, verify database persistence, and remove only the uniquely created test products and their inventory records after each test. Set `PRODUCT_SERVICE_ADDRESS` to override the test target (default `127.0.0.1:50051`).

Product Service verification: all 11 gRPC/PostgreSQL integration test cases passed, including validation, inventory creation, stock updates, not-found behavior, and persisted changes.

## Order Service

The Order Service implements `CreateOrder` and `GetOrder` from `protos/order.proto`. It obtains product prices and stock through Product Service gRPC calls, saves the order and item prices in its own database, then calls Payment Service with the generated order ID and calculated total. A successful payment marks the order `PAID` and reduces stock through Product Service; a failed payment marks it `PAYMENT_FAILED` and leaves stock unchanged.

The `unit_price` in the current `OrderItemInput` request is ignored: the Product Service price is authoritative. `Order` responses include the payment ID and Payment Service status (`SUCCESS` or `FAILED`) when a payment was processed. Older orders without payment details return the protobuf defaults.

Existing databases need the additive payment-details migration once. From the project root, run:

```powershell
Get-Content order-service/migrations/0001_add_payment_details.sql | docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

The migration adds nullable columns only and does not create a cross-service foreign key.

Start PostgreSQL and all three services from the project root, each in its own terminal:

```powershell
docker compose up -d postgres
python product-service/server.py
python order-service/server.py
python payment-service/server.py
```

Default gRPC ports are Product `50051`, Order `50052`, and Payment `50053`. Set `PRODUCT_SERVICE_ADDRESS` or `PAYMENT_SERVICE_ADDRESS` to override client targets; `PRODUCT_SERVICE_PORT`, `ORDER_SERVICE_PORT`, and `PAYMENT_SERVICE_PORT` configure server/default client ports. Client call timeouts are configurable with `PRODUCT_SERVICE_TIMEOUT_SECONDS` and `PAYMENT_SERVICE_TIMEOUT_SECONDS`.

With all three services running, install dependencies and run the live integration tests:

```powershell
python -m pip install -r requirements.txt
python -m pytest order-service/tests/ -v
```

The tests use real gRPC clients and the running Product and Payment Services. They create uniquely named products and clean up only their own order, payment, product, inventory, and item records. Product and Payment Service outage cases use isolated temporary Order Service gRPC listeners while retaining the real dependent service for the other call.

Order workflow caveat: PostgreSQL and the two downstream gRPC services do not share a distributed transaction. If payment succeeds but Product Service subsequently fails to decrement stock, the order remains `PAID` and the RPC reports the stock-update error. With multiple products, a later stock-update failure can also occur after earlier product stocks were decremented. Payment reconciliation, compensation/refunds, and retries are not implemented.

## End-to-end integration tests

Start PostgreSQL and all three services in separate terminals from the project root:

```powershell
docker compose up -d postgres
python product-service/server.py
python order-service/server.py
python payment-service/server.py
```

Run the end-to-end workflows from another terminal:

```powershell
python -m pytest tests/e2e/ -v
```

These tests use real gRPC clients against ports `50051`, `50052`, and `50053`. The success example creates a product at `19.95` with stock `10`, places an order for `2`, verifies the `39.90` total and actual payment record, and confirms stock falls to `8`. The failure example arranges an odd order ID, then verifies `PAYMENT_FAILED` / `FAILED`, a returned payment ID, and stock remaining at `10`. Parity setup uses disposable orders through the actual gRPC API.

Expected rows during a successful flow: one product and inventory row (stock updated from `10` to `8`), one `PAID` order with its item and payment IDs, and one `SUCCESS` payment for the order total. A failed-payment flow creates the same rows, but stores `PAYMENT_FAILED` / `FAILED` and leaves inventory at `10`. Test-created rows are removed during teardown.

Error coverage includes missing products, insufficient stock, invalid quantities, and Product or Payment Service unavailability. The tests verify records across `products`, `inventory`, `orders`, `order_items`, and `payments`, then delete only records created by the tests. Unavailability cases use isolated temporary Order Service gRPC listeners while calling the real remaining services.

## Payment Service

The Payment Service implements `ProcessPayment` and `GetPaymentStatus` from `protos/payment.proto`.

Start the server from the project root in Terminal 1:

```powershell
python payment-service/server.py
```

The service listens on gRPC port `50053`.

In Terminal 2, run the live gRPC and PostgreSQL integration tests:

```powershell
python -m pip install -r requirements.txt
python -m pytest payment-service/tests/ -v
```

Set `PAYMENT_SERVICE_ADDRESS` to override the test target (default `127.0.0.1:50053`). The tests require PostgreSQL on port `5432` and a running Payment Service.

Payment processing is a deterministic simulation: a positive, even `order_id` produces `SUCCESS`; a positive, odd `order_id` produces `FAILED`. The payment amount must be positive and valid with at most two decimal places. Both successful and failed results are committed to PostgreSQL and can be retrieved with `GetPaymentStatus`.

Test records are assigned unique database IDs. Cleanup removes only the payment records created by the test fixture.

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

The database listens on local port `5432`; Compose stores its data in the `minicart_postgres_data` named volume. The Python database modules default to `DB_HOST=localhost` for local execution. When a service runs in a Compose network, configure `DB_HOST=postgres`. For an existing volume, the `DB_PASSWORD` must match the password already configured for the database role; changing `.env` alone does not reset that password.

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

The script also generates protobuf `.pyi` files so Pylance can resolve generated message classes. The generated gRPC modules use package-relative imports so they can be imported as `generated.product_pb2`, `generated.product_pb2_grpc`, and similarly for order and payment.

## Setup verification

- PostgreSQL started with Compose and reported healthy on local port `5432`.
- SQLAlchemy connected from all three service configurations and created their owned tables.
- The database contains `products`, `inventory`, `orders`, `order_items`, and `payments`.
- gRPC code generation completed; all generated protobuf modules, service stubs, servicers, and RPC methods imported successfully.
- A temporary persistence marker remained after restarting PostgreSQL; the marker table was removed after the check.
- Payment Service passed 13 live gRPC/PostgreSQL integration test cases on port `50053`.
- Order Service passed 12 live gRPC integration test cases, including persisted payment IDs/statuses for successful and failed payments. Existing protobuf field numbers were preserved; Payment Service ran on temporary port `50054` because port `50053` was already occupied.
- E2E suite passed all 8 live cross-service tests using the existing Payment Service on port `50053`.
