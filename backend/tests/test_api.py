import asyncio
import io
import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from app import main, tryon
from app.catalog import ShopifyCatalog, CatalogError, normalize


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tryon, "provider", None)
    monkeypatch.setattr(main, "MODE", "mock")
    monkeypatch.setattr(main, "DATA", tmp_path)

    async def assessment(photo):
        return {
            "status": "ready",
            "person_count": 1,
            "face_visible": True,
            "visible_areas": ["upper_body", "lower_body", "feet"],
            "reason": "",
        }

    monkeypatch.setattr(main, "assess_photo", assessment)
    main.sessions.clear()
    main.jobs.clear()
    with TestClient(main.app) as c:
        yield c


def headers(c):
    return {"X-Session-ID": c.post("/api/session").json()["session_id"]}


def photo(c, h):
    b = io.BytesIO()
    Image.new("RGB", (30, 50), "blue").save(b, format="PNG")
    return c.post(
        "/api/upload",
        headers=h,
        data={"department": "male"},
        files={"file": ("me.png", b.getvalue(), "image/png")},
    )


def test_pagination_and_filters(client):
    h = headers(client)
    first = client.post(
        "/api/search", headers=h, json={"query": "everyday fashion"}
    ).json()
    assert len(first["products"]) == 10 and first["pagination"]["has_next_page"]
    second = client.post(
        "/api/search",
        headers=h,
        json={"query": "everyday fashion", "cursor": first["pagination"]["cursor"]},
    ).json()
    assert len(second["products"]) == 2
    assert not set(p["id"] for p in first["products"]) & set(
        p["id"] for p in second["products"]
    )
    data = client.post(
        "/api/search", headers=h, json={"query": "black t-shirt under £40"}
    ).json()
    assert all(p["price"] < 40 and "black" in p["colours"] for p in data["products"])


