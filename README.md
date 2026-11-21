# MiniCart

MiniCart is a Python microservices e-commerce application with service-owned SQLAlchemy models, shared PostgreSQL development infrastructure, Protocol Buffer contracts, and a FastAPI API Gateway.

## Technology stack

- Python
- gRPC and Protocol Buffers for internal service contracts
- FastAPI and Uvicorn for the REST API gateway
- PostgreSQL, SQLAlchemy 2, and psycopg2-binary
- Docker Compose for local PostgreSQL

Each component has a separate `requirements.txt`; see [Environment and dependency management](#environment-and-dependency-management) for local setup and Docker instructions.

## Project structure

```text
minicart/
├── api-gateway/            # FastAPI REST-to-gRPC API Gateway
│   ├── Dockerfile          # Gateway image with Gateway-only dependencies
│   ├── gateway/             # Gateway app, request/response schemas, Order gRPC client
│   └── tests/               # Isolated API Gateway tests
├── product-service/        # Product/inventory models and DB access
│   ├── Dockerfile          # Product image with Product-only dependencies
│   ├── server.py           # Product Service gRPC server
│   ├── servicer.py         # Product RPC handlers
│   └── tests/              # Live gRPC/PostgreSQL integration tests
├── order-service/          # Order service, database access, and gRPC clients
│   ├── Dockerfile          # Order image with Order-only dependencies
│   ├── clients/            # Product and Payment Service gRPC clients
│   ├── migrations/         # Additive Order schema migrations
│   ├── server.py           # Order Service gRPC server
│   ├── servicer.py         # Order RPC handlers and workflow
│   └── tests/              # Live gRPC/PostgreSQL integration tests
├── payment-service/        # Payment model and DB access
│   ├── Dockerfile          # Payment image with Payment-only dependencies
│   ├── server.py           # Payment Service gRPC server
│   ├── servicer.py         # Payment RPC handlers
│   └── tests/              # Live gRPC/PostgreSQL integration tests
├── tests/
│   └── e2e/                # Cross-service gRPC/PostgreSQL workflows
├── protos/                 # Product, order, and payment RPC contracts
├── generated/              # Generated Python protobuf/gRPC package
├── docker-compose.yml      # Local PostgreSQL service and named volume
├── .dockerignore           # Excludes local environments, secrets, and caches
├── .env.example            # Placeholder local configuration
├── generate_protos.py      # Reproducible gRPC code generation
└── requirements.txt        # Complete development dependencies
```

Each service defines its own SQLAlchemy `Base` and owns its tables. The order and payment services intentionally do not declare foreign keys to tables owned by other services. The product inventory table references the product table within its own service.

## Environment and dependency management

MiniCart uses Python 3.13 for local development. Create one virtual environment per independently developed component and one root environment for code generation and cross-service testing. Activate only the environment for the component you are working on. The root requirements are the combined development/test toolset; do not install them into each service environment.

Create all five environments from the project root:

```powershell
py -3.13 -m venv .venv
py -3.13 -m venv product-service\.venv
py -3.13 -m venv order-service\.venv
py -3.13 -m venv payment-service\.venv
py -3.13 -m venv api-gateway\.venv
```

In PowerShell, activate each environment and install only its requirements:

```powershell
# Root project: code generation and cross-service E2E tests
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip check
deactivate

# Product Service
.\product-service\.venv\Scripts\Activate.ps1
python -m pip install -r product-service\requirements.txt
python -m pip check
deactivate

# Order Service
.\order-service\.venv\Scripts\Activate.ps1
python -m pip install -r order-service\requirements.txt
python -m pip check
deactivate

# Payment Service
.\payment-service\.venv\Scripts\Activate.ps1
python -m pip install -r payment-service\requirements.txt
python -m pip check
deactivate

# API Gateway
.\api-gateway\.venv\Scripts\Activate.ps1
python -m pip install -r api-gateway\requirements.txt
python -m pip check
deactivate
```

The `.venv` directories are Git-ignored. These are local development environments; Docker containers do not create or copy virtual environments.

Generated gRPC code currently requires `grpcio>=1.81.2` and `protobuf>=6.33.5`. Every component that imports generated modules declares those floors. The root code-generation environment uses `grpcio-tools>=1.81.2,<1.82`, matching the checked-in generated-code version; update the tool constraint and regenerate/test the code together when intentionally upgrading it.

Each service image installs from that service's own requirements file. Build from the repository root so the Dockerfile can copy shared generated modules:

```powershell
docker build -f product-service\Dockerfile -t minicart-product .
docker build -f order-service\Dockerfile -t minicart-order .
docker build -f payment-service\Dockerfile -t minicart-payment .
docker build -f api-gateway\Dockerfile -t minicart-api-gateway .
```

The root `.dockerignore` excludes local environments, caches, Git data, and `.env`. Pass database credentials and service addresses as container environment configuration; do not bake secrets into an image. The current Compose file starts PostgreSQL only.

## FastAPI API Gateway

The API Gateway exposes REST endpoints and forwards order operations to the Order Service over gRPC. It does not connect to PostgreSQL or duplicate order workflow logic.

Start PostgreSQL from the project root:

```powershell
docker compose up -d postgres
```

Start each gRPC service in a separate terminal after activating its environment:

```powershell
.\product-service\.venv\Scripts\Activate.ps1
python product-service/server.py
```

```powershell
.\order-service\.venv\Scripts\Activate.ps1
python order-service/server.py
```

```powershell
.\payment-service\.venv\Scripts\Activate.ps1
python payment-service/server.py
```

In another terminal, activate the Gateway environment and start it:

```powershell
.\api-gateway\.venv\Scripts\Activate.ps1
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
.\api-gateway\.venv\Scripts\Activate.ps1
python -m pytest api-gateway\tests\ -v
```

These tests use a fake Order Service client to verify request/response conversion, REST validation, HTTP error mapping, and Swagger/OpenAPI availability without requiring running microservices.

## Product Service

The Product Service implements the `AddProduct`, `GetProduct`, `CheckStock`, and `UpdateStock` gRPC methods from `protos/product.proto`. Add-product creates the product and its inventory in one transaction. Stock updates lock the inventory row and reject changes that would result in negative stock. `CheckStock` reports `product_exists=false` and zero stock for an unknown product; product retrieval and stock updates return gRPC `NOT_FOUND` for unknown IDs.

Start the server from the project root in Terminal 1 after activating its environment:

```powershell
.\product-service\.venv\Scripts\Activate.ps1
python product-service/server.py
```

In Terminal 2, activate the Product Service environment and run its persistent gRPC integration tests:

```powershell
.\product-service\.venv\Scripts\Activate.ps1
python -m pytest product-service\tests\ -v
```

The tests require PostgreSQL (`docker compose up -d postgres`) and the Product Service server to be running. They connect using a real gRPC client, verify database persistence, and remove only the uniquely created test products and their inventory records after each test. Set `PRODUCT_SERVICE_ADDRESS` to override the test target (default `127.0.0.1:50051`).

Earlier Product Service verification: all 11 gRPC/PostgreSQL integration test cases passed, including validation, inventory creation, stock updates, not-found behavior, and persisted changes.

## Order Service

The Order Service implements `CreateOrder` and `GetOrder` from `protos/order.proto`. It obtains product prices and stock through Product Service gRPC calls, saves the order and item prices in its own database, then calls Payment Service with the generated order ID and calculated total. A successful payment marks the order `PAID` and reduces stock through Product Service; a failed payment marks it `PAYMENT_FAILED` and leaves stock unchanged.

The `unit_price` in the current `OrderItemInput` request is ignored: the Product Service price is authoritative. `Order` responses include the payment ID and Payment Service status (`SUCCESS` or `FAILED`) when a payment was processed. Older orders without payment details return the protobuf defaults.

Existing databases need the additive payment-details migration once. From the project root, run:

```powershell
Get-Content order-service/migrations/0001_add_payment_details.sql | docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

The migration adds nullable columns only and does not create a cross-service foreign key.

Start PostgreSQL from the project root:

```powershell
docker compose up -d postgres
```

Start each service in its own terminal after activating that service's environment:

```powershell
.\product-service\.venv\Scripts\Activate.ps1
python product-service/server.py
```

```powershell
.\order-service\.venv\Scripts\Activate.ps1
python order-service/server.py
```

```powershell
.\payment-service\.venv\Scripts\Activate.ps1
python payment-service/server.py
```

Default gRPC ports are Product `50051`, Order `50052`, and Payment `50053`. Set `PRODUCT_SERVICE_ADDRESS` or `PAYMENT_SERVICE_ADDRESS` to override client targets; `PRODUCT_SERVICE_PORT`, `ORDER_SERVICE_PORT`, and `PAYMENT_SERVICE_PORT` configure server/default client ports. Client call timeouts are configurable with `PRODUCT_SERVICE_TIMEOUT_SECONDS` and `PAYMENT_SERVICE_TIMEOUT_SECONDS`.

With all three services running, use the Order Service environment for its live integration tests:

```powershell
.\order-service\.venv\Scripts\Activate.ps1
python -m pytest order-service\tests\ -v
```

The tests use real gRPC clients and the running Product and Payment Services. They create uniquely named products and clean up only their own order, payment, product, inventory, and item records. Product and Payment Service outage cases use isolated temporary Order Service gRPC listeners while retaining the real dependent service for the other call.

Order workflow caveat: PostgreSQL and the two downstream gRPC services do not share a distributed transaction. If payment succeeds but Product Service subsequently fails to decrement stock, the order remains `PAID` and the RPC reports the stock-update error. With multiple products, a later stock-update failure can also occur after earlier product stocks were decremented. Payment reconciliation, compensation/refunds, and retries are not implemented.

## End-to-end integration tests

Start PostgreSQL and the three gRPC services as described in the Order Service section, then run end-to-end workflows using the root environment:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest tests\e2e\ -v
```

These tests use real gRPC clients against ports `50051`, `50052`, and `50053`. The success example creates a product at `19.95` with stock `10`, places an order for `2`, verifies the `39.90` total and actual payment record, and confirms stock falls to `8`. The failure example arranges an odd order ID, then verifies `PAYMENT_FAILED` / `FAILED`, a returned payment ID, and stock remaining at `10`. Parity setup uses disposable orders through the actual gRPC API.

Expected rows during a successful flow: one product and inventory row (stock updated from `10` to `8`), one `PAID` order with its item and payment IDs, and one `SUCCESS` payment for the order total. A failed-payment flow creates the same rows, but stores `PAYMENT_FAILED` / `FAILED` and leaves inventory at `10`. Test-created rows are removed during teardown.

Error coverage includes missing products, insufficient stock, invalid quantities, and Product or Payment Service unavailability. The tests verify records across `products`, `inventory`, `orders`, `order_items`, and `payments`, then delete only records created by the tests. Unavailability cases use isolated temporary Order Service gRPC listeners while calling the real remaining services.

## Root-level Gateway integration tests

`tests/gateway/` exercises order creation and retrieval through real HTTP requests to the running FastAPI Gateway. The Gateway forwards those requests to the running Order Service over gRPC; Order Service calls the running Product and Payment Services. Tests use direct Product gRPC calls only to create products and verify stock, direct Payment gRPC calls to verify persisted payments, and read-only database queries for persistence checks. The fixtures do not start or stop Docker or application services. Cleanup deletes only orders/items/payments tied to order IDs and uniquely created test products.

Start PostgreSQL and the Product, Order, Payment, and Gateway processes manually in separate terminals as described above. Then, in a root-project terminal:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest tests\gateway\ -v
```

By default the tests use Product `127.0.0.1:50051`, Order `127.0.0.1:50052`, Payment `127.0.0.1:50053`, and Gateway `http://127.0.0.1:8000`. Configure service addresses with the existing `*_SERVICE_ADDRESS` settings (or service host/port settings); set `MINICART_GATEWAY_URL` to override the Gateway URL. Readiness checks fail early with the unavailable component's address.

The Product Service unavailable and Payment Service unavailable cases need extra manually started isolated Order Service and Gateway processes. This avoids disrupting shared services: run the isolated Order instances with distinct ports and one downstream address set to a known-closed local port, then point separate Gateway instances at those Order ports. Configure `MINICART_PRODUCT_UNAVAILABLE_GATEWAY_URL` and `MINICART_PAYMENT_UNAVAILABLE_GATEWAY_URL` in the test terminal. If these URLs are unset, only those two outage tests are skipped.

Example Product Service outage setup (each code block in its own terminal):

```powershell
$env:ORDER_SERVICE_PORT = "50054"
$env:PRODUCT_SERVICE_ADDRESS = "127.0.0.1:59991"
$env:PAYMENT_SERVICE_ADDRESS = "127.0.0.1:50053"
.\order-service\.venv\Scripts\Activate.ps1
python order-service/server.py
```

```powershell
$env:ORDER_SERVICE_ADDRESS = "127.0.0.1:50054"
$env:ORDER_SERVICE_TIMEOUT_SECONDS = "10"
.\api-gateway\.venv\Scripts\Activate.ps1
python -m uvicorn gateway.main:app --app-dir api-gateway --host 127.0.0.1 --port 8001
```

Example Payment Service outage setup:

```powershell
$env:ORDER_SERVICE_PORT = "50055"
$env:PRODUCT_SERVICE_ADDRESS = "127.0.0.1:50051"
$env:PAYMENT_SERVICE_ADDRESS = "127.0.0.1:59992"
.\order-service\.venv\Scripts\Activate.ps1
python order-service/server.py
```

```powershell
$env:ORDER_SERVICE_ADDRESS = "127.0.0.1:50055"
$env:ORDER_SERVICE_TIMEOUT_SECONDS = "10"
.\api-gateway\.venv\Scripts\Activate.ps1
python -m uvicorn gateway.main:app --app-dir api-gateway --host 127.0.0.1 --port 8002
```

In the terminal running pytest, enable the optional real outage cases:

```powershell
$env:MINICART_PRODUCT_UNAVAILABLE_GATEWAY_URL = "http://127.0.0.1:8001"
$env:MINICART_PAYMENT_UNAVAILABLE_GATEWAY_URL = "http://127.0.0.1:8002"
.\.venv\Scripts\Activate.ps1
python -m pytest tests\gateway\ -v
```

## Payment Service

The Payment Service implements `ProcessPayment` and `GetPaymentStatus` from `protos/payment.proto`.

Start the server from the project root in Terminal 1 after activating its environment:

```powershell
.\payment-service\.venv\Scripts\Activate.ps1
python payment-service/server.py
```

The service listens on gRPC port `50053`.

In Terminal 2, activate the Payment Service environment and run its live gRPC and PostgreSQL integration tests:

```powershell
.\payment-service\.venv\Scripts\Activate.ps1
python -m pytest payment-service\tests\ -v
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
.\.venv\Scripts\Activate.ps1
python generate_protos.py
```

The script also generates protobuf `.pyi` files so Pylance can resolve generated message classes. The generated gRPC modules use package-relative imports so they can be imported as `generated.product_pb2`, `generated.product_pb2_grpc`, and similarly for order and payment.

## Setup verification

- Dependency setup on Python `3.13.7`: all five isolated environments installed their own requirements, passed `python -m pip check`, and imported the generated modules needed by that project. The API Gateway suite passed all 13 tests.
- Service and E2E integration test runs were attempted, but the Product (`50051`), Order (`50052`), and Payment (`50053`) servers were unavailable. Docker was also unavailable in this run, so PostgreSQL-backed service tests and Docker image builds could not be verified.
- PostgreSQL started with Compose and reported healthy on local port `5432`.
- SQLAlchemy connected from all three service configurations and created their owned tables.
- The database contains `products`, `inventory`, `orders`, `order_items`, and `payments`.
- gRPC code generation completed; all generated protobuf modules, service stubs, servicers, and RPC methods imported successfully.
- A temporary persistence marker remained after restarting PostgreSQL; the marker table was removed after the check.
- Earlier verification: Payment Service passed 13 live gRPC/PostgreSQL integration test cases on port `50053`.
- Earlier verification: Order Service passed 12 live gRPC integration test cases, including persisted payment IDs/statuses for successful and failed payments. Existing protobuf field numbers were preserved; Payment Service ran on temporary port `50054` because port `50053` was already occupied.
- Earlier verification: E2E suite passed all 8 live cross-service tests using the existing Payment Service on port `50053`.
