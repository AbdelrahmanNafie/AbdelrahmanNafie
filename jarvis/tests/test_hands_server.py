import json

import anyio
from mcp.client.client import Client

from jarvis.hands_server import build_server


def _payload(result):
    return result.structured_content or json.loads(result.content[0].text)


def test_claude_sees_only_the_allowlisted_tools(gateway):
    async def go():
        async with Client(build_server(gateway)) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert tools == {"list_files", "read_file", "system_info", "write_note", "open_app",
                             "delete_file", "open_website", "google_search", "read_web_page",
                             "search_files", "open_file_or_folder", "draft_message", "media_control",
                             "lock_screen", "list_running_apps", "close_app", "read_clipboard",
                             "copy_to_clipboard", "remember", "recall", "forget", "db_add", "db_find",
                             "db_update", "db_delete", "reply_to_user"}
            r = _payload(await client.call_tool("write_note", {"name": "hi", "text": "hello"}))
            assert r["status"] == "ok"
            r = _payload(await client.call_tool("reply_to_user", {"text": "Done"}))
            assert r["status"] == "delivered"
    anyio.run(go)
    kinds = [e["kind"] for e in gateway.store.events()]
    assert kinds[-1] == "reply"
