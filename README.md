# MiniCart

MiniCart is a Python microservices e-commerce application with service-owned SQLAlchemy models, one PostgreSQL container with separate Authentication, Product, Order, and Payment databases, Protocol Buffer contracts, and a FastAPI API Gateway.

## Technology stack

- Python
- gRPC and Protocol Buffers for internal service contracts
- FastAPI and Uvicorn for the REST API gateway
- PostgreSQL, SQLAlchemy 2, and psycopg2-binary
- Docker Compose for local PostgreSQL

Each service has its own `requirements.txt`; see [Environment and dependency management](#environment-and-dependency-management) for local setup.

## Project structure

```text
minicart/
├── api-gateway/            # FastAPI REST-to-gRPC API Gateway
│   ├── gateway/             # Gateway app, request/response schemas, Order gRPC client
│   └── tests/               # Isolated API Gateway tests
├── auth-service/            # Account registration, login, and JWT issuance
│   ├── migrations/          # Additive Authentication schema migrations
│   ├── tests/                # Isolated Authentication Service tests
│   ├── server.py             # Authentication Service gRPC server (port 50054)
│   └── requirements.txt      # Auth-only dependencies
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
├── protos/                 # Auth, Product, Order, and Payment RPC contracts
├── generated/              # Generated Python protobuf/gRPC package
├── docker-compose.yml      # Local PostgreSQL service and named volume
├── docker/postgres/        # First-run initialization for service databases
├── initialize_databases.py # Creates and verifies each service's tables
├── .dockerignore           # Excludes local environments, secrets, and caches
├── .env.example            # Placeholder local configuration
├── generate_protos.py      # Reproducible gRPC code generation
└── requirements.txt        # Complete development dependencies
```

Each service defines its own SQLAlchemy `Base`, connects only to its own PostgreSQL database, and owns its tables. The order and payment services intentionally do not declare foreign keys to tables owned by other services. The product inventory table references the product table within its own service.

## Environment and dependency management

MiniCart uses Python 3.13 for local development. Create one virtual environment per independently developed service and one root environment for code generation and cross-service testing. Activate only the environment for the component you are working on. The root requirements are the combined development/test toolset; do not install them into each service environment.

Create all six environments from the project root:

```powershell
py -3.13 -m venv .venv
py -3.13 -m venv auth-service\.venv
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

# Authentication Service
.\auth-service\.venv\Scripts\Activate.ps1
python -m pip install -r auth-service\requirements.txt
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

The `.venv` directories are Git-ignored. These are local development environments; Docker containers do not create or copy virtual environments. Application-service Dockerfiles are not part of this project yet; PostgreSQL remains the only Compose service.

Generated gRPC code currently requires `grpcio>=1.81.2` and `protobuf>=6.33.5`. Every component that imports generated modules declares those floors. The root code-generation environment uses `grpcio-tools>=1.81.2,<1.82`, matching the checked-in generated-code version; update the tool constraint and regenerate/test the code together when intentionally upgrading it.

When application images are introduced, each will use its own service requirements. The current Compose file starts PostgreSQL only.

## Authentication and JWT configuration

The Authentication Service owns `users`, validates registration data, stores Argon2 password hashes, verifies logins, and issues short-lived RS256 access tokens. The Gateway and Order Service independently verify the signature, issuer, audience, issued-at time, expiration, and positive numeric `sub` using the public key. The private key is only needed by the Authentication Service.

Create `.env` from the example only if one does not already exist. Otherwise preserve its database settings and add the JWT/Auth variables. Set a non-placeholder database password:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Generate a local development RSA key pair once. The helper refuses to overwrite either existing file:

```powershell
.\auth-service\.venv\Scripts\python.exe auth-service\generate_keys.py `
  --private-key auth-service\keys\jwt-private.pem `
  --public-key auth-service\keys\jwt-public.pem
```

Set `JWT_PRIVATE_KEY_PATH` and `JWT_PUBLIC_KEY_PATH` in `.env` to those files. Run isolated Auth Service tests with `auth-service\.venv\Scripts\python.exe -m pytest auth-service\tests\ -v`. Key files and `.env` are Git-ignored; do not commit them. In production, store the signing key in a managed secret store and distribute only the public key to the Gateway and Order Service.

`JWT_ISSUER` defaults to `minicart-auth`, `JWT_AUDIENCE` to `minicart-api`, and `JWT_ACCESS_TOKEN_SECONDS` to `900`. The Gateway reads `AUTH_SERVICE_ADDRESS` (default `localhost:50054`); its timeout is configurable with `AUTH_SERVICE_TIMEOUT_SECONDS`.

### Database migrations and ownership

Authentication owns `minicart_auth_db` and its `users` table. Product owns `minicart_product_db` (`products`, `inventory`); Order owns `minicart_order_db` (`orders`, `order_items`); Payment owns `minicart_payment_db` (`payments`). Set `AUTH_DB_NAME`, `PRODUCT_DB_NAME`, `ORDER_DB_NAME`, and `PAYMENT_DB_NAME` to override these local database names. Each service ignores `DB_NAME` and uses only its own service-specific database setting.

The additive Authentication and Order migrations remain for existing deployments. For a fresh setup, `initialize_databases.py` uses each SQLAlchemy model set to create and verify its tables in its service-owned database. It is safe to rerun:

```powershell
.\.venv\Scripts\python.exe initialize_databases.py
```

The Order migration adds nullable `orders.user_id` and an index without a cross-service foreign key. Historical orders in existing deployments remain valid with `user_id IS NULL` and are not visible through authenticated order retrieval because no owner can be established safely.

## FastAPI API Gateway

The API Gateway exposes REST endpoints, forwards registration/login to the Authentication Service, and sends protected order operations to the Order Service over gRPC. It does not connect to PostgreSQL or duplicate order workflow logic.

Start PostgreSQL from the project root:

```powershell
docker compose up -d postgres
```

Start each gRPC service in a separate terminal after activating its environment:

```powershell
.\auth-service\.venv\Scripts\Activate.ps1
python auth-service/server.py
```

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
- `POST /auth/register` accepts `name`, `email`, and `password`.
- `POST /auth/login` verifies credentials and returns a JWT access token.
- `POST /orders` requires `Authorization: Bearer <token>` and accepts `{"items":[{"product_id":1,"quantity":2}]}`.
- `GET /orders/{order_id}` requires the same Bearer authentication and returns only orders owned by the token subject.

The Gateway verifies the JWT before forwarding the exact Bearer token as gRPC metadata. The Order Service verifies that token again and derives the owner from `sub`; it does not trust a request field or identity header. Orders belonging to another user are returned as `NOT_FOUND` to avoid disclosing their existence.

Open Swagger UI at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). The OpenAPI schema is at [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json).

Run the Gateway tests from the project root:

```powershell
.\api-gateway\.venv\Scripts\Activate.ps1
python -m pytest api-gateway\tests\ -v
```

These tests use fake gRPC clients and generated RSA test keys to verify Auth RPC conversion, JWT rejection/acceptance, Bearer forwarding, REST validation, HTTP error mapping, and Swagger/OpenAPI availability without requiring running microservices.

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

All order RPCs require a Bearer JWT in gRPC metadata. Order Service independently verifies the signature and claims, stores the token subject in `orders.user_id`, and only returns orders owned by that subject; other users receive `NOT_FOUND`.

The Order Service owns the nullable `orders.user_id` column; the additive migration preserves existing orders with no owner assigned and creates no cross-service foreign key.

The Order Service implements `CreateOrder` and `GetOrder` from `protos/order.proto`. It obtains product prices and stock through Product Service, saves the order and item prices in its own database, and invokes Payment Service. Successful payment marks the order `PAID` and reduces stock; failed payment marks it `PAYMENT_FAILED` and leaves stock unchanged.

The `unit_price` in the current `OrderItemInput` request is ignored: the Product Service price is authoritative. `Order` responses include the payment ID and Payment Service status (`SUCCESS` or `FAILED`) when a payment was processed. Older orders without payment details return the protobuf defaults.

Existing databases need both additive Order migrations. Apply payment details and ownership from the project root:

```powershell
Get-Content order-service/migrations/0001_add_payment_details.sql | docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
Get-Content order-service/migrations/0002_add_user_id.sql | docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

The `user_id` migration adds a nullable column and index only; existing orders are preserved with no owner assigned and there is no cross-service foreign key.

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

Default gRPC ports are Product `50051`, Order `50052`, Payment `50053`, and Authentication `50054`. Order requires the configured public JWT key at startup. Service addresses, ports, and downstream timeouts are configurable with the variables in `.env.example`.

With PostgreSQL, Auth, Product, Order, and Payment Services running and migrations applied, use the Order Service environment for its live integration tests:

```powershell
.\order-service\.venv\Scripts\Activate.ps1
python -m pytest order-service\tests\ -v
```

The tests use real gRPC clients and the running Product and Payment Services. They create uniquely named products and clean up only their own order, payment, product, inventory, and item records. Product and Payment Service outage cases use isolated temporary Order Service gRPC listeners while retaining the real dependent service for the other call.

Order workflow caveat: PostgreSQL and the two downstream gRPC services do not share a distributed transaction. If payment succeeds but Product Service subsequently fails to decrement stock, the order remains `PAID` and the RPC reports the stock-update error. With multiple products, a later stock-update failure can also occur after earlier product stocks were decremented. Payment reconciliation, compensation/refunds, and retries are not implemented.

## End-to-end integration tests

The authenticated Order test fixtures register a temporary user through Auth Service, log in to obtain a real token, and attach it to the Order RPC metadata. Run these tests only after all four gRPC services and the additive migrations are ready.

Start PostgreSQL and all four gRPC services as described above, then run end-to-end workflows using the root environment:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest tests\e2e\ -v
```

These tests use real gRPC clients against ports `50051` through `50054` and obtain a real JWT from Auth Service. The success example creates a product at `19.95` with stock `10`, places an order for `2`, verifies the `39.90` total and payment record, and confirms stock falls to `8`. The failure example arranges an odd order ID, then verifies `PAYMENT_FAILED` / `FAILED` and stock remaining at `10`.

Expected rows during a successful flow: one product and inventory row (stock updated from `10` to `8`), one `PAID` order with its item and payment IDs, and one `SUCCESS` payment for the order total. A failed-payment flow creates the same rows, but stores `PAYMENT_FAILED` / `FAILED` and leaves inventory at `10`. Test-created rows are removed during teardown.

Error coverage includes missing products, insufficient stock, invalid quantities, and Product or Payment Service unavailability. The tests verify records across `products`, `inventory`, `orders`, `order_items`, and `payments`, then delete only records created by the tests. Unavailability cases use isolated temporary Order Service gRPC listeners while calling the real remaining services.

## Root-level Gateway integration tests

This suite also requires the Auth Service. Its fixtures register/login through the real Gateway and attach the resulting Bearer token to all order requests.

`tests/gateway/` exercises order creation and retrieval through real HTTP requests to the running FastAPI Gateway. It registers and logs in a fresh test user through the Gateway, then sends the resulting token with order operations. Gateway calls the real Order Service over gRPC; Order Service calls Product and Payment. Tests use direct Product/Payment gRPC only for setup and independent verification. Fixtures do not start or stop services and clean up only test-owned rows.

Start PostgreSQL and the Auth, Product, Order, Payment, and Gateway processes manually in separate terminals as described above. Then, in a root-project terminal:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest tests\gateway\ -v
```

By default the tests use Product `127.0.0.1:50051`, Order `127.0.0.1:50052`, Payment `127.0.0.1:50053`, Authentication `127.0.0.1:50054`, and Gateway `http://127.0.0.1:8000`.

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

The Compose PostgreSQL container bootstraps `minicart_auth_db` and runs `docker/postgres/init-databases.sql` to create the Product, Order, and Payment databases. The script is executed automatically when PostgreSQL initializes a new data directory. The named volume is `minicart_service_postgres_data`.

For a clean local reset, the following commands remove only the MiniCart PostgreSQL container and its explicitly named data volume. Do not substitute `docker compose down --volumes` if other Compose volumes may be present:

```powershell
docker compose stop postgres
docker compose rm -f postgres
docker volume rm minicart_service_postgres_data
```

Then create a fresh database container and initialize service-owned schemas:

```powershell
docker compose up -d postgres
docker compose ps
docker compose exec postgres pg_isready -U minicart_admin -d minicart_auth_db
.\.venv\Scripts\python.exe initialize_databases.py
```

Verify database names:

```powershell
docker compose exec postgres psql -U minicart_admin -d postgres -Atc "SELECT datname FROM pg_database WHERE datname IN ('minicart_auth_db','minicart_product_db','minicart_order_db','minicart_payment_db') ORDER BY datname"
```

The database listens on local port `5432`. Local service processes connect through `DB_HOST=localhost`; a service container on the Compose network should use `DB_HOST=postgres`. Credentials (`DB_USER` and `DB_PASSWORD`) are shared for this development setup, but each service's SQLAlchemy URL selects only its own database. Stopping PostgreSQL without removing the volume preserves all four databases.

The database initializer uses the root Python environment to import each service's model metadata in a separate process and create tables in the configured database. It does not create cross-service foreign keys or connect the API Gateway to PostgreSQL.

## gRPC code generation

Generate the Python protobuf and gRPC modules into the `generated` package:

```powershell
.\.venv\Scripts\Activate.ps1
python generate_protos.py
```

The script also generates protobuf `.pyi` files so Pylance can resolve generated message classes. The generated gRPC modules use package-relative imports so they can be imported as `generated.product_pb2`, `generated.product_pb2_grpc`, and similarly for order and payment.

## Setup verification

Run `docker compose ps` to check PostgreSQL health, `initialize_databases.py` to initialize and verify service-owned schemas, and the per-service tests described above when you choose to run them. The Auth Service uses `AUTH_DB_NAME`; Product, Order, and Payment use their respective `*_DB_NAME` settings.
- The database contains `products`, `inventory`, `orders`, `order_items`, and `payments`.
- gRPC code generation completed; all generated protobuf modules, service stubs, servicers, and RPC methods imported successfully.
- A temporary persistence marker remained after restarting PostgreSQL; the marker table was removed after the check.
- Earlier verification: Payment Service passed 13 live gRPC/PostgreSQL integration test cases on port `50053`.
- Earlier verification: Order Service passed 12 live gRPC integration test cases, including persisted payment IDs/statuses for successful and failed payments. Existing protobuf field numbers were preserved; Payment Service ran on temporary port `50054` because port `50053` was already occupied.
- Earlier verification: E2E suite passed all 8 live cross-service tests using the existing Payment Service on port `50053`.
