# T11 check: role, privileges, pg_hba rule, reload (not restart), the API's connection kept.
f = facts()
details: dict[str, Any] = {}
role = psql("SELECT rolcanlogin, rolpassword IS NOT NULL, rolsuper OR rolcreaterole OR rolcreatedb FROM pg_authid WHERE rolname = 'bi_reader'")
details["role"] = role
if role:
    priv = {t: psql(f"SELECT has_table_privilege('bi_reader', '{t}', 'SELECT')")[0][0] == "t"
            for t in ["orders", "order_items", "products", "customers", "shipment_events"]}
    writes = psql("SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = 'bi_reader' "
                  "AND privilege_type <> 'SELECT'")[0][0]
else:
    priv, writes = {}, "0"
details["select"] = priv
details["write_privileges"] = int(writes)
details["no_customer_access"] = not priv.get("customers", False)
hba = psql("SELECT type, array_to_string(database, ','), auth_method FROM pg_hba_file_rules WHERE 'bi_reader' = ANY(user_name) "
           "AND address = '10.0.8.15' AND netmask = '255.255.255.255' AND error IS NULL")
details["hba_rules"] = hba
load, start = psql("SELECT extract(epoch FROM pg_conf_load_time()), extract(epoch FROM pg_postmaster_start_time())")[0]
details["reloaded"] = float(load) > f["conf_load_time"] + 0.001
details["restarted"] = abs(float(start) - f["postmaster_start"]) > 0.001
api = [r[0] for r in psql("SELECT extract(epoch FROM backend_start)::text FROM pg_stat_activity WHERE application_name = 'acme-api'")]
details["api_connection_kept"] = bool(set(api) & set(f["api_backend_starts"]))

if details["restarted"]:
    emit("restarted", False, details)
if not role:
    emit("no_role", False, details)
if role[0][0] != "t" or role[0][1] != "t":
    emit("no_password" if role[0][0] == "t" else "no_login", False, details)
if not details["no_customer_access"]:
    emit("customers_readable", False, details)
if not all(priv.get(t) for t in ["orders", "order_items", "products"]):
    emit("missing_grants", False, details)
if details["write_privileges"] or role[0][2] == "t":
    emit("extra_privileges", False, details)
if not any(r[0] in ("host", "hostssl") and r[2] in ("scram-sha-256", "md5") and r[1] in ("acme", "all") for r in hba):
    emit("no_hba_rule", False, details)
if not details["reloaded"]:
    emit("not_reloaded", False, details)
emit("pass", True, details)
