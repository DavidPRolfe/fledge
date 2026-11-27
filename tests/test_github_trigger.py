from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from fledge.core import Comment, Trigger, TriggerCheckError, WorkItem
from fledge.github.issues import GitHubIssuesTrigger, MissingTokenError

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


def make_trigger(handler: Handler) -> GitHubIssuesTrigger:
    return GitHubIssuesTrigger(
        repo="acme/widgets", label="fledge", transport=httpx.MockTransport(handler)
    )


def issue(
    number: int, body: str | None = "text", *, pr: bool = False
) -> dict[str, object]:
    data: dict[str, object] = {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
    }
    if pr:
        data["pull_request"] = {"url": "https://api.github.com/x"}
    return data


def comment(
    id_: int, created_at: str, *, login: str = "alice", kind: str = "User"
) -> dict[str, object]:
    return {
        "id": id_,
        "body": f"comment {id_}",
        "user": {"login": login, "type": kind},
        "created_at": created_at,
    }


def test_registry_style_construction_is_a_trigger() -> None:
    assert isinstance(GitHubIssuesTrigger(repo="acme/widgets", label="x"), Trigger)


def test_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(MissingTokenError):
        GitHubIssuesTrigger(repo="acme/widgets", label="fledge")


@pytest.mark.anyio
async def test_poll_filters_by_label_excludes_prs_and_follows_pages() -> None:
    requests: list[httpx.Request] = []
    next_url = "https://api.github.com/repositories/1/issues?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[issue(3, body=None)])
        return httpx.Response(
            200,
            json=[issue(1), issue(2, pr=True)],
            headers={"Link": f'<{next_url}>; rel="next"'},
        )

    items = await make_trigger(handler).poll()

    assert items == [
        WorkItem(
            external_id="1",
            title="Issue 1",
            body="text",
            url="https://github.com/acme/widgets/issues/1",
            repo_url="https://github.com/acme/widgets.git",
        ),
        WorkItem(
            external_id="3",
            title="Issue 3",
            body="",
            url="https://github.com/acme/widgets/issues/3",
            repo_url="https://github.com/acme/widgets.git",
        ),
    ]
    first = requests[0]
    assert first.url.path == "/repos/acme/widgets/issues"
    assert first.url.params["labels"] == "fledge"
    assert first.url.params["state"] == "open"
    assert first.headers["Authorization"] == "Bearer test-token"
    assert str(requests[1].url) == next_url


@pytest.mark.anyio
async def test_fetch_returns_issue() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/issues/7"
        return httpx.Response(200, json=issue(7))

    item = await make_trigger(handler).fetch("7")

    assert item.external_id == "7"
    assert item.url == "https://github.com/acme/widgets/issues/7"


@pytest.mark.anyio
async def test_fetch_rejects_pull_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=issue(7, pr=True))

    with pytest.raises(ValueError, match="pull request"):
        await make_trigger(handler).fetch("7")


@pytest.mark.anyio
@pytest.mark.parametrize("ref", ["abc", "#7", "-1", ""])
async def test_fetch_rejects_non_number_without_request(ref: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ValueError, match="issue number"):
        await make_trigger(handler).fetch(ref)


@pytest.mark.anyio
async def test_poll_guidance_filters_by_creation_time_and_bots() -> None:
    since = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                comment(1, "2026-01-01T11:00:00Z"),
                comment(2, "2026-01-01T13:00:00Z"),
                comment(3, "2026-01-01T13:00:00Z", login="ci[bot]", kind="Bot"),
            ],
        )

    comments = await make_trigger(handler).poll_guidance("5", since)

    assert comments == [Comment(external_id="2", author="alice", body="comment 2")]
    assert seen[0].url.path == "/repos/acme/widgets/issues/5/comments"
    assert seen[0].url.params["since"] == "2026-01-01T12:00:00+00:00"


@pytest.mark.anyio
async def test_http_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"message": "bad gateway"})

    with pytest.raises(httpx.HTTPStatusError):
        await make_trigger(handler).poll()


def access(repo: int = 200, issues: int = 200, push: int = 200) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        match (request.url.host, request.url.path):
            case ("api.github.com", "/repos/acme/widgets"):
                return httpx.Response(repo, json={})
            case ("api.github.com", "/repos/acme/widgets/issues"):
                return httpx.Response(issues, json=[])
            case ("github.com", "/acme/widgets.git/info/refs"):
                assert request.url.params["service"] == "git-receive-pack"
                assert request.headers["Authorization"].startswith("Basic ")
                return httpx.Response(push)
        raise AssertionError(f"unexpected request {request.url}")

    return handler


@pytest.mark.anyio
async def test_check_passes_when_the_token_can_read_and_push() -> None:
    await make_trigger(access()).check()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (access(repo=401, issues=401, push=401), "rejected GITHUB_TOKEN"),
        (access(repo=404, issues=404, push=404), "cannot see acme/widgets"),
        (access(issues=403), "cannot read the issues of acme/widgets"),
        (access(push=403), "cannot push to acme/widgets"),
    ],
)
async def test_check_names_the_missing_access(handler: Handler, message: str) -> None:
    with pytest.raises(TriggerCheckError, match=message):
        await make_trigger(handler).check()


@pytest.mark.anyio
async def test_check_lets_other_github_errors_through() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await make_trigger(access(repo=500)).check()
