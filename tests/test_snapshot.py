import httpx
import respx

from catalyst import snapshot
from catalyst.enrich import LexiconScorer

PROPOSALS = [
    {
        "id": "0xabc",
        "title": "Protocol Fee Expansion: Three More Chains",
        "state": "active",
        "created": 1778952290,
        "author": "0xdead",
        "link": "https://snapshot.box/#/s:uniswapgovernance.eth/proposal/0xabc",
        "space": {"id": "uniswapgovernance.eth", "name": "Uniswap"},
    }
]


def test_proposal_normalization_and_symbol_attribution():
    posts = snapshot.proposals_to_posts(PROPOSALS, symbols={"uniswapgovernance.eth": "UNI"})
    p = posts[0]
    assert p.source == "snapshot"
    assert p.uri == "snapshot:proposal:0xabc"
    assert "Governance proposal $UNI" in p.text
    assert "Uniswap" in p.text
    assert p.author.handle == "UNI"
    assert p.created_at.startswith("2026-")


def test_proposal_classifies_as_governance_with_asset():
    post = snapshot.proposals_to_posts(PROPOSALS, symbols={"uniswapgovernance.eth": "UNI"})[0]
    e = LexiconScorer().score(post.text)
    assert e.catalyst == "governance"
    assert e.assets == ["UNI"]


@respx.mock
def test_fetch_proposals_queries_graphql():
    route = respx.post("https://hub.snapshot.org/graphql").mock(
        return_value=httpx.Response(200, json={"data": {"proposals": PROPOSALS}})
    )
    posts = snapshot.fetch_proposals(["uniswapgovernance.eth"], state="active")
    assert route.called
    # The space filter and state went into the GraphQL variables.
    sent = route.calls[0].request
    import json
    body = json.loads(sent.content)
    assert body["variables"]["where"]["space_in"] == ["uniswapgovernance.eth"]
    assert body["variables"]["where"]["state"] == "active"
    assert posts[0].source == "snapshot"


@respx.mock
def test_fetch_proposals_raises_on_graphql_errors():
    respx.post("https://hub.snapshot.org/graphql").mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "bad space"}]})
    )
    import pytest

    with pytest.raises(RuntimeError, match="Snapshot GraphQL errors"):
        snapshot.fetch_proposals(["nope.eth"])
