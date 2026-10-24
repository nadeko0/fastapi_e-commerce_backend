"""A response model survives its own JSON: dump it, read it back, get it back.

For every response_model of every route (the models behind the OpenAPI
responses), Hypothesis builds instances out of the model's field types and the
test checks

    Model.model_validate_json(instance.model_dump_json()) == instance

Pydantic's JSON mode is what leaves the server (Decimal as a string, datetime
as an RFC 3339 string), and model_validate_json is how a client with the same
strict model reads it. model_validate(json.loads(...)) is not the same check:
strict models refuse a str for a Decimal or datetime in Python mode and accept
it in JSON mode (Pydantic docs, "JSON" concept page). Money and timestamps are
the point: an amount must come back as the same Decimal and a timestamp as the
same instant, with nothing lost in the string.

Pydantic v2 ships no Hypothesis plugin, and st.builds ignores Pydantic's own
constraints (decimal_places, pattern), so the strategies are written from the
annotations. Instances are made with model_construct: the values are valid by
construction, and building through validation would send money through the
input validators, which accept only strings.

A model that cannot round-trip by design goes into NOT_ROUND_TRIPPABLE with
the reason next to its name; the guard test fails if the table names a model
that is not a response model any more.
"""
import datetime as dt
import re
import types
import typing
from decimal import Decimal
from enum import Enum

import pytest
from fastapi.routing import APIRoute
from hypothesis import HealthCheck, given, reject, settings
from hypothesis import strategies as st
from pydantic import BaseModel, EmailStr, ValidationError
from pydantic.fields import FieldInfo

from app.main import app

# None at the time of writing: every response model round-trips.
NOT_ROUND_TRIPPABLE: dict[str, str] = {}

IMAGE_URLS = st.sampled_from(["https://cdn.example/a.png", "https://cdn.example/b.jpg"])

PHONES = st.one_of(st.none(), st.from_regex(r"\+[1-9][0-9]{6,14}", fullmatch=True))

# Rules a model enforces in a validator rather than in a field type, which a
# strategy built from the types cannot see.
FIELD_STRATEGIES = {
    # ProductBase.validate_images: at least one image, no duplicates.
    ("ProductResponse", "images"): st.lists(IMAGE_URLS, min_size=1, max_size=2, unique=True),
    # _validate_phone: 5 to 15 digits after the plus sign.
    ("UserResponse", "phone"): PHONES,
    ("AddressResponse", "delivery_phone"): PHONES,
}


def _routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _routes(route.original_router.routes)


def _response_models() -> dict[str, type[BaseModel]]:
    models = {}
    for route in _routes(app.routes):
        model = route.response_model
        if isinstance(model, type) and issubclass(model, BaseModel):
            models[model.__name__] = model
    return models


def _reachable(models) -> dict[str, type[BaseModel]]:
    """The models and every model named in their fields, at any depth."""
    found = {}

    def visit(annotation):
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation.__name__ not in found:
                found[annotation.__name__] = annotation
                for field in annotation.model_fields.values():
                    visit(field.annotation)
            return
        for part in typing.get_args(annotation):
            visit(part)

    for model in models:
        visit(model)
    return found


MODELS = _response_models()
KNOWN = _reachable(MODELS.values())
_BUILT: dict[str, st.SearchStrategy] = {}


def _limits(metadata) -> dict:
    """ge/gt/le/lt, lengths, digits and pattern from annotated_types and
    Pydantic constraint objects, whether or not wrapped in a FieldInfo."""
    limits = {}
    for item in metadata:
        if isinstance(item, FieldInfo):
            limits.update(_limits(item.metadata))
        for name in ("ge", "gt", "le", "lt", "min_length", "max_length",
                     "max_digits", "decimal_places", "pattern"):
            value = getattr(item, name, None)
            if value is not None:
                limits[name] = value
    return limits


