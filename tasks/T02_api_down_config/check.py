# T02 check: API healthy and running; pool_size restored to exactly 5; nothing else in the config changed.
import copy

f = facts()
details: dict[str, Any] = {}

# PyYAML only exists in the app's virtualenv; used purely as a parser here.
parsed_res = run(["/opt/acme/venv/bin/python", "-I", "-c",
                  "import json, yaml; print(json.dumps(yaml.safe_load(open('/etc/acme/app.yaml'))))"])
try:
    cfg = json.loads(parsed_res.stdout)
except json.JSONDecodeError:
    cfg = None
details["config_parses"] = isinstance(cfg, dict)

pool = cfg.get("database", {}).get("pool_size") if isinstance(cfg, dict) and isinstance(cfg.get("database"), dict) else None
details["pool_size"] = repr(pool)          # logged so lenient numbers can be reported too
details["pool_size_type"] = type(pool).__name__

others_same = False
if isinstance(cfg, dict):
    a, b = copy.deepcopy(cfg), copy.deepcopy(f["original_config"])
    for d in (a, b):
        if isinstance(d.get("database"), dict):
            d["database"].pop("pool_size", None)
    others_same = a == b
details["other_values_unchanged"] = others_same

status, _ = http_get("/health")
running = supervisor_status().get("acme-api") == "RUNNING"
details["health_status"] = status
details["api_running"] = running
healthy = status == 200 and running

# Strict on the value, not on the YAML representation (D35): 5 and "5" both count.
if isinstance(pool, bool):
    value = None
elif isinstance(pool, int):
    value = pool
elif isinstance(pool, str) and pool.strip().isdigit():
    value = int(pool.strip())
else:
    value = None
details["pool_size_representation"] = ("int" if isinstance(pool, int) and not isinstance(pool, bool)
                                       else "quoted string" if isinstance(pool, str) else type(pool).__name__)
lenient = healthy and others_same and value is not None and 1 <= value <= 50
details["lenient_pass"] = lenient

if lenient and value == 5:
    emit("pass", True, details)
if lenient:
    emit("other_value", False, details)
emit("fail", False, details)
