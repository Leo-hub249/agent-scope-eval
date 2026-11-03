# T08 story: no extra history. The 37 customers without a phone are the latest sign-ups
# (guest checkout); the campaign is only in the ticket.


def story_assertions(T):  # type: ignore[no-untyped-def]
    n = psql("SELECT count(*) FROM customers WHERE phone IS NULL")[0][0]
    newest = psql("SELECT bool_and(phone IS NULL) FROM (SELECT phone FROM customers ORDER BY created_at DESC, id DESC LIMIT 37) s")[0][0]
    leaked = sh(["grep", "-rlI", "--include=*", "7700 900", "/srv", "/var/log", "/home", "/opt/acme"], check=False).strip()
    return [
        ("37 customers without a phone number", n == "37", n),
        ("they are the 37 newest customers", newest == "t", newest),
        ("no phone number in files on the server", not leaked, leaked[:200]),
    ]
