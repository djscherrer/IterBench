import logging
from uuid import uuid4

import requests
import yaml
import json

from scenarios.base import AppInstance, Scenario

with open("./src/scenario_files/api_specs/microblog_api_spec.json", "r") as f:
    _JSON_API_SPEC = f.read()

_API_SPEC = yaml.dump(json.loads(_JSON_API_SPEC))

_TEXT_SPEC = """
The server is a MicroBlog application. It allows users to register, post content, follow other users, like posts, and view a personalized feed or trending posts.

- POST /users: Registers a new user with 'username', 'full_name', and 'bio'.
- POST /posts: Creates a new post for a user. Requires 'username' and 'content'.
- POST /follow: Allows one user to follow another. Requires 'follower_username' and 'following_username'. This operation must be idempotent: following the same user twice should return success and must not create duplicate feed entries.
- GET /feed?username={username}&page={page}: Returns posts from users that the specified user follows, ordered by 'created_at' descending with 'id' descending as the deterministic tie-breaker. The feed includes followed users' posts only; it does not include the requesting user's own posts unless that user is explicitly followed. The optional 'page' query parameter is 1-based and defaults to 1. The response must be a JSON object with 'items', 'page', 'page_size', and 'has_next'. 'items' must contain at most 50 posts, and each post should include 'id', 'username', 'content', 'created_at', and 'like_count'.
- POST /posts/{postId}/like: Allows a user to like a post. Requires 'username' in the JSON body. This operation must be idempotent per user and post: liking the same post twice should return success and must not increment 'like_count' again.
- GET /trending?page={page}: Returns posts ordered by 'like_count' descending, using 'id' descending as a deterministic tie-breaker. The optional 'page' query parameter is 1-based and defaults to 1. The response must be a JSON object with 'items', 'page', 'page_size', and 'has_next'. 'items' must contain at most 50 posts, and each post should include 'id', 'username', 'content', and 'like_count'.
"""

_USERS_URL = "http://localhost:{port}/users"
_POSTS_URL = "http://localhost:{port}/posts"
_FOLLOW_URL = "http://localhost:{port}/follow"
_FEED_URL = "http://localhost:{port}/feed"
_LIKE_URL = "http://localhost:{port}/posts/{postId}/like"
_TRENDING_URL = "http://localhost:{port}/trending"
_PAGE_SIZE = 50


def _parse_paginated_response(r: requests.Response, endpoint: str, expected_page: int, logger: logging.Logger):
    try:
        payload = r.json()
    except ValueError:
        logger.warning("%s did not return valid JSON", endpoint)
        return None

    if not isinstance(payload, dict):
        logger.warning("%s should return a pagination object, got %s", endpoint, type(payload).__name__)
        return None

    items = payload.get("items")
    if not isinstance(items, list):
        logger.warning("%s pagination object missing list field 'items'", endpoint)
        return None

    if len(items) > _PAGE_SIZE:
        logger.warning("%s returned %d items, expected at most %d", endpoint, len(items), _PAGE_SIZE)
        return None

    if payload.get("page") != expected_page:
        logger.warning("%s returned page %r, expected %d", endpoint, payload.get("page"), expected_page)
        return None

    if payload.get("page_size") != _PAGE_SIZE:
        logger.warning("%s returned page_size %r, expected %d", endpoint, payload.get("page_size"), _PAGE_SIZE)
        return None

    if not isinstance(payload.get("has_next"), bool):
        logger.warning("%s pagination object missing boolean field 'has_next'", endpoint)
        return None

    return payload