def _strategy(annotation, metadata=(), owner=None) -> st.SearchStrategy:
    if isinstance(annotation, typing.ForwardRef):
        return _model_strategy(KNOWN[annotation.__forward_arg__])
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        return _strategy(args[0], (*metadata, *args[1:]), owner)
    if annotation is typing.Any:
        return st.one_of(st.none(), st.booleans(), st.integers(-1000, 1000), st.text(max_size=10))
    if origin in (typing.Union, types.UnionType):
        return st.one_of(*(_strategy(arg, metadata, owner) for arg in args))
    if annotation is type(None):
        return st.none()
    if origin is typing.Literal:
        return st.sampled_from(args)
    if origin is list:
        # A model that contains a list of itself (the category tree) stays
        # finite because each level holds at most one child.
        return st.lists(_strategy(args[0], owner=owner), max_size=1 if args[0] is owner else 3)
    if annotation is dict:
        return st.dictionaries(st.text(min_size=1, max_size=8),
                               st.one_of(st.integers(), st.text(max_size=8)), max_size=3)
    if origin is dict:
        return st.dictionaries(st.text(min_size=1, max_size=8),
                               _strategy(args[1], owner=owner), max_size=3)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return st.sampled_from(list(annotation))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _model_strategy(annotation)

    limits = _limits(metadata)
    if annotation is bool:
        return st.booleans()
    if annotation is int:
        low = limits.get("ge", limits["gt"] + 1 if "gt" in limits else 0)
        high = limits.get("le", limits["lt"] - 1 if "lt" in limits else low + 10**6)
        return st.integers(low, max(low, min(high, low + 10**6)))
    if annotation is Decimal:
        low = limits.get("ge", limits.get("gt", 0))
        high = limits.get("le", limits.get("lt", Decimal("99999999.99")))
        return st.decimals(min_value=Decimal(low), max_value=Decimal(high),
                           places=limits.get("decimal_places", 2),
                           allow_nan=False, allow_infinity=False)
    if annotation is str:
        if "pattern" in limits:
            return st.from_regex(limits["pattern"], fullmatch=True)
        return st.text(min_size=limits.get("min_length", 0),
                       max_size=min(limits.get("max_length", 20), 20))
    if annotation is EmailStr:
        return st.emails()
    if annotation is dt.datetime:
        return st.datetimes(timezones=st.just(dt.UTC))
    if annotation is dt.date:
        return st.dates()
    if getattr(annotation, "__name__", "") == "HttpUrl":
        return IMAGE_URLS
    raise AssertionError(f"no strategy for {annotation!r}; teach _strategy about it")


def _model_strategy(model: type[BaseModel]) -> st.SearchStrategy:
    if model.__name__ not in _BUILT:
        # deferred: a model may (through its children) contain itself.
        _BUILT[model.__name__] = st.deferred(lambda: st.builds(
            lambda values: model.model_construct(**values),
            st.fixed_dictionaries({
                name: (FIELD_STRATEGIES[model.__name__, name]
                       if (model.__name__, name) in FIELD_STRATEGIES
                       else _strategy(field.annotation, field.metadata, model))
                for name, field in model.model_fields.items()
            })))
    return _BUILT[model.__name__]


@pytest.mark.parametrize("name", sorted(set(MODELS) - set(NOT_ROUND_TRIPPABLE)))
def test_a_response_model_round_trips_through_its_json(name):
    model = MODELS[name]
    accepted = []

    # too_slow: the first draws build the nested strategies, which takes
    # a moment and says nothing about the model.
    @settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow])
    @given(_model_strategy(model))
    def check(raw):
        # The response models normalise on the way in (a country is title-cased,
        # an address e-mail lower-cased, a phone gets its plus sign), so a
        # value drawn from the types is first read through the model once, the
        # way a stored value is when a route builds its response. What that
        # produces is a real response; it is the one that must survive its JSON.
        try:
            instance = model.model_validate_json(raw.model_dump_json())
        except ValidationError:
            reject()  # a value the model's own rules refuse is not a response
        accepted.append(instance)
        assert model.model_validate_json(instance.model_dump_json()) == instance

    check()
    # Not vacuous: the model accepted what was drawn, not only rejected it.
    assert accepted, f"every drawn value was refused by {name}; the strategy is wrong"


def test_the_exceptions_table_names_only_response_models():
    assert set(NOT_ROUND_TRIPPABLE) <= set(MODELS)


@settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow])
@given(_model_strategy(MODELS["OrderResponse"]))
def test_an_order_writes_money_with_two_decimals_and_timestamps_with_z(raw):
    """The wire form the round trip rests on, stated once on a real response:
    an amount is a string with exactly two decimals, a timestamp ends in Z."""
    try:
        order = MODELS["OrderResponse"].model_validate_json(raw.model_dump_json())
    except ValidationError:
        reject()
    wire = order.model_dump(mode="json")
    assert re.fullmatch(r"-?\d+\.\d{2}", wire["total_amount"]), wire["total_amount"]
    assert wire["created_at"].endswith("Z"), wire["created_at"]
