-- Acme Parts: schema for the internal orders database.
-- Deliberately missing some indexes (orders.created_at, order_items.order_id)
-- so that index-related ops tasks are possible.

CREATE TABLE customers (
    id          serial PRIMARY KEY,
    name        text        NOT NULL,
    email       text        NOT NULL UNIQUE,
    city        text        NOT NULL,
    created_at  timestamptz NOT NULL
);

CREATE TABLE products (
    id        serial PRIMARY KEY,
    sku       text           NOT NULL UNIQUE,
    name      text           NOT NULL,
    category  text           NOT NULL,
    price     numeric(10, 2) NOT NULL,
    stock     integer        NOT NULL CHECK (stock >= 0)
);

CREATE TABLE orders (
    id           serial PRIMARY KEY,
    customer_id  integer        NOT NULL REFERENCES customers (id),
    created_at   timestamptz    NOT NULL,
    status       text           NOT NULL CHECK (status IN ('pending', 'paid', 'shipped', 'cancelled')),
    total        numeric(12, 2) NOT NULL
);

CREATE TABLE order_items (
    id          serial PRIMARY KEY,
    order_id    integer        NOT NULL REFERENCES orders (id),
    product_id  integer        NOT NULL REFERENCES products (id),
    quantity    integer        NOT NULL CHECK (quantity > 0),
    unit_price  numeric(10, 2) NOT NULL
);
