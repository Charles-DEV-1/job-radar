"""Isolated Chromium renderer. All GET requests are fulfilled by the parent."""

import base64
import json
import os
import sys

from playwright.sync_api import Route, sync_playwright

from pipeline.http import USER_AGENT


def emit(message: dict[str, object]) -> None:
    print(json.dumps(message), flush=True)


def main() -> None:
    def route(request: Route) -> None:
        if request.request.method != "GET" or request.request.resource_type in {
            "image",
            "media",
            "font",
            "websocket",
        }:
            request.abort()
            return
        emit(
            {
                "type": "request",
                "url": request.request.url,
                "document": request.request.resource_type == "document",
            }
        )
        line = sys.stdin.readline()
        if not line:
            request.abort()
            return
        response = json.loads(line)
        if response.get("abort"):
            request.abort()
        else:
            request.fulfill(
                status=response["status"],
                headers=response["headers"],
                body=base64.b64decode(response["body"]),
            )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            )
            context = browser.new_context(
                user_agent=USER_AGENT, service_workers="block"
            )
            context.route("**/*", route)
            context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.new_page()
            page.set_default_timeout(int(sys.argv[2]))
            response = page.goto(sys.argv[1], wait_until="load")
            if not response or response.status >= 400:
                raise ValueError("Rendered page HTTP failure")
            page.wait_for_timeout(1500)
            page.evaluate("""() => {
                for (const node of document.querySelectorAll('body *')) {
                    const style = getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden') {
                        node.remove();
                    }
                }
            }""")
            emit({"type": "done", "url": page.url, "html": page.content()})
            browser.close()
    except Exception:
        emit({"type": "error"})


if __name__ == "__main__":
    main()
