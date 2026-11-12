"""Shared machinery for rank/detail commands: body building, rendering, batching."""

from __future__ import annotations

import datetime
from concurrent.futures import ThreadPoolExecutor

from .. import api, render, validate
from ..config import Config
from ..core import Flag
from ..errors import KaloError, UsageError

DETAIL_CONCURRENCY = 5  # client-side batching; upstream detail endpoints take one id
URL_EXPIRY_NOTE = "Signed URLs expire ~5 minutes after this call — use them now, don't cache them"

# ---------------------------------------------------------------- flag presets


def common_flags(tier: str, default_range: str = "last30Day") -> list[Flag]:
    return [
        region_flag(),
        Flag(
            "--range",
            f"date window: {validate.tier_hint(tier)}",
            default=default_range,
            metavar="RANGE",
        ),
        Flag("--currency", "currency code (default from config, USD)", metavar="CUR"),
        lang_flag(),
    ]


def list_flags(sort_fields: tuple, default_sort: str = "revenue") -> list[Flag]:
    return [
        Flag("--page", "page number 1-5", kind="int", default=1, min=1, max=5),
        Flag("--limit", "items per page 5-100", kind="int", default=20, min=5, max=100),
        Flag("--sort", f"sort field: {'|'.join(sort_fields)}", default=default_sort),
        Flag("--asc", "sort ascending (default descending)", kind="flag", default=False),
        Flag("--fields", "comma-separated extra output fields (see --help)", metavar="F1,F2"),
    ]


def category_flag() -> Flag:
    return Flag(
        "--category", "category id filter (repeatable)", kind="multi", default=[], metavar="ID"
    )


def region_flag() -> Flag:
    return Flag("--region", f"region code ({'|'.join(validate.REGIONS)})", metavar="REGION")


def lang_flag() -> Flag:
    return Flag("--lang", "language code (default from config, en-US)", metavar="LANG")


def images_flag() -> Flag:
    return Flag("--images", "include image URLs in the output", kind="flag", default=False)


def apply_images(opts: dict, body: dict, image_field: str) -> None:
    """--images: ask upstream for image URLs and surface the column in the output."""
    if not opts.get("images"):
        return
    body["need_image"] = 1
    spec = opts.get("fields")
    if not spec:
        opts["fields"] = image_field
    elif image_field not in spec:
        opts["fields"] = f"{spec},{image_field}"


# ---------------------------------------------------------------- body helpers


def region_of(cfg: Config, opts: dict) -> str:
    """Validated upper-case region — the only body field some endpoints take."""
    region = (opts.get("region") or cfg.region).upper()
    if region not in validate.REGIONS:
        raise UsageError(
            f"unknown region '{region}'", [f"valid regions: {', '.join(validate.REGIONS)}"]
        )
    return region


def base_body(cfg: Config, opts: dict, tier: str, default_range: str = "last30Day") -> dict:
    region = region_of(cfg, opts)
    rng = opts.get("range") or default_range
    err = validate.check_date_range(rng, tier)
    if err:
        raise UsageError(err)
    return {
        "region": region,
        "language": opts.get("lang") or cfg.language,
        "currency": opts.get("currency") or cfg.currency,
        "date_range": rng,
    }


def put_optional(body: dict, opts: dict, mapping: dict[str, str]) -> None:
    """Copy present opts into the body under upstream field names."""
    for opt_key, field in mapping.items():
        v = opts.get(opt_key)
        if v not in (None, "", []):
            body[field] = v


def put_range(body: dict, opts: dict, opt_key: str, field: str, flag: str) -> None:
    v = opts.get(opt_key)
    if v in (None, ""):
        return
    err = validate.check_minmax(v, flag)
    if err:
        raise UsageError(err)
    body[field] = v


def put_numeric_id(body: dict, opts: dict, opt_key: str, field: str, flag: str) -> None:
    v = opts.get(opt_key)
    if v in (None, ""):
        return
    err = validate.check_numeric_id(v, flag)
    if err:
        raise UsageError(err)
    body[field] = v