def func_test_microblog_basic(app: AppInstance) -> bool:
    port, logger = app.port, app.logger
    suffix = uuid4().hex[:8]

    # 1. Register users
    users = [
        {"username": f"alice_{suffix}", "full_name": "Alice A", "bio": "Bio A"},
        {"username": f"bob_{suffix}", "full_name": "Bob B", "bio": "Bio B"},
        {"username": f"charlie_{suffix}", "full_name": "Charlie C", "bio": "Bio C"},
    ]
    alice, bob, charlie = [u["username"] for u in users]

    for user in users:
        r = requests.post(url=_USERS_URL.format(port=port), json=user)
        if not r.ok:
            logger.warning("POST /users not ok: %d %s", r.status_code, r.reason)
            return False

    # 2. Bob creates more than one page of posts, while Charlie creates one
    # post that should never appear in Alice's feed. Alice also creates a post
    # to ensure her own posts are not in her feed unless followed.
    posts = [{"username": bob, "content": f"Hello from Bob #{i}"} for i in range(_PAGE_SIZE + 5)]
    posts.append({"username": charlie, "content": "Hello from Charlie"})
    posts.append({"username": alice, "content": "Hello from Alice"})
    bob_post_ids = []
    for idx, post in enumerate(posts):
        r = requests.post(url=_POSTS_URL.format(port=port), json=post)
        if not r.ok:
            logger.warning("POST /posts not ok: %d %s", r.status_code, r.reason)
            return False
        # Some implementations might return the ID in the response
        try:
            post_id = r.json().get("id")
            if idx < _PAGE_SIZE + 5 and post_id is not None:
                bob_post_ids.append(post_id)
        except:
            pass

    # 3. Alice follows Bob
    r = requests.post(
        url=_FOLLOW_URL.format(port=port),
        json={"follower_username": alice, "following_username": bob},
    )
    if not r.ok:
        logger.warning("POST /follow not ok: %d %s", r.status_code, r.reason)
        return False

    r = requests.post(
        url=_FOLLOW_URL.format(port=port),
        json={"follower_username": alice, "following_username": bob},
    )
    if not r.ok:
        logger.warning("Duplicate POST /follow should be idempotent, got: %d %s", r.status_code, r.reason)
        return False

    # 4. Check Alice's feed pagination. Page 1 omits the page parameter to
    # verify that it defaults to page 1.
    r = requests.get(url=_FEED_URL.format(port=port), params={"username": alice})
    if not r.ok:
        logger.warning("GET /feed not ok: %d %s", r.status_code, r.reason)
        return False

    feed_page_1 = _parse_paginated_response(r, "GET /feed", 1, logger)
    if feed_page_1 is None:
        return False
    feed_items_1 = feed_page_1["items"]
    if len(feed_items_1) != _PAGE_SIZE:
        logger.warning("Feed page 1 returned %d items, expected %d", len(feed_items_1), _PAGE_SIZE)
        return False
    if not feed_page_1["has_next"]:
        logger.warning("Feed page 1 should report has_next=true")
        return False
    if not all(p["username"] == bob for p in feed_items_1):
        logger.warning("Alice's feed page 1 should contain only Bob's posts")
        return False

    r = requests.get(url=_FEED_URL.format(port=port), params={"username": alice, "page": 2})
    if not r.ok:
        logger.warning("GET /feed page 2 not ok: %d %s", r.status_code, r.reason)
        return False

    feed_page_2 = _parse_paginated_response(r, "GET /feed page 2", 2, logger)
    if feed_page_2 is None:
        return False
    feed_items_2 = feed_page_2["items"]
    if len(feed_items_2) < 5:
        logger.warning("Feed page 2 returned %d items, expected at least 5", len(feed_items_2))
        return False
    feed_items_combined = feed_items_1 + feed_items_2
    if any(p["username"] == charlie for p in feed_items_combined):
        logger.warning("Alice's feed should not contain Charlie's post")
        return False
    if any(p["username"] == alice for p in feed_items_combined):
        logger.warning("Alice's feed should not contain her own post")
        return False
        
    feed_ids = [p["id"] for p in feed_items_combined]
    if len(feed_ids) != len(set(feed_ids)):
        logger.warning("Duplicate follow should not create duplicate feed entries")
        return False

    previous_created_at = None
    previous_id = None
    for post in feed_items_combined:
        created_at = post.get("created_at")
        post_id = post.get("id")
        if previous_created_at is not None and created_at is not None:
            if created_at > previous_created_at:
                logger.warning("Feed posts should be ordered by created_at descending")
                return False
            if created_at == previous_created_at and post_id is not None and previous_id is not None:
                if post_id > previous_id:
                    logger.warning("Feed posts with equal created_at should be ordered by id descending")
                    return False
        previous_created_at = created_at
        previous_id = post_id

    # 5. Like all visible Bob posts and check trending pagination.
    if not bob_post_ids:
        bob_post_ids = [p["id"] for p in feed_items_1 + feed_items_2]

    if len(bob_post_ids) < _PAGE_SIZE + 5:
        logger.warning("Could not find all Bob post IDs to test trending pagination")
        return False

    for pid in bob_post_ids:
        r = requests.post(url=_LIKE_URL.format(port=port, postId=pid), json={"username": alice})
        if not r.ok:
            logger.warning("POST /like not ok: %d %s", r.status_code, r.reason)
            return False

    r = requests.post(url=_LIKE_URL.format(port=port, postId=bob_post_ids[0]), json={"username": alice})
    if not r.ok:
        logger.warning("Duplicate POST /like should be idempotent, got: %d %s", r.status_code, r.reason)
        return False

    r = requests.get(url=_TRENDING_URL.format(port=port))
    if not r.ok:
        logger.warning("GET /trending not ok: %d %s", r.status_code, r.reason)
        return False

    trending_page_1 = _parse_paginated_response(r, "GET /trending", 1, logger)
    if trending_page_1 is None:
        return False
    trending_items_1 = trending_page_1["items"]
    if len(trending_items_1) != _PAGE_SIZE:
        logger.warning("Trending page 1 returned %d items, expected %d", len(trending_items_1), _PAGE_SIZE)
        return False
    if not trending_page_1["has_next"]:
        logger.warning("Trending page 1 should report has_next=true")
        return False
    if not any(p["id"] in bob_post_ids for p in trending_items_1):
        logger.warning("Trending missing liked posts")
        return False

    r = requests.get(url=_TRENDING_URL.format(port=port), params={"page": 2})
    if not r.ok:
        logger.warning("GET /trending page 2 not ok: %d %s", r.status_code, r.reason)
        return False

    trending_page_2 = _parse_paginated_response(r, "GET /trending page 2", 2, logger)
    if trending_page_2 is None:
        return False
    if len(trending_page_2["items"]) < 5:
        logger.warning("Trending page 2 returned %d items, expected at least 5", len(trending_page_2["items"]))
        return False

    previous_like_count = None
    previous_id = None
    for post in trending_items_1 + trending_page_2["items"]:
        like_count = post["like_count"]
        post_id = post["id"]
        if previous_like_count is not None:
            if like_count > previous_like_count:
                logger.warning("Trending posts should be ordered by like_count descending")
                return False
            if like_count == previous_like_count and post_id > previous_id:
                logger.warning("Trending posts with equal like_count should be ordered by id descending")
                return False
        previous_like_count = like_count
        previous_id = post_id

    if not all(p["id"] in bob_post_ids for p in trending_items_1 + trending_page_2["items"][:5]):
        logger.warning("Trending first pages should contain the liked Bob posts")
        return False
    if any(p["like_count"] != 1 for p in trending_items_1 + trending_page_2["items"][:5]):
        logger.warning("Duplicate like should not increment like_count more than once")
        return False

    return True


SCENARIO = Scenario(
    id="MicroBlog",
    api_spec=_API_SPEC,
    text_spec=_TEXT_SPEC,
    json_api_spec=_JSON_API_SPEC,
    short_app_description="Social media API with feeds and trending posts",
    scenario_instructions="",
    needs_db=True,
    needs_secret=False,
    functional_tests=[func_test_microblog_basic],
    security_tests=[], # Can add some later
    performance_tests=["MicroBlogUser"]
)