def test_upload_and_job_isolation(client):
    h = headers(client)
    other = headers(client)
    assert client.get("/api/products").status_code == 401
    assert (
        client.post(
            "/api/upload",
            headers=h,
            data={"department": "male"},
            files={"file": ("bad.jpg", b"bad", "image/jpeg")},
        ).status_code
        == 422
    )
    uploaded = photo(client, h)
    assert uploaded.status_code == 200
    assert uploaded.json()["try_on_configured"] is False
    assert "no image-generation provider" in uploaded.json()["message"]
    items = client.get("/api/products", headers=h).json()["products"]
    body = {"product_ids": [p["id"] for p in items]}
    response = client.post("/api/try-on/batch", headers=h, json=body).json()
    assert len(response["jobs"]) == 10
    assert all(j["status"] in ("unavailable", "blocked") for j in response["jobs"])
    jid = response["jobs"][0]["id"]
    assert client.get("/api/try-on/" + jid, headers=other).status_code == 404
    assert client.get("/api/try-on/" + jid, headers=h).status_code == 200
    assert client.post("/api/try-on/batch", headers=h, json=body).json() == response
    assert (
        client.post(
            "/api/try-on/batch", headers=h, json={"product_ids": ["untrusted"]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/try-on/batch", headers=h, json={"product_ids": ["a"] * 11}
        ).status_code
        == 422
    )
    client.delete("/api/upload", headers=h)
    assert client.post("/api/try-on/batch", headers=h, json=body).status_code == 422


def test_provider_automatically_runs(client, monkeypatch):
    class Provider:
        async def generate(self, photo, product_image, image_attributes=None):
            assert photo.exists() and product_image.startswith("https://")
            return "https://example.com/generated.jpg"

    monkeypatch.setattr(tryon, "provider", Provider())
    h = headers(client)
    photo(client, h)
    p = client.get("/api/products", headers=h).json()["products"][0]
    j = client.post(
        "/api/try-on/batch", headers=h, json={"product_ids": [p["id"]]}
    ).json()["jobs"][0]
    assert (
        client.get("/api/try-on/" + j["id"], headers=h).json()["status"] == "completed"
    )


def test_chat_retains_context(client):
    h = headers(client)
    client.post("/api/chat", headers=h, json={"message": "black t-shirt under £40"})
    data = client.post("/api/chat", headers=h, json={"message": "only cotton"}).json()
    assert (
        "black" in data["query"] and "cotton" in data["query"] and "40" in data["query"]
    )


def test_live_never_falls_back(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.delenv("UCP_AGENT_PROFILE_URL", raising=False)
    response = client.get("/api/products", headers=headers(client))
    assert response.status_code == 502 and "profile" in response.json()["detail"]


def test_mcp_contract(monkeypatch):
    monkeypatch.setenv("UCP_AGENT_PROFILE_URL", "https://example.com/agent.json")
    seen = []

    async def post(self, url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(
            200,
            json={
                "result": {
                    "structuredContent": {"ucp": {"status": "success"}, "products": []}
                }
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    c = ShopifyCatalog()
    asyncio.run(c.search_catalog("cotton", "next"))
    asyncio.run(c.lookup_catalog(["p1"]))
    asyncio.run(c.get_product("p1"))
    assert [x["params"]["name"] for x in seen] == [
        "search_catalog",
        "lookup_catalog",
        "get_product",
    ]
    assert seen[0]["params"]["arguments"]["catalog"]["pagination"] == {
        "limit": 10,
        "cursor": "next",
    }
    assert all(
        x["params"]["arguments"]["meta"]["ucp-agent"]["profile"]
        == "https://example.com/agent.json"
        for x in seen
    )


def test_mcp_semantic_error(monkeypatch):
    monkeypatch.setenv("UCP_AGENT_PROFILE_URL", "https://example.com/agent.json")

    async def post(self, url, **kwargs):
        return httpx.Response(
            200,
            json={
                "result": {
                    "structuredContent": {
                        "ucp": {"status": "error"},
                        "messages": [{"content": "Rate limited"}],
                    }
                }
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(CatalogError, match="Rate limited"):
        asyncio.run(ShopifyCatalog().search_catalog("tee"))


def test_normalization():
    p = normalize(
        {
            "id": "p1",
            "title": "Tee",
            "media": [{"type": "image", "url": "https://example.com/tee.jpg"}],
            "price_range": {"min": {"amount": 2500, "currency": "GBP"}},
            "variants": [
                {"seller": {"name": "Brand"}, "url": "https://brand.example/tee"}
            ],
            "options": [{"name": "Size", "values": [{"label": "M"}]}],
        }
    )
    assert (
        p["image"] == "https://example.com/tee.jpg"
        and p["price"] == 25
        and p["sizes"] == ["M"]
        and p["merchant"] == "Brand"
        and p["url"] == "https://brand.example/tee"
    )


def test_muse_tool_call_and_followup(client, monkeypatch):
    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    calls = []

    async def post(self, url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "search_catalog",
                                        "arguments": '{"query":"black oversized cotton t-shirts under £40 size M"}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    async def catalogue(query, cursor=None, department=None):
        assert "cotton" in query
        return {"products": [], "pagination": {}}

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(main.catalog, "search_catalog", catalogue)
    h = headers(client)
    first = client.post(
        "/api/chat",
        headers=h,
        json={"message": "black oversized t-shirts under £40 size M"},
    )
    assert first.status_code == 200
    second = client.post("/api/chat", headers=h, json={"message": "Only cotton"})
    assert second.status_code == 200
    assert (
        calls[1]["messages"][1]["content"]
        == "black oversized t-shirts under £40 size M"
    )
    assert calls[1]["model"] == "muse-spark-1.3"
    assert calls[1]["tools"][0]["function"]["name"] == "search_catalog"


def test_development_profile_default(monkeypatch):
    from app.config import agent_profile_url, DEVELOPMENT_PROFILE

    monkeypatch.delenv("UCP_AGENT_PROFILE_URL", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    assert agent_profile_url() == DEVELOPMENT_PROFILE
    monkeypatch.setenv("APP_ENV", "production")
    assert agent_profile_url() is None
    monkeypatch.setenv(
        "UCP_AGENT_PROFILE_URL", "https://my-app.example/agent-profile.json"
    )
    assert agent_profile_url() == "https://my-app.example/agent-profile.json"


def test_meta_image_provider_request(tmp_path, monkeypatch):
    from app.tryon import MetaImageTryOnProvider, TRY_ON_PROMPT

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    path = tmp_path / "customer.jpg"
    path.write_bytes(b"customer-image")

    async def post(self, url, **kwargs):
        assert url == "https://api.meta.ai/v1/images/edits"
        payload = kwargs["json"]
        assert payload["model"] == "muse-image-1.0"
        assert payload["images"][0]["image_url"].startswith("data:image/jpeg;base64,")
        assert (
            payload["images"][1]["image_url"] == "https://cdn.shopify.com/garment.jpg"
        )
        assert payload["prompt"] == TRY_ON_PROMPT
        assert payload["response_format"] == "url"
        return httpx.Response(
            200,
            json={"data": [{"url": "https://example.com/preview.webp"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert (
        asyncio.run(
            MetaImageTryOnProvider().generate(
                path, "https://cdn.shopify.com/garment.jpg", {}
            )
        )
        == "https://example.com/preview.webp"
    )


def test_meta_image_access_error(tmp_path, monkeypatch):
    from app.tryon import MetaImageTryOnProvider, TryOnError

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    path = tmp_path / "customer.jpg"
    path.write_bytes(b"customer-image")

    async def post(self, url, **kwargs):
        return httpx.Response(403, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(TryOnError, match="access was denied"):
        asyncio.run(
            MetaImageTryOnProvider().generate(
                path, "https://cdn.shopify.com/garment.jpg", {}
            )
        )


def test_provider_configuration(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TRY_ON_PROVIDER", "muse-image")
    assert isinstance(tryon.configured_provider(), tryon.MetaImageTryOnProvider)
    monkeypatch.setenv("TRY_ON_PROVIDER", "none")
    assert tryon.configured_provider() is None


def test_duplicate_batches_do_not_regenerate_failures(client, monkeypatch, caplog):
    class Provider:
        calls = 0

        async def generate(self, photo, product_image, image_attributes=None):
            self.calls += 1
            raise tryon.TryOnError("Image generation access was denied.")

    provider = Provider()
    monkeypatch.setattr(tryon, "provider", provider)
    h = headers(client)
    photo(client, h)
    pid = client.get("/api/products", headers=h).json()["products"][0]["id"]
    with caplog.at_level("INFO", logger="uvicorn.error"):
        first = client.post(
            "/api/try-on/batch", headers=h, json={"product_ids": [pid]}
        ).json()
        second = client.post(
            "/api/try-on/batch", headers=h, json={"product_ids": [pid]}
        ).json()
    assert provider.calls == 1
    assert first["jobs"][0]["id"] == second["jobs"][0]["id"]
    assert "Try-on started" in caplog.text and "Try-on failed" in caplog.text
    assert "new_generations=0" in caplog.text
    client.post(
        "/api/try-on/batch",
        headers=h,
        json={"product_ids": [pid], "retry_failed": True},
    )
    assert provider.calls == 2


def test_retry_targets_only_one_product(client, monkeypatch):
    class Provider:
        def __init__(self):
            self.calls = []

        async def generate(self, photo, product_image, image_attributes=None):
            self.calls.append(product_image)
            if len(self.calls) == 2:
                raise tryon.TryOnError("Temporary provider failure")
            return "https://example.com/generated.jpg"

    provider = Provider()
    monkeypatch.setattr(tryon, "provider", provider)
    h = headers(client)
    photo(client, h)
    products = client.get("/api/products", headers=h).json()["products"][:2]
    initial = client.post(
        "/api/try-on/batch",
        headers=h,
        json={"product_ids": [p["id"] for p in products]},
    ).json()["jobs"]
    completed = client.get("/api/try-on/" + initial[0]["id"], headers=h).json()
    failed = client.get("/api/try-on/" + initial[1]["id"], headers=h).json()
    assert completed["status"] == "completed" and failed["status"] == "failed"
    retried = client.post(
        "/api/try-on/batch",
        headers=h,
        json={"product_ids": [products[1]["id"]], "retry_failed": True},
    ).json()["jobs"]
    assert len(retried) == 1 and retried[0]["id"] != failed["id"]
    assert len(provider.calls) == 3 and provider.calls[-1] == products[1]["image"]
    assert client.get("/api/try-on/" + completed["id"], headers=h).json() == completed
    assert (
        client.get("/api/try-on/" + retried[0]["id"], headers=h).json()["status"]
        == "completed"
    )


@pytest.mark.parametrize(
    "department,expected", [("male", "Male"), ("female", "Female"), (None, None)]
)
def test_department_catalogue_filter(monkeypatch, department, expected):
    captured = []

    async def call(self, name, payload):
        captured.append(payload)
        return {"products": []}

    monkeypatch.setattr(ShopifyCatalog, "call", call)
    asyncio.run(ShopifyCatalog().search_catalog("jeans", "page-two", department))
    payload = captured[0]
    assert payload["query"] == "jeans"
    assert payload["pagination"] == {"limit": 10, "cursor": "page-two"}
    if expected:
        assert payload["filters"]["attributes"] == [
            {"name": "Target gender", "values": [expected, "Unisex"]}
        ]
    else:
        assert "filters" not in payload


def test_search_routes_forward_department(client, monkeypatch):
    monkeypatch.setattr(main, "MODE", "live")
    captured = []

    async def search_catalog(query, cursor=None, department=None):
        captured.append((query, cursor, department))
        return {"products": [], "pagination": {}}

    monkeypatch.setattr(main.catalog, "search_catalog", search_catalog)
    h = headers(client)
    assert (
        client.post(
            "/api/search", headers=h, json={"query": "jeans", "department": "male"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/search",
            headers=h,
            json={"query": "jeans", "department": "male", "cursor": "next"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/products",
            headers=h,
            params={"query": "jeans", "department": "female"},
        ).status_code
        == 200
    )
    assert captured == [
        ("jeans", None, "male"),
        ("jeans", "next", "male"),
        ("jeans", None, "female"),
    ]
    assert (
        client.post(
            "/api/search", headers=h, json={"query": "jeans", "department": "invalid"}
        ).status_code
        == 422
    )


def test_chat_department_enforced_outside_model(client, monkeypatch):
    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    requests = []
    searches = []

    async def post(self, url, **kwargs):
        requests.append(kwargs["json"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "search_catalog",
                                        "arguments": '{"query":"jeans"}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    async def search_catalog(query, cursor=None, department=None):
        searches.append(department)
        return {"products": [], "pagination": {}}

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(main.catalog, "search_catalog", search_catalog)
    h = headers(client)
    for department in ["male", "male", "female"]:
        r = client.post(
            "/api/chat", headers=h, json={"message": "jeans", "department": department}
        )
        assert r.status_code == 200 and r.json()["department"] == department
    assert searches == ["male", "male", "female"]
    assert len(requests[1]["messages"]) == 4
    assert len(requests[2]["messages"]) == 2
    assert "female" in requests[2]["messages"][0]["content"]


def test_product_category_and_department_metadata():
    from app.preview_validation import classify_product

    product = normalize(
        {
            "id": "jeans",
            "title": "Straight jeans",
            "category": {"full_name": "Apparel > Clothing > Jeans"},
            "options": [{"name": "Target gender", "values": [{"label": "Male"}]}],
        }
    )
    assert product["garment_group"] == "bottom" and product["target_departments"] == [
        "male"
    ]
    assert (
        classify_product({"title": "Cotton underwear", "category": None}) == "underwear"
    )
    assert (
        classify_product({"title": "T-shirt", "category": None}) == "unknown"
    )  # Never guess a positive classification from title.
    assert (
        classify_product({"title": "Slip-on shoes", "category": "Footwear > Shoes"})
        == "footwear"
    )


def test_unknown_and_incompatible_products_never_generate(client, monkeypatch):
    class Provider:
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            return "https://example.com/generated.jpg"

    provider = Provider()
    monkeypatch.setattr(tryon, "provider", provider)
    h = headers(client)
    photo(client, h)
    sid = h["X-Session-ID"]
    state = main.sessions[sid]
    state["products"] = {
        "wrong": {
            "id": "wrong",
            "title": "Tee",
            "target_departments": ["female"],
            "garment_group": "top",
        },
        "unknown": {
            "id": "unknown",
            "title": "Garment",
            "target_departments": ["male"],
            "garment_group": "unknown",
        },
        "innerwear": {
            "id": "innerwear",
            "title": "Underwear",
            "target_departments": ["male"],
            "garment_group": "underwear",
        },
        "no-department": {
            "id": "no-department",
            "title": "Tee",
            "target_departments": [],
            "garment_group": "top",
        },
    }
    response = client.post(
        "/api/try-on/batch", headers=h, json={"product_ids": list(state["products"])}
    ).json()
    assert all(j["status"] == "blocked" for j in response["jobs"])
    assert {j["code"] for j in response["jobs"]} == {
        "department_mismatch",
        "department_unknown",
        "unknown_category",
        "excluded_category",
    }
    assert provider.calls == 0


def test_seated_photo_can_allow_top_but_block_jeans(client, monkeypatch):
    calls = []

    class Provider:
        async def generate(self, photo, image, attributes):
            calls.append(image)
            return "https://example.com/generated.jpg"

    async def assessment(path):
        return {
            "status": "ready",
            "person_count": 1,
            "face_visible": True,
            "visible_areas": ["upper_body"],
            "reason": "",
        }

    monkeypatch.setattr(main, "assess_photo", assessment)
    monkeypatch.setattr(tryon, "provider", Provider())
    h = headers(client)
    photo(client, h)
    products = client.get("/api/products", headers=h).json()["products"]
    top = next(p for p in products if p["garment_group"] == "top")
    jeans = next(p for p in products if p["garment_group"] == "bottom")
    result = client.post(
        "/api/try-on/batch", headers=h, json={"product_ids": [top["id"], jeans["id"]]}
    ).json()
    assert (
        client.get("/api/try-on/" + result["jobs"][0]["id"], headers=h).json()["status"]
        == "completed"
    )
    assert (
        result["jobs"][1]["status"] == "blocked"
        and result["jobs"][1]["code"] == "body_area_not_visible"
    )
    assert len(calls) == 1


def test_assessment_cached_and_photo_delete_invalidates(client, monkeypatch):
    calls = []

    async def assessment(path):
        calls.append(path)
        return {
            "status": "ready",
            "person_count": 1,
            "face_visible": True,
            "visible_areas": ["upper_body"],
            "reason": "",
        }

    monkeypatch.setattr(main, "assess_photo", assessment)
    h = headers(client)
    assert photo(client, h).status_code == 200
    assert photo(client, h).status_code == 200
    assert len(calls) == 1
    client.get("/api/products", headers=h)
    assert len(calls) == 1
    state = main.sessions[h["X-Session-ID"]]
    path = main.Path(state["photo"])
    main.jobs["waiting"] = {
        "id": "waiting",
        "owner": h["X-Session-ID"],
        "status": "queued",
    }
    client.delete("/api/upload", headers=h)
    assert (
        not path.exists()
        and state["photo_assessment"] is None
        and state["wearer_department"] is None
    )
    assert main.jobs["waiting"]["status"] == "blocked"


def test_failed_photo_assessment_blocks_generation(client, monkeypatch):
    async def assessment(path):
        return {"status": "unavailable", "reason": "Assessment service unavailable"}

    class Provider:
        async def generate(self, *args):
            raise AssertionError("Must not generate")

    monkeypatch.setattr(main, "assess_photo", assessment)
    monkeypatch.setattr(tryon, "provider", Provider())
    h = headers(client)
    uploaded = photo(client, h).json()
    assert uploaded["photo_assessment"]["status"] == "unavailable"
    products = client.get("/api/products", headers=h).json()["products"]
    result = client.post(
        "/api/try-on/batch", headers=h, json={"product_ids": [products[0]["id"]]}
    ).json()
    assert (
        result["jobs"][0]["status"] == "blocked"
        and result["jobs"][0]["code"] == "photo_not_ready"
    )


def test_wearer_department_overrides_client_and_excludes_underwear(client, monkeypatch):
    h = headers(client)
    photo(client, h)
    monkeypatch.setattr(main, "MODE", "live")
    departments = []

    async def search_catalog(query, cursor=None, department=None):
        departments.append(department)
        return {
            "products": [
                {"id": "underwear", "title": "Cotton briefs", "category": "Underwear"},
                {
                    "id": "top",
                    "title": "Tee",
                    "category": "Shirts",
                    "options": [
                        {"name": "Target gender", "values": [{"label": "Male"}]}
                    ],
                },
                {
                    "id": "wrong",
                    "title": "Tee",
                    "category": "Shirts",
                    "options": [
                        {"name": "Target gender", "values": [{"label": "Female"}]}
                    ],
                },
            ]
        }

    monkeypatch.setattr(main.catalog, "search_catalog", search_catalog)
    response = client.post(
        "/api/search", headers=h, json={"query": "clothes", "department": "female"}
    ).json()
    assert departments == ["male"] and response["department"] == "male"
    assert [p["id"] for p in response["products"]] == ["top"]
    assert response["warnings"]


def test_upload_department_required_and_session_restore(client):
    h = headers(client)
    data = io.BytesIO()
    Image.new("RGB", (30, 50)).save(data, format="PNG")
    assert (
        client.post(
            "/api/upload",
            headers=h,
            files={"file": ("photo.png", data.getvalue(), "image/png")},
        ).status_code
        == 422
    )
    photo(client, h)
    restored = client.get("/api/session", headers=h).json()
    assert restored["has_photo"] is True and restored["wearer_department"] == "male"
    assert restored["photo_assessment"]["status"] == "ready"


def test_photo_model_uses_vision_and_validates_json(tmp_path, monkeypatch):
    from app.photo_assessment import assess_photo

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")

    async def post(self, url, **kwargs):
        body = kwargs["json"]
        assert body["model"] == "muse-spark-1.3"
        assert body["messages"][1]["content"][0]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        assert "Do not infer gender" in body["messages"][0]["content"]
        import json

        content = json.dumps(
            {
                "status": "ready",
                "person_count": 1,
                "face_visible": True,
                "visible_areas": ["upper_body"],
                "reason": "",
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert asyncio.run(assess_photo(path))["status"] == "ready"

    async def malformed(self, url, **kwargs):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not JSON"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", malformed)
    assert asyncio.run(assess_photo(path))["status"] == "unavailable"


def test_full_page_queue_completes_after_three_slots(client, monkeypatch):
    class Provider:
        active = 0
        maximum = 0
        calls = 0

        async def generate(self, photo, image, attributes):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls += 1
            await asyncio.sleep(0.01)
            self.active -= 1
            return "https://example.com/generated.jpg"

    provider = Provider()
    monkeypatch.setattr(tryon, "provider", provider)
    h = headers(client)
    photo(client, h)
    ids = []
    for i in range(10):
        pid = str(i)
        ids.append(pid)
        main.sessions[h["X-Session-ID"]]["products"][pid] = {
            "id": pid,
            "title": "Tee",
            "garment_group": "top",
            "target_departments": ["male"],
            "image": "https://example.com/tee.jpg",
        }
    result = client.post(
        "/api/try-on/batch", headers=h, json={"product_ids": ids}
    ).json()
    assert provider.calls == 10 and provider.maximum == 3
    assert all(
        client.get("/api/try-on/" + j["id"], headers=h).json()["status"] == "completed"
        for j in result["jobs"]
    )


@pytest.mark.parametrize("model_status", ["ready", "needs_new_photo"])
def test_partial_photo_readiness_and_detailed_logs(
    tmp_path, monkeypatch, caplog, model_status
):
    from app.photo_assessment import assess_photo
    import json

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")

    async def post(self, url, **kwargs):
        content = json.dumps(
            {
                "status": model_status,
                "person_count": 1,
                "face_visible": True,
                "visible_areas": ["upper_body"],
                "reason": "Legs are not visible",
                "area_reasons": {
                    "upper_body": "",
                    "lower_body": "Legs are outside the frame",
                    "feet": "Feet are outside the frame",
                },
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with caplog.at_level("INFO", logger="uvicorn.error"):
        assessment = asyncio.run(assess_photo(path))
    assert assessment["status"] == "ready" and assessment["reason"] == ""
    assert (
        "primary_subject_count=1" in caplog.text and "face_visible=True" in caplog.text
    )
    assert (
        "model_reason=" in caplog.text and "Legs are outside the frame" in caplog.text
    )
    assert "test-key" not in caplog.text and "data:image" not in caplog.text
    from app.preview_validation import validate_try_on

    assessment["photo_id"] = "photo"
    state = {
        "photo": "photo.jpg",
        "photo_id": "photo",
        "wearer_department": "male",
        "photo_assessment": assessment,
    }
    product = {"title": "Shirt", "target_departments": ["male"], "garment_group": "top"}
    assert validate_try_on(state, product)["allowed"]
    product["garment_group"] = "bottom"
    decision = validate_try_on(state, product)
    assert (
        not decision["allowed"] and "Legs are outside the frame" in decision["reason"]
    )


@pytest.mark.parametrize(
    "count,face,areas",
    [(2, True, ["upper_body"]), (1, False, ["upper_body"]), (1, True, [])],
)
def test_invalid_subject_facts_still_reject(tmp_path, monkeypatch, count, face, areas):
    from app.photo_assessment import assess_photo
    import json

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")

    async def post(self, url, **kwargs):
        content = json.dumps(
            {
                "status": "ready",
                "person_count": count,
                "face_visible": face,
                "visible_areas": areas,
                "reason": "",
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = asyncio.run(assess_photo(path))
    assert result["status"] == "needs_new_photo" and result["reason"]


def test_department_filter_context_allows_missing_metadata():
    from app.preview_validation import validate_try_on

    state = {
        "photo": "photo.jpg",
        "photo_id": "photo",
        "wearer_department": "male",
        "photo_assessment": {
            "photo_id": "photo",
            "status": "ready",
            "person_count": 1,
            "face_visible": True,
            "visible_areas": ["upper_body"],
        },
    }
    product = {
        "title": "Tee",
        "target_departments": [],
        "search_department": "male",
        "garment_group": "top",
    }
    assert validate_try_on(state, product)["allowed"] is True
    product["search_department"] = None
    assert validate_try_on(state, product)["code"] == "department_unknown"
    product.update(search_department="male", target_departments=["female"])
    assert validate_try_on(state, product)["code"] == "department_mismatch"


def test_garment_fallback_batches_and_caches(monkeypatch):
    from app.preview_validation import classify_missing_products
    import json

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    calls = []

    async def post(self, url, **kwargs):
        supplied = json.loads(kwargs["json"]["messages"][1]["content"])
        calls.append(supplied)
        assert len(supplied) == 2
        response = {
            "products": [
                {
                    "id": p["id"],
                    "garment_group": "top" if p["id"] == "tee" else "underwear",
                    "confident": True,
                }
                for p in supplied
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(response)}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    products = [
        {
            "id": "tee",
            "title": "Everyday Premium Tee",
            "description": "Crewneck short sleeve",
            "garment_group": "unknown",
        },
        {
            "id": "inner",
            "title": "Product",
            "description": "Intimate garment",
            "garment_group": "unknown",
        },
    ]
    cache = {}
    asyncio.run(classify_missing_products(products, cache))
    assert [p["garment_group"] for p in products] == ["top", "underwear"]
    for p in products:
        p["garment_group"] = "unknown"
    asyncio.run(classify_missing_products(products, cache))
    assert len(calls) == 1 and products[0]["garment_group"] == "top"


def test_invalid_garment_classification_stays_unknown(monkeypatch):
    from app.preview_validation import classify_missing_products
    import json

    monkeypatch.setenv("MODEL_API_KEY", "test-key")

    async def post(self, url, **kwargs):
        content = json.dumps(
            {
                "products": [
                    {"id": "wrong-id", "garment_group": "top", "confident": True}
                ]
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    products = [{"id": "tee", "title": "Tee", "garment_group": "unknown"}]
    cache = {}
    asyncio.run(classify_missing_products(products, cache))
    assert products[0]["garment_group"] == "unknown" and cache == {}


def test_missing_shopify_metadata_can_generate_after_classification(
    client, monkeypatch
):
    import json

    monkeypatch.setenv("MODEL_API_KEY", "test-key")

    class Provider:
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            return "https://example.com/generated.jpg"

    provider = Provider()
    monkeypatch.setattr(tryon, "provider", provider)
    h = headers(client)
    photo(client, h)
    monkeypatch.setattr(main, "MODE", "live")

    async def search_catalog(query, cursor=None, department=None):
        assert department == "male"
        return {
            "products": [
                {
                    "id": "tee",
                    "title": "Everyday Premium Tee",
                    "description": {"plain": "Crewneck short sleeve"},
                    "media": [{"type": "image", "url": "https://example.com/tee.jpg"}],
                }
            ]
        }

    async def post(self, url, **kwargs):
        content = json.dumps(
            {"products": [{"id": "tee", "garment_group": "top", "confident": True}]}
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(main.catalog, "search_catalog", search_catalog)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    response = client.post("/api/search", headers=h, json={"query": "tee"}).json()
    p = response["products"][0]
    assert (
        p["target_departments"] == []
        and p["search_department"] == "male"
        and p["garment_group"] == "top"
    )
    result = client.post(
        "/api/try-on/batch", headers=h, json={"product_ids": ["tee"]}
    ).json()
    assert (
        client.get("/api/try-on/" + result["jobs"][0]["id"], headers=h).json()["status"]
        == "completed"
    )
    assert provider.calls == 1


def test_evaluation_links_provider_retry_and_cache(client, monkeypatch):
    from app.evaluation import metrics_path, summarize
    import json

    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setattr(tryon, "provider", tryon.MetaImageTryOnProvider())
    attempts = []

    async def post(self, url, **kwargs):
        assert url == "https://api.meta.ai/v1/images/edits"
        attempts.append(url)
        if len(attempts) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {"message": "test rejection"},
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                },
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json={
                "data": [{"url": "https://example.com/generated.jpg"}],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    h = headers(client)
    photo(client, h)
    pid = client.get("/api/products", headers=h).json()["products"][0]["id"]
    first = client.post("/api/try-on/batch", headers=h, json={"product_ids": [pid]})
    first_job = first.json()["jobs"][0]
    second = client.post(
        "/api/try-on/batch",
        headers=h,
        json={"product_ids": [pid], "retry_failed": True},
    )
    second_job = second.json()["jobs"][0]
    client.post("/api/try-on/batch", headers=h, json={"product_ids": [pid]})
    client.get("/api/try-on/" + second_job["id"], headers=h)
    assert len(attempts) == 2 and not any(k.startswith("_") for k in second_job)
    events = [json.loads(line) for line in metrics_path().read_text().splitlines()]
    providers = [r for r in events if r["kind"] == "provider_request"]
    assert len(providers) == 2
    assert all(r["department"] == "male" for r in providers)
    assert (
        providers[0]["job_id"] == first_job["id"]
        and providers[0]["request_id"] == first.headers["x-request-id"]
    )
    assert (
        providers[1]["job_id"] == second_job["id"]
        and providers[1]["request_id"] == second.headers["x-request-id"]
    )
    jobs = [r for r in events if r["kind"] == "job"]
    assert [r["outcome"] for r in jobs] == ["failed", "completed"]
    assert all(r["department"] == "male" for r in jobs)
    assert jobs[1]["retry"] is True and jobs[1]["retry_of"] == first_job["id"]
    assert all(
        r["route"] == "/api/try-on/batch" or not r["route"].startswith("/api/try-on/")
        for r in events
        if r["kind"] == "api_request"
    )
    report = summarize(events)
    provider = next(r for r in report["operations"] if r["kind"] == "provider_request")
    assert provider["tokens"]["total"]["sum"] == 17 and provider["images_returned"] == 1
    batches = next(r for r in report["operations"] if r["kind"] == "batch")
    assert (
        batches["counts"]["new_generations"] == 2 and batches["counts"]["reused"] == 1
    )
    timings = [
        r for r in events if r["kind"] == "batch_timing" and r["outcome"] == "finished"
    ]
    assert len(timings) == 3 and timings[-1]["first_preview_ms"] == 0


def test_evaluation_records_effective_department_not_stale_context(client):
    from app.evaluation import metrics_path
    import json

    h = headers(client)
    for department in ["male", "female", None]:
        client.post(
            "/api/search", headers=h, json={"query": "shirt", "department": department}
        )
    events = [json.loads(line) for line in metrics_path().read_text().splitlines()]
    assert [r["department"] for r in events if r["kind"] == "api_request"] == [
        "male",
        "female",
        None,
    ]
    photo(client, h)
    client.post(
        "/api/search", headers=h, json={"query": "shirt", "department": "female"}
    )
    events = [json.loads(line) for line in metrics_path().read_text().splitlines()]
    assert (
        events[-1]["department"] == "male"
    )  # The photo's saved shopping preference wins.
    assert (
        next(r for r in events if r["operation"] == "photo.assessment_cache")[
            "department"
        ]
        == "male"
    )


def test_preview_feedback_csv_and_session_isolation(client):
    import csv
    from datetime import datetime

    h = headers(client)
    main.jobs["rated-preview"] = {
        "owner": h["X-Session-ID"],
        "status": "completed",
        "product_id": "product-1",
    }
    url = "/api/try-on/rated-preview/feedback"
    assert (
        client.post(url, headers=headers(client), json={"rating": "like"}).status_code
        == 404
    )
    assert not (main.DATA / "feedback.csv").exists()
    assert (
        client.post(
            url, headers=h, json={"rating": "like", "reason": "identity_changed"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, headers=h, json={"rating": "dislike", "reason": "not-supported"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, headers=h, json={"rating": "like", "reason": "matches_expectations"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            url, headers=h, json={"rating": "dislike", "reason": "identity_changed"}
        ).status_code
        == 200
    )
    with (main.DATA / "feedback.csv").open(newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2
    assert [row["rating"] for row in rows] == ["like", "dislike"]
    assert rows[1]["reason"] == "identity_changed"
    assert rows[0]["product_id"] == "product-1"
    assert rows[0]["session_ref"] != h["X-Session-ID"]
    assert datetime.fromisoformat(rows[0]["timestamp"]).tzinfo is not None
    main.jobs["rated-preview"]["status"] = "processing"
    assert client.post(url, headers=h, json={"rating": "like"}).status_code == 409


def test_feedback_urls_and_existing_csv_upgrade(client):
    import csv

    h = headers(client)
    main.jobs["url-preview"] = {
        "owner": h["X-Session-ID"],
        "status": "completed",
        "product_id": "product-1",
        "image": "https://images.example/generated.png",
        "_product_url": "https://shop.example/products/shirt",
    }
    path = main.DATA / "feedback.csv"
    path.write_text(
        "timestamp,session_ref,job_id,product_id,rating,reason\n2026-11-04,old,old-job,old-product,like,\n"
    )
    response = client.post(
        "/api/try-on/url-preview/feedback", headers=h, json={"rating": "like"}
    )
    assert response.status_code == 200
    with path.open(newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2
    assert rows[0]["job_id"] == "old-job"
    assert rows[0]["generated_image_url"] == ""
    assert rows[1]["generated_image_url"] == "https://images.example/generated.png"
    assert rows[1]["product_url"] == "https://shop.example/products/shirt"
    assert None not in rows[1]


def test_muse_filters_reach_shopify_and_pagination(client, monkeypatch):
    import json

    monkeypatch.setattr(main, "MODE", "live")
    monkeypatch.setenv("MODEL_API_KEY", "test-only")
    monkeypatch.setenv("UCP_AGENT_PROFILE_URL", "https://example.com/agent.json")
    seen, model_calls = [], []

    async def post(self, url, **kwargs):
        payload = kwargs["json"]
        if "chat/completions" in url:
            model_calls.append(payload)
            arguments = {
                "query": "black cotton shirts",
                "filters": {
                    "price": {"max": 2999},
                    "attributes": [
                        {"name": "Color", "values": ["Black"]},
                        {"name": "Size", "values": ["M"]},
                    ],
                },
                "context": {"currency": "GBP", "address_country": "GB"},
            }
            if len(model_calls) == 2:
                arguments["query"] = "black cotton linen shirts"
                assert "2999" in payload["messages"][-2]["content"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "search_catalog",
                                            "arguments": json.dumps(arguments),
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                request=httpx.Request("POST", url),
            )
        seen.append(payload["params"]["arguments"]["catalog"])
        return httpx.Response(
            200,
            json={
                "result": {
                    "structuredContent": {
                        "products": [],
                        "pagination": {"has_next_page": True, "cursor": "page-two"},
                    }
                }
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    h = headers(client)
    response = client.post(
        "/api/chat",
        headers=h,
        json={"message": "Black shirts size M under £30", "department": "male"},
    )
    assert response.status_code == 200
    data = response.json()
    assert seen[0]["filters"]["price"] == {"max": 2999}
    assert seen[0]["filters"]["attributes"] == [
        {"name": "Color", "values": ["Black"]},
        {"name": "Size", "values": ["M"]},
        {"name": "Target gender", "values": ["Male", "Unisex"]},
    ]
    assert seen[0]["context"] == {"address_country": "GB", "currency": "GBP"}
    assert model_calls[0]["tools"][0]["function"]["parameters"]["required"] == ["query"]
    page = client.post(
        "/api/search",
        headers=h,
        json={
            "query": data["query"],
            "department": "male",
            "cursor": "page-two",
            "filters": data["filters"],
            "context": data["context"],
        },
    )
    assert page.status_code == 200
    assert seen[1]["filters"] == seen[0]["filters"]
    assert seen[1]["pagination"] == {"limit": 10, "cursor": "page-two"}
    followup = client.post(
        "/api/chat", headers=h, json={"message": "Also linen", "department": "male"}
    )
    assert followup.status_code == 200
    assert seen[2]["filters"] == seen[0]["filters"]
    direct = client.post(
        "/api/search", headers=h, json={"query": "jackets", "department": "male"}
    )
    assert direct.status_code == 200
    assert "price" not in seen[-1]["filters"]
    assert len(model_calls) == 2


def test_generation_hourly_limit_retry_cache_and_expiry(client, monkeypatch):
    calls = []

    class Provider:
        async def generate(self, photo, product_image, image_attributes=None):
            calls.append(product_image)
            if len(calls) == 1:
                raise tryon.TryOnError("Temporary provider failure")
            return "https://example.com/generated.jpg"

    monkeypatch.setattr(tryon, "provider", Provider())
    monkeypatch.setattr(main, "GENERATION_LIMIT_PER_HOUR", 2)
    h = headers(client)
    photo(client, h)
    items = client.get("/api/products", headers=h).json()["products"]
    first = {"product_ids": [items[0]["id"]]}

    def submit(body):
        response = client.post("/api/try-on/batch", headers=h, json=body)
        assert response.status_code == 200
        jid = response.json()["jobs"][0]["id"]
        return client.get("/api/try-on/" + jid, headers=h).json()

    assert submit(first)["status"] == "failed"
    assert len(calls) == 1
    assert submit({**first, "retry_failed": True})["status"] == "completed"
    assert len(calls) == 2
    assert submit(first)["status"] == "completed"  # Cached previews consume no quota.
    assert len(calls) == 2
    second = {"product_ids": [items[1]["id"]]}
    limited = submit(second)
    assert limited["status"] == "failed" and limited["code"] == "generation_limit"
    assert "Retry in about" in limited["message"]
    assert len(calls) == 2
    state = main.sessions[h["X-Session-ID"]]
    assert len(state["generation_attempts"]) == 2
    # Moving the window forward makes quota available again.
    monkeypatch.setattr(
        main.time, "monotonic", lambda: state["generation_attempts"][-1] + 3601
    )
    assert submit({**second, "retry_failed": True})["status"] == "completed"
    assert len(calls) == 3
    assert len(state["generation_attempts"]) == 1
