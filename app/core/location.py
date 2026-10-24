"""The Location header of a 201 Created.

RFC 9110 9.3.3: a POST that created a resource SHOULD answer 201 with a
Location header field that identifies the primary resource created, and
15.3.2: without one, the target URI of the request identifies it, which for
a POST to a collection is the collection. So a route answers 201 only for
what has a URL of its own that a GET serves.

The reference is an absolute path, without scheme or host (which behind a
proxy are the proxy's to know). Starlette's `url_for` builds it from the
route's name and includes the ASGI root_path a proxy prefix sets (Starlette
docs, Routing, "Reverse URL lookups").
"""
from fastapi import Request, Response


def set_location(request: Request, response: Response, route_name: str, **path_params) -> None:
    """Put the path of the route `route_name` (the name of its function)
    with `path_params` in the Location header of `response`."""
    response.headers["Location"] = request.url_for(route_name, **path_params).path
