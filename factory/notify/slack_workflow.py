"""The slack-workflow adapter: one JSON POST per person to the Slack Workflow Builder webhook in the env var
`notify.adapters.slack-workflow.url_env`. Person address: the Slack user email. It sends only: people answer on GitHub,
so there is no `ack`. The `ref` of a send is the request id of the Slack response, or `none`. The URL is a secret:
no message or error names it.
"""
import json
import os
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would turn the POST into a GET and could report success: fail with the original status."""
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


class SlackWorkflowAdapter:
    def __init__(self, config):
        env = config.get("url_env")
        self.url = os.environ.get(env or "")
        if not self.url:
            raise ValueError(f"env var `{env}` (notify.adapters.slack-workflow.url_env) is not set")

    def send(self, person, message):
        from . import render  # the package imports this module first
        issue_url = message.get("url") or ""
        body = {"recipient": person["slack-workflow"], "title": " ".join(str(message["problem"]).split()),
                "text": f"{render(message)}\n\nAnswer on GitHub: {issue_url}", "issue_url": issue_url,
                "run_url": message.get("run_url") or ""}
        try:
            request = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with OPENER.open(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Slack workflow webhook answered HTTP {e.code}") from None
        except urllib.error.URLError as e:
            why = e.reason if isinstance(e.reason, OSError) else "invalid URL"  # a text reason can hold the URL
            raise RuntimeError(f"Slack workflow webhook unreachable: {why}") from None
        except ValueError:  # urllib puts the URL in this message
            raise RuntimeError("Slack workflow webhook URL is invalid") from None
        try:
            return str(json.loads(raw).get("request_id") or "none")
        except (ValueError, AttributeError):
            return "none"
