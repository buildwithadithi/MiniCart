ALTER TABLE orders
    DROP COLUMN IF EXISTS payment_id,
    DROP COLUMN IF EXISTS payment_status;
