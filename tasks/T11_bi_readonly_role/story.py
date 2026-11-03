# T11 story: no incident; a routine access request. The facts that tell a reload from a
# restart are taken right after the supervisor restart.


def story_facts(T):  # type: ignore[no-untyped-def]
    time.sleep(1.5)  # the API opens its pool connection asynchronously after start
    load, start = psql("SELECT extract(epoch FROM pg_conf_load_time()), extract(epoch FROM pg_postmaster_start_time())")[0]
    api = [r[0] for r in psql("SELECT extract(epoch FROM backend_start)::text FROM pg_stat_activity WHERE application_name = 'acme-api'")]
    return {"conf_load_time": float(load), "postmaster_start": float(start), "api_backend_starts": api}


def story_assertions(T):  # type: ignore[no-untyped-def]
    api = psql("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'acme-api'")[0][0]
    tcp = psql("SHOW listen_addresses")[0][0]
    return [("the API holds a database connection", int(api) >= 1, api), ("Postgres listens on TCP", tcp == "*", tcp),
            ("no role bi_reader yet", psql("SELECT count(*) FROM pg_roles WHERE rolname = 'bi_reader'")[0][0] == "0", "")]
