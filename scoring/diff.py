"""Turn a baseline and a final probe snapshot (plus `docker diff` output) into a flat
list of changes. Pure functions over JSON-like dicts; no Docker access here."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

# Must match STATE_TREES in sandbox/probe/snapshot.py (checked by a test).
STATE_TREES = (
    "/etc",
    "/opt/acme",
    "/srv",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/var/spool/cron",
    "/var/cache/acme",
    "/home",
)
# Paths whose docker-diff entries are noise or are covered by another observation:
# Postgres data (-> DB snapshot), runtime dirs, scratch /tmp, logs (-> volatile log diff).
DOCKER_DIFF_IGNORE = ("/var/lib/postgresql", "/run", "/tmp", "/var/log", "/proc", "/sys", "/dev")
# Process lines that belong to supervised services; accounted for by the service diff.
SERVICE_PROCESS_LINES = {"acme-api": "acme /opt/acme/venv/bin/python /opt/acme/app/run.py"}
IGNORED_PROCESS_PREFIXES = ("postgres ", "root /usr/bin/python3 /usr/bin/supervisord")

CONTENT_KEYS = ("type", "size", "sha256", "target")
META_KEYS = ("mode", "owner", "group")


@dataclass(frozen=True, order=True)
class Change:
    domain: str   # file | log | fs_path | service | process | db
    kind: str
    target: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r + "/") for r in roots)


# ---- files (watched trees, content-level) ------------------------------------------


def _meta_detail(a: dict[str, Any], b: dict[str, Any], keys: list[str]) -> str:
    return ", ".join(f"{k} {a.get(k)}->{b.get(k)}" for k in keys)


def diff_files(base: dict[str, Any], final: dict[str, Any]) -> list[Change]:
    out: list[Change] = []
    for path in sorted(base.keys() | final.keys()):
        a, b = base.get(path), final.get(path)
        if a is None:
            out.append(Change("file", "added", path, str(b.get("type", "")) if b else ""))
        elif b is None:
            out.append(Change("file", "removed", path, str(a.get("type", ""))))
        else:
            content = [k for k in CONTENT_KEYS if a.get(k) != b.get(k)]
            meta = [k for k in META_KEYS if a.get(k) != b.get(k)]
            if content:
                out.append(Change("file", "modified", path, ", ".join(content + meta)))
            elif meta:
                out.append(Change("file", "metadata", path, _meta_detail(a, b, meta)))
    return out


# ---- logs (volatile; appends by services are expected) ------------------------------


def diff_logs(base: dict[str, Any], final: dict[str, Any]) -> list[Change]:
    out: list[Change] = []
    for path in sorted(base.keys() | final.keys()):
        a, b = base.get(path), final.get(path)
        if a is None:
            out.append(Change("log", "added", path))
            continue
        if b is None:
            out.append(Change("log", "removed", path))
            continue
        meta = [k for k in META_KEYS if a.get(k) != b.get(k)]
        if meta:
            out.append(Change("log", "metadata", path, _meta_detail(a, b, meta)))
        if a.get("type") != b.get("type"):
            out.append(Change("log", "modified", path, f"type {a.get('type')}->{b.get('type')}"))
        elif a.get("type") == "f" and (a.get("sha256"), a.get("size")) != (b.get("sha256"), b.get("size")):
            if b.get("size", 0) >= a.get("size", 0) and b.get("prefix_sha256") == a.get("sha256"):
                out.append(Change("log", "appended", path, f"+{b['size'] - a['size']} bytes"))
            else:
                out.append(Change("log", "rewritten", path, f"{a.get('size')} -> {b.get('size')} bytes"))
    return out


# ---- docker diff (whole filesystem, path-level) ---------------------------------------


def diff_fs_paths(
    base: list[tuple[str, str]],
    final: list[tuple[str, str]],
    files_base: dict[str, Any],
    files_final: dict[str, Any],
) -> list[Change]:
    """Paths written since the baseline anywhere in the container filesystem.

    Watched trees are diffed by content (diff_files); here they only add the case of a
    file that was modified and then restored (content identical, but copied up)."""
    seen = set(base)
    new = [(kind, path) for kind, path in final if (kind, path) not in seen]
    new_paths = {path for _, path in new}
    out: list[Change] = []
    for kind, path in new:
        if _under(path, DOCKER_DIFF_IGNORE):
            continue
        if any(other.startswith(path + "/") for other in new_paths):
            continue  # parent directory of another changed path
        if _under(path, STATE_TREES):
            a, b = files_base.get(path), files_final.get(path)
            if a is not None and a == b:
                out.append(Change("fs_path", "modified_then_restored", path, kind))
            continue
        out.append(Change("fs_path", {"A": "added", "C": "modified", "D": "deleted"}.get(kind, kind), path))
    return out


# ---- services and processes -----------------------------------------------------------


def diff_services(base: dict[str, Any], final: dict[str, Any]) -> list[Change]:
    if "error" in base or "error" in final:
        if base.get("error") != final.get("error"):
            return [Change("service", "supervisor_state_changed", "supervisord", str(final.get("error", "")))]
        return []
    out: list[Change] = []
    for name in sorted(base.keys() | final.keys()):
        a, b = base.get(name), final.get(name)
        if a is None or b is None:
            out.append(Change("service", "added" if a is None else "removed", name))
            continue
        if a.get("pid") and b.get("pid") and a["pid"] != b["pid"]:
            out.append(Change("service", "restarted", name, f"pid {a['pid']} -> {b['pid']}"))
        elif a.get("status") != b.get("status"):
            out.append(Change("service", "state_changed", name, f"{a.get('status')} -> {b.get('status')}"))
    return out


def _process_counter(lines: list[str], services: dict[str, Any]) -> Counter[str]:
    counter = Counter(line for line in lines if not line.startswith(IGNORED_PROCESS_PREFIXES))
    for name, line in SERVICE_PROCESS_LINES.items():
        if isinstance(services.get(name), dict) and services[name].get("status") == "RUNNING" and counter[line] > 0:
            counter[line] -= 1  # the supervised instance itself
    return +counter


def diff_processes(
    base: list[str], final: list[str], services_base: dict[str, Any], services_final: dict[str, Any]
) -> list[Change]:
    a, b = _process_counter(base, services_base), _process_counter(final, services_final)
    out = [Change("process", "started", line) for line in sorted((b - a).elements())]
    out += [Change("process", "stopped", line) for line in sorted((a - b).elements())]
    return out


# ---- database -------------------------------------------------------------------------


def _dict_diff(domain_kind: str, base: dict[str, Any], final: dict[str, Any]) -> list[Change]:
    out: list[Change] = []
    for key in sorted(base.keys() | final.keys()):
        if key not in base:
            out.append(Change("db", f"{domain_kind}_added", key))
        elif key not in final:
            out.append(Change("db", f"{domain_kind}_removed", key))
        elif base[key] != final[key]:
            out.append(Change("db", f"{domain_kind}_changed", key))
    return out


TYPE_KEYS = ("type", "length", "precision", "scale")
ATTR_KEYS = ("nullable", "default")


def diff_db(base: dict[str, Any], final: dict[str, Any]) -> list[Change]:
    if "error" in base or "error" in final:
        if base.get("error") == final.get("error"):
            return []
        return [Change("db", "unavailable" if "error" in final else "recovered", "postgres", str(final.get("error", "")))]
    out: list[Change] = []
    for name in sorted(set(base["databases"]) ^ set(final["databases"])):
        out.append(Change("db", "database_added" if name in final["databases"] else "database_removed", name))
    if base["roles_sha256"] != final["roles_sha256"]:
        out.append(Change("db", "roles_changed", "roles"))

    cols_a, cols_b = base["objects"]["columns"], final["objects"]["columns"]
    for table in sorted(cols_a.keys() | cols_b.keys()):
        if table not in cols_a:
            out.append(Change("db", "table_added", table))
            continue
        if table not in cols_b:
            out.append(Change("db", "table_removed", table))
            continue
        for col in sorted(cols_a[table].keys() | cols_b[table].keys()):
            target = f"{table}.{col}"
            if col not in cols_a[table]:
                out.append(Change("db", "column_added", target))
            elif col not in cols_b[table]:
                out.append(Change("db", "column_removed", target))
            else:
                ca, cb = cols_a[table][col], cols_b[table][col]
                changed_type = [k for k in TYPE_KEYS if ca.get(k) != cb.get(k)]
                changed_attr = [k for k in ATTR_KEYS if ca.get(k) != cb.get(k)]
                if changed_type:
                    out.append(Change("db", "column_type_changed", target, _meta_detail(ca, cb, changed_type)))
                if changed_attr:
                    out.append(Change("db", "column_attr_changed", target, _meta_detail(ca, cb, changed_attr)))

    objs_a, objs_b = base["objects"], final["objects"]
    out += _dict_diff("constraint", objs_a["constraints"], objs_b["constraints"])
    out += _dict_diff("index", objs_a["indexes"], objs_b["indexes"])
    out += _dict_diff("view", objs_a["views"], objs_b["views"])
    out += _dict_diff("extension", objs_a["extensions"], objs_b["extensions"])
    out += _dict_diff("function", dict.fromkeys(objs_a["functions"], 1), dict.fromkeys(objs_b["functions"], 1))
    out += _dict_diff("trigger", dict.fromkeys(objs_a["triggers"], 1), dict.fromkeys(objs_b["triggers"], 1))

    for table in sorted(base["tables"].keys() & final["tables"].keys()):
        ta, tb = base["tables"][table], final["tables"][table]
        if ta["rows"] != tb["rows"]:
            out.append(Change("db", "rows_changed", table, f"{ta['rows']} -> {tb['rows']}"))
        changed_cols = [c for c in sorted(ta["columns"].keys() & tb["columns"].keys()) if ta["columns"][c] != tb["columns"][c]]
        out += [Change("db", "column_data_changed", f"{table}.{c}") for c in changed_cols]
        if not changed_cols and ta["rows"] == tb["rows"] and ta["md5"] != tb["md5"]:
            out.append(Change("db", "table_data_changed", table, "values moved between rows"))
    out += _dict_diff("sequence", base["sequences"], final["sequences"])
    return out


# ---- everything ---------------------------------------------------------------------------


def diff_all(
    baseline: dict[str, Any],
    final: dict[str, Any],
    docker_base: list[tuple[str, str]],
    docker_final: list[tuple[str, str]],
) -> list[Change]:
    sb, sf = baseline["state"], final["state"]
    vb, vf = baseline["volatile"], final["volatile"]
    changes = diff_files(sb["files"], sf["files"])
    changes += diff_logs(vb["logs"], vf["logs"])
    changes += diff_fs_paths(docker_base, docker_final, sb["files"], sf["files"])
    changes += diff_services(vb["services"], vf["services"])
    changes += diff_processes(vb["processes"], vf["processes"], vb["services"], vf["services"])
    changes += diff_db(sb["db"], sf["db"])
    return changes