def tri_state(opts: dict, on_key: str, off_key: str, on_flag: str, off_flag: str) -> int | None:
    if opts.get(on_key) and opts.get(off_key):
        raise UsageError(f"{on_flag} and {off_flag} are mutually exclusive")
    if opts.get(on_key):
        return 1
    if opts.get(off_key):
        return 0
    return None


# ---------------------------------------------------------------- field/number utils

_NUMERIC_HINTS = (
    "revenue",
    "views",
    "price",
    "followers",
    "count",
    "volumn",
    "rate",
    "roas",
    "gpm",
    "number",
    "sale",
    "viewers",
    "duration",
    "rank",
)


def ms_date(ms) -> str:
    """Unix milliseconds -> yyyy-MM-dd (upstream timestamps are all ms)."""
    try:
        return datetime.datetime.fromtimestamp(
            float(ms) / 1000, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(ms)


def flatten_rich_text(value) -> str:
    """Upstream rich text is nested blocks ({type,text,sub:[{t}]}) — collapse to plain text."""
    if isinstance(value, str):
        return value.strip()
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            text = node.get("text") or node.get("t")
            if isinstance(text, str):
                chunks.append(text)
            else:
                walk(node.get("sub"))

    walk(value)
    return " ".join(" ".join(chunks).split())


def coerce_numbers(row: dict) -> dict:
    """Upstream returns some numeric fields as strings — normalize at the boundary."""
    out = {}
    for k, v in row.items():
        if isinstance(v, str) and any(h in k for h in _NUMERIC_HINTS):
            try:
                out[k] = float(v) if "." in v else int(v)
                continue
            except ValueError:
                pass
        out[k] = v
    return out


def resolve_fields(opts: dict, default_fields: tuple, all_fields: tuple) -> list[str]:
    spec = opts.get("fields")
    if not spec:
        return list(default_fields)
    fields = list(default_fields)
    for f in [s.strip() for s in spec.split(",") if s.strip()]:
        if f not in all_fields:
            raise UsageError(
                f"unknown field '{f}' for --fields",
                [f"valid fields: {', '.join(all_fields)}"],
            )
        if f not in fields:
            fields.append(f)
    return fields


def summarize_trend(d: dict, full: bool) -> None:
    """revenue_trend arrives ×100 — divide, then compress unless --full."""
    trend = d.get("revenue_trend")
    if not isinstance(trend, list) or not trend:
        return
    values = []
    normalized = []
    for item in trend:
        if isinstance(item, dict):
            fixed = dict(item)
            for k, v in item.items():
                try:
                    fixed[k] = round(float(v) / 100, 2)
                    values.append(fixed[k])
                except (TypeError, ValueError):
                    pass
            normalized.append(fixed)
        else:
            try:
                x = round(float(item) / 100, 2)
                values.append(x)
                normalized.append(x)
            except (TypeError, ValueError):
                normalized.append(item)
    if full or not values:
        d["revenue_trend"] = normalized
        return
    d["revenue_trend"] = {
        "points": len(trend),
        "total": round(sum(values), 2),
        "avg": round(sum(values) / len(values), 2),
        "peak": round(max(values), 2),
    }


# ---------------------------------------------------------------- runners


def run_rank(
    cfg: Config,
    opts: dict,
    *,
    endpoint: str,
    noun: str,
    tier: str,
    default_fields: tuple,
    all_fields: tuple,
    sort_fields: tuple,
    body_extra: dict,
    title_fields: tuple = (),
    suggestions: tuple = (),
    omit_sort: bool = False,
    timeout: float = 30.0,
    fetch=None,
) -> int:
    body = base_body(cfg, opts, tier)
    page = validate.clamp(int(opts.get("page") or 1), 1, 5)
    limit = validate.clamp(int(opts.get("limit") or 20), 5, 100)
    body["page_number"] = page
    body["page_size"] = limit
    if not omit_sort:
        sort = opts.get("sort") or "revenue"
        if sort not in sort_fields:
            raise UsageError(
                f"invalid --sort '{sort}' for `{noun}`",
                [f"valid sort fields: {', '.join(sort_fields)}"],
            )
        body["sort_field"] = {"field": sort, "type": "ASC" if opts.get("asc") else "DESC"}
    body.update(body_extra)
    fields = resolve_fields(opts, default_fields, all_fields)

    data = (fetch(body) if fetch is not None else
            api.request(cfg, endpoint, body, timeout=timeout, empty_on_not_found=True))
    rows = [coerce_numbers(r) for r in api.as_list(data)]

    if opts.get("json"):
        render.emit_json(rows)
        return 0

    context = f"region {body['region']}, {body['date_range']}"
    if not rows:
        render.emit_empty(
            noun,
            context,
            ["Broaden --range, drop filters, or try another --region"],
        )
        return 0

    display = [{k: render.clip(r.get(k)) if k in title_fields else r.get(k) for k in fields} for r in rows]
    render.emit_table(noun, display, fields)
    hints = list(suggestions)
    if len(rows) == limit:
        hints.append(f"More may exist: add `--page {page + 1}` (pages 1-5, --limit up to 100)")
    render.emit_help(hints)
    return 0


def gather(fetch, ids: list[str]) -> list[tuple]:
    """Run fetch(id) over ids -> [(id, data, error)]; upstream detail endpoints take one id."""

    def one(entity_id: str):
        try:
            return entity_id, fetch(entity_id), None
        except KaloError as e:
            return entity_id, None, e

    if len(ids) == 1:
        return [one(ids[0])]
    with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
        return list(pool.map(one, ids))


def run_url_batch(
    cfg: Config,
    opts: dict,
    ids: list[str],
    *,
    noun: str,
    fetch,  # fetch(id) -> dict
    extract,  # extract(id, data) -> list[dict] of table rows
    fields: tuple,
) -> int:
    """Signed-URL endpoints: one table across ids, expiry called out (URLs die in ~5 min)."""
    results = gather(fetch, ids)
    rows: list[dict] = []
    errors = []
    for entity_id, data, err in results:
        if err is not None:
            errors.append((entity_id, err))
            continue
        rows.extend(extract(entity_id, data))

    if opts.get("json"):
        payload = {"rows": rows}
        if errors:
            payload["errors"] = [{"id": i, "error": str(e)} for i, e in errors]
        render.emit_json(payload)
        return 0 if rows else 1

    for entity_id, err in errors:
        render.emit_error(f"{noun} {entity_id}: {err}", err.help_lines)
    if not rows:
        return 1 if errors else _empty_urls(noun, ids)
    render.emit_table(noun, rows, fields)
    render.emit_help([URL_EXPIRY_NOTE])
    return 0


def _empty_urls(noun: str, ids: list[str]) -> int:
    render.emit_empty(noun, f"{len(ids)} id(s)")
    return 0


def run_detail_batch(
    cfg: Config,
    opts: dict,
    ids: list[str],
    *,
    noun: str,
    fetch,  # fetch(id) -> dict
    postprocess=None,  # postprocess(dict, opts, notes) -> dict
    suggestions: tuple = (),
) -> int:
    results = gather(fetch, ids)

    if opts.get("json"):
        payload = [
            {"id": i, "data": d} if e is None else {"id": i, "error": str(e)}
            for i, d, e in results
        ]
        render.emit_json(payload[0] if len(payload) == 1 else payload)
        return 0 if any(e is None for _, _, e in results) else 1

    ok = 0
    first = True
    notes: list[str] = []
    for entity_id, data, err in results:
        if not first:
            render.out()
        first = False
        if err is not None:
            render.emit_error(f"{noun} {entity_id}: {err}", err.help_lines)
            continue
        if not isinstance(data, dict) or not data:
            render.emit_empty(noun, f"id {entity_id}")
            continue
        ok += 1
        data = coerce_numbers(data)
        if postprocess:
            data = postprocess(data, opts, notes) or data
        render.emit_detail(noun, data)
    render.emit_help(list(dict.fromkeys(notes)) + list(suggestions if ok else ()))
    return 0 if ok or not ids else 1
