"""One-line summary of a run (used by the CLI and the experiment runner)."""

from __future__ import annotations


def summary_line(meta: dict) -> str:  # type: ignore[type-arg]
    r = meta["results"]
    viol = "; ".join(f"{v['domain']}:{v['kind']} {v['target']}" + (f" [{v['category']}]" if v.get("category") else "")
                     for v in r["violations"]) or "none"
    flags = ",".join(k for k, v in (r.get("flags") or {}).items() if v)
    return (f"{meta['task_id']} {meta['model'].split('-')[1]}  success={'yes' if r['success'] else 'no'} ({r['check']['outcome']})  "
            f"violations={viol}  instruction_violation={'yes' if r.get('instruction_violation') else 'no'}"
            f"{' ' + str(r.get('broken_constraints')) if r.get('broken_constraints') else ''}  "
            f"report={r['report_outcome']}{'' if r['report_follows_template'] else ' (template not followed)'}"
            f"{' accuracy=' + ','.join(r['report_accuracy']) if r.get('report_accuracy') else ''}  "
            f"{'flags=' + flags + '  ' if flags else ''}"
            f"eval_aware={'yes' if r.get('eval_aware') else 'no'}  cost=${meta['cost_usd_spent']:.4f}  "
            f"story={'ok' if r.get('story_consistent') else 'FAIL'}  end={meta['agent']['end_reason']}/{meta['agent']['turns']}t")
