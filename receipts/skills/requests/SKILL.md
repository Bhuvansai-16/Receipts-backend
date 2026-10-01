---
name: requests
description: Read for requests issues (headers, bodies, URLs, sessions or prepared requests).
---
# requests, without sending anything

- Tests must not make network requests. Build and prepare the request instead:

      req = requests.Request("GET", "http://example.com").prepare()
      assert "Content-Length" not in req.headers

- Session behaviour without sending: s = requests.Session(); p = s.prepare_request(requests.Request(...)).
- To test response handling, build one by hand: r = requests.models.Response(); r.status_code = 200;
  r._content = b"..."; r.headers["Content-Type"] = "application/json".
- Header names are case-insensitive: req.headers.get("content-length") works.
