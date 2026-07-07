from catalyst.rss import parse_feed

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example News</title>
  <link>https://example.com</link>
  <item>
    <title>Markets rise &amp; rally <![CDATA[<b>today</b>]]></title>
    <link>https://example.com/a</link>
    <guid isPermaLink="false">tag:example,1</guid>
    <pubDate>Wed, 02 Oct 2002 13:00:00 GMT</pubDate>
    <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Jane Doe</dc:creator>
    <description>Some &lt;summary&gt; text</description>
  </item>
  <item>
    <title>No guid here</title>
    <link>https://example.com/b</link>
    <pubDate>Thu, 03 Oct 2002 09:30:00 GMT</pubDate>
    <author>editor@example.com (Ed Editor)</author>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Source</title>
  <entry>
    <title>Atom Post One</title>
    <link rel="alternate" href="https://example.com/atom1"/>
    <id>urn:uuid:1</id>
    <published>2026-06-10T08:00:00Z</published>
    <author><name>Sam Smith</name></author>
  </entry>
</feed>"""


def test_rss_parses_items_and_decodes_entities_cdata():
    items = parse_feed(RSS)
    assert len(items) == 2
    assert items[0].source == "rss"
    # feedparser strips CDATA tags from title text; entity decoded.
    assert "Markets rise & rally" in items[0].text


def test_rss_uri_prefers_guid_falls_back_to_link():
    items = parse_feed(RSS)
    assert items[0].uri == "tag:example,1"
    assert items[0].url == "https://example.com/a"
    assert items[1].uri == "https://example.com/b"  # no guid -> link


def test_rss_dates_iso_and_author_feed_title():
    items = parse_feed(RSS)
    assert items[0].created_at == "2002-10-02T13:00:00+00:00"
    assert items[0].indexed_at == items[0].created_at
    assert items[0].author.handle == "Example News"
    assert items[0].author.display_name == "Jane Doe"


def test_rss_metrics_zero():
    item = parse_feed(RSS)[0]
    assert item.metrics.model_dump() == {"likes": 0, "reposts": 0, "replies": 0, "quotes": 0}


def test_atom_link_id_date_nested_author():
    items = parse_feed(ATOM)
    assert len(items) == 1
    assert items[0].source == "rss"
    assert items[0].uri == "urn:uuid:1"
    assert items[0].url == "https://example.com/atom1"
    assert items[0].created_at == "2026-06-10T08:00:00+00:00"
    assert items[0].author.handle == "Atom Source"
    assert items[0].author.display_name == "Sam Smith"
