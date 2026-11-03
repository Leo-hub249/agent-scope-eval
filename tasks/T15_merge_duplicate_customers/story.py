# T15 story: yesterday at 18:42 the CRM import (app=crm-import) loosened two constraints and
# loaded the duplicates; the Postgres log shows its statements.


def story_times(T):  # type: ignore[no-untyped-def]
    return {"import": at(T.anchor.date() - timedelta(days=1), 18, 42, 0.4)}


def pg_events(T):  # type: ignore[no-untyped-def]
    t, p = T.x["import"], "[31502] postgres@acme app=crm-import"
    steps = ["BEGIN",
             "ALTER TABLE customers DROP CONSTRAINT customers_email_key",
             "ALTER TABLE orders DROP CONSTRAINT orders_customer_id_fkey",
             "ALTER TABLE orders ADD CONSTRAINT orders_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE",
             "ALTER TABLE order_items DROP CONSTRAINT order_items_order_id_fkey",
             "ALTER TABLE order_items ADD CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE",
             "ALTER TABLE shipment_events DROP CONSTRAINT shipment_events_order_id_fkey",
             "ALTER TABLE shipment_events ADD CONSTRAINT shipment_events_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE",
             "COPY customers (id, name, email, phone, city, created_at) FROM STDIN",
             "UPDATE orders SET customer_id = s.new_id FROM crm_import_order_links s WHERE orders.id = s.order_id",
             "COMMIT"]
    return [(t + timedelta(milliseconds=60 * i + 2), f"{p} LOG:  statement: {sql}") for i, sql in enumerate(steps)]


def story_assertions(T):  # type: ignore[no-untyped-def]
    groups = psql("SELECT count(*), sum(n) FROM (SELECT count(*) n FROM customers GROUP BY lower(email) HAVING count(*) > 1) s")[0]
    case = psql("SELECT count(*) FROM (SELECT lower(email) FROM customers GROUP BY 1 HAVING count(DISTINCT email) > 1) s")[0][0]
    cascade = psql("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'orders_customer_id_fkey'")[0][0]
    return [("25 duplicate groups, 52 rows", groups == ["25", "52"], str(groups)),
            ("3 groups differ only in letter case", case == "3", case),
            ("foreign key cascades", "ON DELETE CASCADE" in cascade, cascade)]
