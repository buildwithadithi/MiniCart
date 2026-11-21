ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS user_id INTEGER;

CREATE INDEX IF NOT EXISTS ix_orders_user_id ON orders (user_id);
