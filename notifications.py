"""Optional Pushover completion notifications; credentials stay in the environment."""
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def notify(message, *, title="Bluefin build", url=None):
    token = os.environ.get("PUSHOVER_APP_TOKEN")
    user = os.environ.get("PUSHOVER_USER_KEY")
    if not token and not user:
        return "disabled"
    if not token or not user:
        return "failed: set both PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY"
    data = {"token": token, "user": user, "title": title, "message": message[:1024]}
    if url:
        data.update(url=url, url_title="Build status")
    try:
        request = Request("https://api.pushover.net/1/messages.json",
                          data=urlencode(data).encode(), method="POST")
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
        if result.get("status") != 1:
            return "failed: Pushover rejected the notification"
        return "sent"
    except Exception as error:
        # Do not log request bodies, which contain the credentials.
        return f"failed: notification delivery error ({type(error).__name__})"
