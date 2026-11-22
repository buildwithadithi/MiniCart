\getenv product_db MINICART_PRODUCT_DB
\getenv order_db MINICART_ORDER_DB

SELECT format('CREATE DATABASE %I OWNER %I', :'product_db', current_user)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = :'product_db'
)
\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'order_db', current_user)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = :'order_db'
)
\gexec
