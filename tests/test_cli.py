import json

from kalodata.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_home_without_config(capsys, monkeypatch, tmp_path):
    for var in ("KALODATA_API_KEY", "KALODATA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KALODATA_CONFIG_DIR", str(tmp_path))
    code, out = run(capsys)
    assert code == 0
    assert "commands[" in out
    assert "auth: not configured" in out
    assert "setup[3]:" in out  # onboarding guide, not just a bare error
    assert "open-center/account" in out  # where the key comes from
    assert "kalo config set --key" in out


def test_query_without_key_shows_setup_guide(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("KALODATA_API_KEY", raising=False)
    monkeypatch.setenv("KALODATA_CONFIG_DIR", str(tmp_path))
    code, out = run(capsys, "product", "rank")
    assert code == 1
    assert "no KaloData API key configured yet" in out
    assert "open-center/account" in out
    assert "kalo config set --key" in out


def test_unknown_flag_exits_2(capsys, env):
    code, out = run(capsys, "product", "rank", "--stat", "x")
    assert code == 2
    assert out.startswith("error: unknown flag --stat")
    assert "--region" in out  # valid flags inlined for one-turn self-correction


def test_renamed_flag_hint(capsys, env):
    code, out = run(capsys, "product", "rank", "--limit", "abc")
    assert code == 2
    assert "expects an integer" in out


def test_rank_happy_path(capsys, env):
    env.responses["/tiktok/product/rank"] = {
        "success": True,
        "data": [
            {
                "product_id": "111",
                "product_name": "Widget, deluxe",
                "revenue": 1234.5,
                "sales_volumn": 42,
                "unit_price": 9.99,
            }
        ],
    }
    code, out = run(capsys, "product", "rank", "--region", "US")
    assert code == 0
    assert "products[1]{product_id,product_name,revenue,sales_volumn}:" in out
    assert '"111","Widget, deluxe",1234.5,42' in out
    assert "kalo product detail" in out  # contextual disclosure
    path, headers, body = env.requests[0]
    assert headers["secret-key"] == "test-key"
    assert headers["source-type"] == "SKILL"
    assert "x-user-id" not in headers
    assert body["sort_field"] == {"field": "revenue", "type": "DESC"}
    assert body["date_range"] == "last30Day"


def test_rank_empty_state(capsys, env):
    env.responses["/tiktok/product/rank"] = {"success": False, "message": "record not found"}
    code, out = run(capsys, "product", "rank")
    assert code == 0
    assert out.startswith("products: 0 results (region US, last30Day)")


def test_membership_error(capsys, env):
    env.responses["/tiktok/product/rank"] = {
        "success": False,
        "code": "2016",
        "message": "query count exceeded",
    }
    code, out = run(capsys, "product", "rank")
    assert code == 1
    assert "membership/quota limit" in out
    assert "Narrow --range" in out


def test_2016_parameter_misclassification(capsys, env):
    env.responses["/tiktok/product/rank"] = {
        "success": False,
        "code": "2016",
        "message": "date_range must be one of ...",
    }
    code, out = run(capsys, "product", "rank")
    assert code == 1
    assert "rejected a parameter" in out


def test_rejected_key_is_auth_error(capsys, env):
    env.responses["/tiktok/product/rank"] = {
        "success": False, "code": "501", "message": "The key is not allowed",
    }
    code, out = run(capsys, "product", "rank")
    assert code == 1
    assert "rejected the API key" in out
    assert "kalo config set --key" in out


def test_501_parameter_error_is_not_auth(capsys, env):
    env.responses["/tiktok/product/rank"] = {
        "success": False, "code": "501", "message": "region and language are required",
    }
    code, out = run(capsys, "product", "rank")
    assert code == 1
    assert "rejected the API key" not in out


def test_limit_out_of_range_is_clamped(capsys, env):
    code, _ = run(capsys, "product", "rank", "--limit", "1")
    assert code == 0
    assert env.requests[0][2]["page_size"] == 5


def test_detail_trend_normalized(capsys, env):
    env.responses["/tiktok/product/detail"] = {
        "success": True,
        "data": {
            "product_id": "111",
            "product_name": "Widget",
            "revenue": 100,
            "revenue_trend": [1000, 3000, 2000],
        },
    }
    code, out = run(capsys, "product", "detail", "111")
    assert code == 0
    # trend arrives x100 -> /100, then summarized
    assert "total: 60" in out
    assert "peak: 30" in out
    body = env.requests[0][2]
    assert body["product_id"] == "111"
    assert body["need_extra"] is True


def test_detail_full_trend(capsys, env):
    env.responses["/tiktok/product/detail"] = {
        "success": True,
        "data": {"product_id": "1", "revenue_trend": [1000, 3000]},
    }
    code, out = run(capsys, "product", "detail", "1", "--full")
    assert code == 0
    assert "revenue_trend[2]: 10,30" in out


def test_credit_balance_is_the_bare_default(capsys, env):
    env.responses["/credit/balance"] = {
        "success": True,
        "data": {"totalRemain": 12.8, "mainRemain": 12.8},
    }
    code, out = run(capsys, "credit")
    assert code == 0
    assert env.requests[0][0] == "/credit/balance"
    assert "total_remain: 12.8" in out
    # a main-account key omits subRemain entirely — never report it as a zero balance
    assert "sub_remain" not in out
    assert "main-account key" in out


def test_credit_balance_sub_account(capsys, env):
    env.responses["/credit/balance"] = {
        "success": True,
        "data": {"totalRemain": 10, "mainRemain": 8, "subRemain": 2},
    }
    code, out = run(capsys, "credit", "balance")
    assert code == 0
    assert "sub_remain: 2" in out
    assert "sub-account key" in out


def test_credit_logs_range_becomes_dates(capsys, env):
    env.responses["/credit/logs"] = {
        "success": True,
        "data": {
            "pageNo": 1,
            "pageSize": 20,
            "total": 1,
            "hasNext": False,
            "data": [
                {
                    "gmtCreated": 1786703233189,
                    "sourceType": "OPEN_API_CALL",
                    "displayQuantity": -0.5,
                    "kaloBizName": "kalodata",
                    "consumerId": 2795912,
                }
            ],
        },
    }
    code, out = run(capsys, "credit", "logs", "--range", "2026-08-01~2026-08-31", "--type", "OPEN_API_CALL")
    assert code == 0
    body = env.requests[0][2]
    assert body["startDate"] == "2026-08-01"
    assert body["endDate"] == "2026-08-31"
    assert body["sourceType"] == "OPEN_API_CALL"
    assert "credit_logs[1]{date,type,credits,product,consumer_id}:" in out
    assert "2026-08-14,OPEN_API_CALL,-0.5,kalodata,2795912" in out


def test_credit_logs_empty_explains_scope(capsys, env):
    env.responses["/credit/logs"] = {
        "success": True,
        "data": {"pageNo": 1, "pageSize": 20, "total": 0, "hasNext": False, "data": []},
    }
    code, out = run(capsys, "credit", "logs")
    assert code == 0
    assert "credit_logs: 0 results" in out
    assert "top-ups and refunds never appear" in out


def test_product_comments(capsys, env):
    env.responses["/tiktok/product/comment/insight"] = {
        "success": True,
        "data": {
            "product_id": "111",
            "country_code": "us",
            "cached_at": "2026-08-26T07:23:16Z",
            "comment_summary": {"summary": "Loved.", "summary_zh": "喜欢"},
            "pain_points": [
                {"view_point": "Runs small", "point_zh": "偏小", "id_list": ["1"]},
                {"view_point": "Zipper sticks", "point_zh": "拉链", "id_list": ["1", "2", "3"]},
            ],
            "positive_points": [{"view_point": "Soft fabric", "id_list": ["1", "2"]}],
            "usage_scenarios": [{"view_point": "Weddings", "id_list": ["4"]}],
            "original_comments": [
                {
                    "review_id": None,
                    "sku_specification": "Navy, 14",
                    "review": {
                        "review_id": "9",
                        "rating": "5",
                        "display_text": "Fits perfectly",
                        "review_timestamp": "1786703233189",
                    },
                }
            ],
        },
    }
    code, out = run(capsys, "product", "comments", "111", "--region", "PH")
    assert code == 0
    body = env.requests[0][2]
    assert body == {"product_id": "111", "country_code": "ph"}  # no date/currency on this endpoint
    assert "comments_analyzed: 1" in out
    # points are ranked by how many reviews back them, zh mirror and uuids dropped
    assert "pain_points[2]{point,mentions}:" in out
    assert out.index("Zipper sticks") < out.index("Runs small")
    assert "summary_zh" not in out
    assert "Add `--comments`" in out


def test_product_comments_raw_list(capsys, env):
    env.responses["/tiktok/product/comment/insight"] = {
        "success": True,
        "data": {
            "product_id": "111",
            "comment_summary": {"summary": "ok"},
            "original_comments": [
                {
                    "review": {
                        "rating": "5",
                        "display_text": "Fits perfectly",
                        "review_timestamp": "1786703233189",
                    },
                    "sku_specification": "Navy, 14",
                }
            ],
        },
    }
    code, out = run(capsys, "product", "comments", "111", "--comments")
    assert code == 0
    assert "Fits perfectly" in out
    assert "2026-08-14" in out


def test_product_specs(capsys, env):
    env.responses["/tiktok/product/analysis"] = {
        "success": True,
        "data": {
            "product_id": "111",
            "region": "PH",
            "language": "zh-CN",
            "highlights": [{"key_word": "纯本地米", "region_text": "Pure Local Rice"}],
            "attributes": [{"key": "品牌", "value": "Busilak"}],
        },
    }
    code, out = run(capsys, "product", "specs", "111", "--region", "PH", "--lang", "zh-CN")
    assert code == 0
    assert env.requests[0][2] == {"product_id": "111", "region": "PH", "language": "zh-CN"}
    assert "highlights[1]{key_word,region_text}:" in out
    assert "attributes[1]{key,value}:" in out


def test_product_specs_pending_is_actionable(capsys, env):
    # upstream kicks off generation and fails this call for free
    env.responses["/tiktok/product/analysis"] = {
        "success": False,
        "data": None,
        "message": "file not found, 0",
    }
    code, out = run(capsys, "product", "specs", "111")
    assert code == 1
    assert "still being generated" in out
    assert "not charged" in out
    assert "Re-run the same command" in out


def test_product_images_table(capsys, env):
    env.responses["/tiktok/product/images"] = {
        "success": True,
        "data": {"product_id": "111", "images": ["https://img/a.png", "https://img/b.png"]},
    }
    code, out = run(capsys, "product", "images", "111")
    assert code == 0
    assert env.requests[0][2] == {"product_id": "111"}
    assert "product_images[2]{product_id,image_url}:" in out
    assert "expire" in out  # signed URLs die in ~5 min; agent must not cache them


def test_video_url(capsys, env):
    env.responses["/tiktok/video/url"] = {
        "success": True,
        "data": {"video_id": "7", "video_url": "https://vid/7.mp4"},
    }
    code, out = run(capsys, "video", "url", "7")
    assert code == 0
    assert "video_urls[1]{video_id,video_url}:" in out
    assert "https://vid/7.mp4" in out


def test_product_description_flattened(capsys, env):
    env.responses["/tiktok/product/detail"] = {
        "success": True,
        "data": {
            "product_id": "111",
            "product_description": [
                {"type": "text", "text": "Boost your routine.", "sub": [{"t": "ignored"}]},
                {"type": "text", "sub": [{"t": "Second block."}]},
            ],
        },
    }
    code, out = run(capsys, "product", "detail", "111")
    assert code == 0
    assert "product_description: Boost your routine. Second block." in out


def test_product_description_truncated(capsys, env):
    env.responses["/tiktok/product/detail"] = {
        "success": True,
        "data": {"product_id": "111", "product_description": [{"type": "text", "text": "x" * 900}]},
    }
    code, out = run(capsys, "product", "detail", "111")
    assert code == 0
    assert "truncated, 900 chars total" in out
    assert "complete product_description" in out


def test_images_flag_adds_column(capsys, env):
    env.responses["/tiktok/shop/rank"] = {
        "success": True,
        "data": [{"shop_id": "9", "shop_name": "S", "revenue": 1, "sales_volumn": 2, "image_url": "https://img/x.jpg"}],
    }
    code, out = run(capsys, "shop", "rank", "--images")
    assert code == 0
    assert env.requests[0][2]["need_image"] == 1
    assert "image_url" in out.splitlines()[0]
    assert "https://img/x.jpg" in out


def test_creator_images(capsys, env):
    env.responses["/tiktok/creator/avatar-images"] = {
        "success": True,
        "data": {
            "images": {"111": "https://img/a.jpg", "222": "https://img/b.jpg"},
            "expires_at": 1786529453.177,
        },
    }
    code, out = run(capsys, "creator", "images", "111", "222")
    assert code == 0
    assert env.requests[0][0] == "/tiktok/creator/avatar-images"
    assert env.requests[0][2] == {"creator_ids": ["111", "222"]}
    assert "creator_images[2]{creator_id,image_url}:" in out
    assert "https://img/a.jpg" in out
    assert "expires_at" not in out
    assert "expire ~5 minutes" in out


def test_creator_images_batch_cap(capsys, env):
    code, out = run(capsys, "creator", "images", *[str(i) for i in range(101)])
    assert code != 0
    assert "at most 100 ids" in out


def test_creator_detail_by_handle(capsys, env):
    env.responses["/tiktok/creator/detailByHandle"] = {
        "success": True,
        "data": {"creator_id": "999", "creator_handle": "@_cakedfinds", "revenue": 1},
    }
    code, out = run(capsys, "creator", "detail", "@cakedfinds")
    assert code == 0
    path, _, body = env.requests[0]
    assert path == "/tiktok/creator/detailByHandle"
    assert body["creator_handle"] == "cakedfinds"  # leading @ stripped
    assert "fuzzy" in out


def test_video_live_keyword_passthrough(capsys, env):
    env.responses["/tiktok/video/rank"] = {"success": True, "data": []}
    env.responses["/tiktok/livestream/rank"] = {"success": True, "data": []}
    code, _ = run(capsys, "video", "rank", "--keyword", "perfume")
    assert code == 0
    assert env.requests[0][2]["keyword"] == "perfume"
    code, _ = run(capsys, "live", "rank", "--keyword", "perfume")
    assert code == 0
    assert env.requests[1][2]["keyword"] == "perfume"


def test_shop_keyword_omits_sort(capsys, env):
    env.responses["/tiktok/shop/rank"] = {"success": True, "data": []}
    code, _ = run(capsys, "shop", "rank", "--keyword", "anker")
    assert code == 0
    assert "sort_field" not in env.requests[0][2]


def test_json_escape_hatch(capsys, env):
    env.responses["/tiktok/shop/rank"] = {
        "success": True,
        "data": [{"shop_id": "9", "shop_name": "S", "revenue": 1}],
    }
    code, out = run(capsys, "shop", "rank", "--json")
    assert code == 0
    assert json.loads(out) == [{"shop_id": "9", "shop_name": "S", "revenue": 1}]


def test_fields_flag_extends_schema(capsys, env):
    env.responses["/tiktok/creator/rank"] = {
        "success": True,
        "data": [{"creator_id": "c1", "creator_handle": "h", "revenue": 1, "creator_followers": "1000", "live_revenue": 5}],
    }
    code, out = run(capsys, "creator", "rank", "--fields", "live_revenue")
    assert code == 0
    assert "live_revenue" in out.splitlines()[0]
    assert ",1000," in out.splitlines()[1]  # string number coerced


def test_fields_flag_rejects_unknown(capsys, env):
    code, out = run(capsys, "creator", "rank", "--fields", "nope")
    assert code == 2
    assert "unknown field 'nope'" in out


def test_default_subcommand_content_first(capsys, env):
    env.responses["/tiktok/product/rank"] = {"success": True, "data": []}
    code, _ = run(capsys, "product", "--region", "GB")
    assert code == 0
    assert env.requests[0][2]["region"] == "GB"


def test_config_set_and_show(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("KALODATA_API_KEY", raising=False)
    monkeypatch.setenv("KALODATA_CONFIG_DIR", str(tmp_path))
    code, out = run(capsys, "config", "set", "--key", "sk-abc123")
    assert code == 0
    code, out = run(capsys, "config")
    assert code == 0
    assert "…c123" in out or "c123" in out
    assert "sk-abc123" not in out  # masked


def test_home_lists_all_commands(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("KALODATA_CONFIG_DIR", str(tmp_path))
    code, out = run(capsys)
    assert code == 0
    assert "commands[22]" in out
    assert "kalo config" in out
    assert "kalo product comments <id...>" in out
