"""Fixture shaped like installed paper_broker/orb_v4_paper.py anchors.

Not the full engine. Exit and flatten lines match the host file.
The submit assignment target stays o, and later lines keep using that name.
"""

def submit_moc(client, req, payload):
    try:
        o = client.submit_order(req)
        return {"dry_run": False, **payload, "id": str(getattr(o, "id", "")), "status": str(getattr(o, "status", ""))}
    except Exception as e:
        return {"error": str(e)}


def cmd_exit(client, due, have_moc, oo, today, out, eng):
    for r in sorted(due, key=lambda x: x["symbol"]):
        if r["symbol"] in have_moc:
            out.append({"symbol": r["symbol"], "action": "moc_already_open"})
            continue
        res = submit_moc(client, None, {})
        out.append(res)


def cmd_eod(client, flat_syms, dry_run, log, pos, cb, oo, today, acts, eng):
    if flat_syms:
        fo = flatten(client, sorted(flat_syms), dry_run=dry_run, log=log)
        acts.append(fo)
