"""`kalo product` — rankings, image search, details and insights."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from .. import api, render
from ..core import Command, Flag
from ..errors import KaloError, UsageError
from . import common

SORT_FIELDS = (
    "revenue",
    "video_revenue",
    "showcase_revenue",
    "commission_rate",
    "revenue_growth_rate",
    "sales_volumn",
    "unit_price",
    "launch_date",
)
ALL_FIELDS = (
    "product_id",
    "product_name",
    "launch_date",
    "revenue",
    "commission_rate",
    "revenue_growth_rate",
    "sales_volumn",
    "unit_price",
    "live_revenue",
    "video_revenue",
    "showcase_revenue",
    "master_image_url",
)
DEFAULT_FIELDS = ("product_id", "product_name", "revenue", "sales_volumn")

DESCRIPTION_LIMIT = 500  # product_description is marketing copy — a taste is enough
SUMMARY_LIMIT = 500
COMMENT_TEXT_LIMIT = 120
PENDING_MARKER = "file not found"  # upstream's "analysis not generated yet" signal


def rank(cfg, opts, args):
    body = {}
    common.put_optional(
        body,
        opts,
        {
            "shop": "shop_id",
            "creator": "creator_id",
            "video": "video_id",
            "live": "livestream_id",
            "commission": "commission_rate",
            "delivery": "delivery_type",
            "launch": "launch_date",
            "keyword": "keyword",
        },
    )
    common.put_range(body, opts, "price", "unit_price_range", "--price")
    common.put_range(body, opts, "revenue", "revenue_range", "--revenue")
    if opts.get("category"):
        body["category_ids"] = opts["category"]
    aff = common.tri_state(opts, "affiliate", "no_affiliate", "--affiliate", "--no-affiliate")
    if aff is not None:
        body["is_affiliate"] = aff
    tts = common.tri_state(opts, "tts", "no_tts", "--tts", "--no-tts")
    if tts is not None:
        body["is_tts_product"] = tts
    if opts.get("all"):
        body["need_all"] = True
    common.apply_images(opts, body, "master_image_url")
    return common.run_rank(
        cfg,
        opts,
        endpoint="/tiktok/product/rank",
        noun="products",
        tier="rank",
        default_fields=DEFAULT_FIELDS,
        all_fields=ALL_FIELDS,
        sort_fields=SORT_FIELDS,
        body_extra=body,
        title_fields=("product_name",),
        suggestions=(
            "Run `kalo product detail <product_id>` for price range, shop id, revenue trend",
            "Run `kalo creator rank --product <product_id>` for creators selling a product",
        ),
    )


def picsearch(cfg, opts, args):
    image = Path(args[0]).expanduser()
    try:
        if not image.is_file():
            raise UsageError(f"image file not found or not a regular file: {image}")
        content_type = mimetypes.guess_type(image.name)[0]
        if not content_type or not content_type.startswith("image/"):
            raise UsageError("expected an image file, e.g. product.jpg, product.png or product.webp")
        contents = image.read_bytes()
    except OSError as exc:
        raise UsageError(f"cannot read image file '{image}': {exc.strerror}") from exc
    if not contents:
        raise UsageError(f"image file is empty: {image}")

    endpoint = "/tiktok/product/picsearch/one-step"

    def fetch(body):
        return api.request(
            cfg, endpoint, {"req": json.dumps(body)},
            files={"pic": (image.name, contents, content_type)},
            auth_header="X-API-Key",
            timeout=60, empty_on_not_found=True,
        )

    return common.run_rank(
        cfg, opts, endpoint=endpoint, noun="products", tier="rank",
        default_fields=DEFAULT_FIELDS, all_fields=ALL_FIELDS, sort_fields=SORT_FIELDS,
        body_extra={}, title_fields=("product_name",), fetch=fetch,
        suggestions=(
            "Run `kalo product detail <product_id>` for price range, shop id, revenue trend",
            "Run `kalo video rank --product <product_id>` for videos selling a matching product",
        ),
    )


def detail(cfg, opts, ids):
    def fetch(product_id):
        body = common.base_body(cfg, opts, "detail")
        body.update(product_id=product_id, need_extra=True)
        if opts.get("images"):
            body["need_image"] = 1
        return api.request(cfg, "/tiktok/product/detail", body, timeout=10)

    def post(d, o, notes):
        common.summarize_trend(d, o.get("full"))
        if "product_description" in d:
            d["product_description"] = common.flatten_rich_text(d["product_description"])
            render.truncate_text(
                d,
                "product_description",
                DESCRIPTION_LIMIT,
                o.get("full"),
                notes,
                "Add `--full` for the complete product_description",
            )
        if not o.get("full"):
            notes.append("Add `--full` for the day-by-day revenue_trend series")
        return d

    return common.run_detail_batch(
        cfg,
        opts,
        ids,
        noun="product",
        fetch=fetch,
        postprocess=post,
        suggestions=(
            "Run `kalo shop detail <product_shop_id>` to resolve the shop name",
            "Run `kalo video rank --product <product_id>` for its top videos",
        ),
    )


def images(cfg, opts, ids):
    def fetch(product_id):
        return api.request(cfg, "/tiktok/product/images", {"product_id": product_id})

    def extract(product_id, data):
        urls = (data or {}).get("images") or []
        return [{"product_id": data.get("product_id") or product_id, "image_url": u} for u in urls]

    return common.run_url_batch(
        cfg,
        opts,
        ids,
        noun="product_images",
        fetch=fetch,
        extract=extract,
        fields=("product_id", "image_url"),
    )


def specs(cfg, opts, ids):
    region = common.region_of(cfg, opts)
    language = opts.get("lang") or cfg.language

    def fetch(product_id):
        body = {"product_id": product_id, "region": region, "language": language}
        try:
            return api.request(cfg, "/tiktok/product/analysis", body, timeout=30)
        except KaloError as e:
            # "file not found, 0|1" = the upstream analysis is still being generated;
            # nothing was charged, so re-running is the fix.
            if PENDING_MARKER in str(e).lower():
                raise KaloError(
                    "specs are still being generated upstream (this call was not charged)",
                    kind="pending",
                    help_lines=["Re-run the same command in a few seconds — retries are free until it succeeds"],
                ) from e
            raise

    def post(d, o, notes):
        return {
            "product_id": d.get("product_id"),
            "region": d.get("region"),
            "language": d.get("language"),
            "highlights": [
                {"key_word": h.get("key_word"), "region_text": h.get("region_text")}
                for h in d.get("highlights") or []
                if isinstance(h, dict)
            ],
            "attributes": [
                {"key": a.get("key"), "value": a.get("value")}
                for a in d.get("attributes") or []
                if isinstance(a, dict)
            ],
        }

    return common.run_detail_batch(
        cfg,
        opts,
        ids,
        noun="product_specs",
        fetch=fetch,
        postprocess=post,
        suggestions=(
            "key_word is the selling point; region_text is the seller's own wording on the listing",
            "Run `kalo product comments <product_id>` to check what buyers actually say about them",
        ),
    )


def _points(items) -> list[dict]:
    """view_point + how many reviews back it — the zh mirror and uuids are noise here."""
    rows = []
    for it in items or []:
        if isinstance(it, dict):
            rows.append(
                {"point": it.get("view_point"), "mentions": len(it.get("id_list") or [])}
            )
    rows.sort(key=lambda r: -(r["mentions"] or 0))
    return rows


def _comment_rows(items, full: bool) -> list[dict]:
    """The real fields live under `review`; the flat duplicates upstream sends are all null."""
    rows = []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        review = c.get("review") if isinstance(c.get("review"), dict) else {}
        text = review.get("display_text") or review.get("comment") or c.get("display_text")
        rows.append(
            {
                "rating": review.get("rating") or c.get("rating"),
                "date": common.ms_date(review.get("review_timestamp") or c.get("review_timestamp")),
                "sku": c.get("sku_specification"),
                "text": text if full else render.clip(text, COMMENT_TEXT_LIMIT),
            }
        )
    rows.sort(key=lambda r: str(r["date"]), reverse=True)
    return rows


def comments(cfg, opts, ids):
    country = common.region_of(cfg, opts).lower()

    def fetch(product_id):
        return api.request(
            cfg,
            "/tiktok/product/comment/insight",
            {"product_id": product_id, "country_code": country},
            timeout=60,
        )

    def post(d, o, notes):
        full = o.get("full")
        summary = (d.get("comment_summary") or {}).get("summary")
        raw = d.get("original_comments") or []
        out = {
            "product_id": d.get("product_id"),
            "country_code": d.get("country_code"),
            "cached_at": d.get("cached_at"),
            "comments_analyzed": len(raw),
            "summary": summary,
        }
        render.truncate_text(
            out, "summary", SUMMARY_LIMIT, full, notes, "Add `--full` for the complete summary"
        )
        out["pain_points"] = _points(d.get("pain_points"))
        out["positive_points"] = _points(d.get("positive_points"))
        out["usage_scenarios"] = _points(d.get("usage_scenarios"))
        if o.get("comments"):
            out["comments"] = _comment_rows(raw, full)
        elif raw:
            notes.append(f"Add `--comments` for the {len(raw)} raw customer comments")
        return out

    return common.run_detail_batch(
        cfg,
        opts,
        ids,
        noun="product_comments",
        fetch=fetch,
        postprocess=post,
        suggestions=(
            "Insight is cached upstream: re-running before cached_at moves costs nothing",
            "Run `kalo product detail <product_id>` for price, shop and revenue context",
        ),
    )


COMMANDS = [
    Command(
        path="product rank",
        summary="Top TikTok Shop products by revenue/sales",
        handler=rank,
        flags=common.common_flags("rank")
        + common.list_flags(SORT_FIELDS)
        + [
            common.category_flag(),
            Flag("--shop", "filter by shop id", metavar="ID"),
            Flag("--creator", "filter by creator id", metavar="ID"),
            Flag("--video", "filter by video id", metavar="ID"),
            Flag("--live", "filter by livestream id", metavar="ID"),
            Flag("--price", 'unit price range "min-max"', metavar="MIN-MAX"),
            Flag("--revenue", 'revenue range "min-max"', metavar="MIN-MAX"),
            Flag("--commission", "commission rate filter", metavar="RATE"),
            Flag("--launch", "launch window: <3 | <7 | >30 days", choices=("<3", "<7", ">30")),
            Flag("--delivery", "delivery type", choices=("local", "global")),
            Flag("--keyword", "product name keyword", metavar="TEXT"),
            Flag("--affiliate", "only affiliate (commissioned) products", kind="flag", default=False),
            Flag("--no-affiliate", "only non-affiliate products", kind="flag", default=False),
            Flag("--tts", "only fully-managed (TTS) products", kind="flag", default=False),
            Flag("--no-tts", "exclude fully-managed products", kind="flag", default=False),
            Flag("--all", "include zero-sales products", kind="flag", default=False),
            common.images_flag(),
        ],
        examples=[
            "kalo product rank --region US --category 601739 --sort revenue",
            "kalo product rank --keyword 'hair dryer' --price 20-100 --launch '<7'",
        ],
    ),
    Command(
        path="product picsearch",
        summary="Find similar TikTok Shop products by uploading a local image file",
        handler=picsearch,
        flags=common.common_flags("rank") + common.list_flags(SORT_FIELDS),
        positional="image_path",
        pos_min=1,
        pos_max=1,
        examples=[
            "kalo product picsearch /path/to/product.jpg --region US --range last30Day",
            "kalo product picsearch /path/to/product.png --sort sales_volumn --limit 20 --json",
        ],
    ),
    Command(
        path="product detail",
        summary="Full metrics for one or more products (batched client-side)",
        handler=lambda cfg, opts, args: detail(cfg, opts, args),
        flags=common.common_flags("detail")
        + [
            Flag("--full", "include the full revenue_trend series", kind="flag", default=False),
            common.images_flag(),
        ],
        positional="product_id",
        pos_min=1,
        pos_max=None,
        examples=["kalo product detail 1729386274 1729399999"],
    ),
    Command(
        path="product comments",
        summary="AI review insight: pain points, positives, usage scenarios",
        handler=lambda cfg, opts, args: comments(cfg, opts, args),
        flags=[
            common.region_flag(),
            Flag("--comments", "list the raw customer comments too", kind="flag", default=False),
            Flag("--full", "untruncated summary and comment text", kind="flag", default=False),
        ],
        positional="product_id",
        pos_min=1,
        pos_max=None,
        examples=[
            "kalo product comments 1729383749752951276",
            "kalo product comments 1729383749752951276 --region PH --comments",
        ],
    ),
    Command(
        path="product specs",
        summary="Selling points and spec attributes for a product",
        handler=lambda cfg, opts, args: specs(cfg, opts, args),
        flags=[
            common.region_flag(),
            Flag(
                "--lang",
                "language code; zh-CN translates the copy, others keep the listing's own wording",
                metavar="LANG",
            ),
        ],
        positional="product_id",
        pos_min=1,
        pos_max=None,
        examples=[
            "kalo product specs 1729448464509734958",
            "kalo product specs 1735489179592131679 --region PH --lang zh-CN",
        ],
    ),
    Command(
        path="product images",
        summary="Product gallery image URLs (signed, ~5 min expiry)",
        handler=lambda cfg, opts, args: images(cfg, opts, args),
        positional="product_id",
        pos_min=1,
        pos_max=None,
        examples=["kalo product images 1729448464509734958"],
    ),
]
