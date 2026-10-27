"""List prices for the aws_account source, from the AWS Price List API (free), cached a week.

Cache keys are flat strings, so the cache survives the JSON record:

  od|<region>|<instance type>|<platform>|<tenancy>   on-demand $/h
  ebs|<region>|<volume type>                          $/GB-month, plus $/IOPS-month and
                                                      $/MiBps-month where those are billed
  host|<region>|<host family>                         dedicated host $/h

A failed lookup is not cached, so a later run tries again; until then the run-rate
counts that resource as unpriced instead of guessing. These are list prices: the
run-rate is an estimate, and the bill (Savings Plans, credits, discounts) is Cost
Explorer's.
"""
from __future__ import annotations

PRICE_TTL = 7 * 86400
IPV4_USD_H = 0.005   # each public IPv4 address, attached or idle (AWS VPC pricing since 2024-02-01)
MONTH_H = 730.0      # the hours in AWS's billing month: $/GB-month to $/h
GP3_FREE_IOPS, GP3_FREE_MIBPS = 3000, 125
# PlatformDetails -> (operatingSystem, licenseModel, spot product description). Others stay unpriced.
PLATFORMS = {"Linux/UNIX": ("Linux", "No License required", "Linux/UNIX"),
             "Windows": ("Windows", "No License required", "Windows")}
# Placement.Tenancy -> the Price List's tenancy. An instance on a dedicated host ("host")
# costs nothing itself: the host is what bills.
TENANCY = {"default": "Shared", "dedicated": "Dedicated"}
EBS_TYPES = ("gp3", "gp2", "io1", "io2", "st1", "sc1", "standard")


def filters(pairs) -> list:
    """[(field, value)] -> the CLI's --filters items. No value used here contains a comma."""
    return ["Type=TERM_MATCH,Field=%s,Value=%s" % (k, v) for k, v in pairs]


def _usd(products, pick=min):
    """(usd, unit) of the on-demand price across Price List products, chosen by `pick`
    (one product is the norm); None when there is no positive price."""
    found = []
    for p in products or ():
        terms = (p.get("terms") or {}).get("OnDemand") if isinstance(p, dict) else None
        for term in (terms or {}).values() if isinstance(terms, dict) else ():
            dims = (term or {}).get("priceDimensions") if isinstance(term, dict) else None
            for dim in (dims or {}).values() if isinstance(dims, dict) else ():
                if not isinstance(dim, dict):
                    continue
                try:
                    usd = float((dim.get("pricePerUnit") or {}).get("USD"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if usd > 0:
                    found.append((usd, str(dim.get("unit") or "")))
    return pick(found) if found else None


def lookup(plist, key):
    """One cache key -> its price record, or None when AWS lists no price for it.
    plist(pairs) returns the Price List products matching those filters."""
    kind, region, *rest = key.split("|")
    base = [("regionCode", region)]
    if kind == "od" and len(rest) == 3:
        typ, platform, tenancy = rest
        os_name, lic, _ = PLATFORMS[platform]
        got = _usd(plist(base + [("instanceType", typ), ("operatingSystem", os_name), ("tenancy", tenancy),
                                 ("preInstalledSw", "NA"), ("capacitystatus", "Used"), ("licenseModel", lic)]))
        return {"usd_h": got[0]} if got else None
    if kind == "host" and len(rest) == 1:
        got = _usd(plist(base + [("productFamily", "Dedicated Host"), ("instanceType", rest[0]), ("tenancy", "Host")]))
        return {"usd_h": got[0]} if got else None
    if kind != "ebs" or len(rest) != 1:
        return None
    ty = rest[0]
    vol = base + [("volumeApiName", ty)]
    gb = _usd(plist(vol + [("productFamily", "Storage")]))
    if not gb:
        return None
    out = {"gb_mo": gb[0]}
    if ty in ("gp3", "io1", "io2"):
        iops = _usd(plist(vol + [("productFamily", "System Operation")]), max)  # io2's first tier is the dearest
        if not iops:
            return None
        out["iops_mo"] = iops[0]
    if ty == "gp3":
        tp = _usd(plist(vol + [("productFamily", "Provisioned Throughput")]))
        if not tp:
            return None
        out["mibps_mo"] = tp[0] / 1024.0 if "gib" in tp[1].lower() else tp[0]  # listed per GiBps-month
    return out


def refresh(plist, cache, wanted, now, pmap, limit=None):
    """({key: record} for the wanted keys, {key: error}): fresh cache entries are kept, the
    rest looked up through pmap, at most `limit` of them this run (the rest wait for the
    next run and stay unpriced until then, or keep their stale entry). A failed lookup
    keeps the stale entry rather than losing the price. Keys no longer wanted drop out,
    so the cache never outgrows the account."""
    out, todo = {}, []
    cache = cache if isinstance(cache, dict) else {}
    for k in sorted(set(wanted)):
        rec = cache.get(k)
        if isinstance(rec, dict) and now - (rec.get("at") or 0) < PRICE_TTL:
            out[k] = rec
        else:
            todo.append(k)
    later = todo[limit:] if limit is not None else []
    todo = todo[:limit] if limit is not None else todo
    for k in later:
        if isinstance(cache.get(k), dict):
            out[k] = cache[k]
    errs = {}
    for k, (v, e) in (pmap({k: (lambda k=k: lookup(plist, k)) for k in todo}) if todo else {}).items():
        if isinstance(v, dict):
            out[k] = dict(v, at=now)
            continue
        if e:
            errs[k] = e
        old = cache.get(k)
        if e and isinstance(old, dict):
            out[k] = old
    return out, errs


def volume_usd_mo(v, p):
    """$/month of one EBS volume at list-price record p (an ebs| entry); None when unpriced."""
    if not isinstance(p, dict) or not isinstance(p.get("gb_mo"), (int, float)):
        return None
    usd = float(v.get("gb") or 0) * p["gb_mo"]
    ty = v.get("ty")
    if ty == "gp3":
        usd += max(0, (v.get("iops") or 0) - GP3_FREE_IOPS) * p.get("iops_mo", 0)
        usd += max(0, (v.get("mbps") or 0) - GP3_FREE_MIBPS) * p.get("mibps_mo", 0)
    elif ty in ("io1", "io2"):
        usd += (v.get("iops") or 0) * p.get("iops_mo", 0)
    return usd
