"""PATCH support shared by the routes that also have a PUT update.

The update schemas already implement JSON Merge Patch (RFC 7396) semantics
on the PUT routes: members present replace, an explicit null clears a
nullable field (and is a 422 "may be omitted but cannot be null" on a
non-nullable one), absent members are unchanged. PATCH reuses the very same
handler, so auth, validation, side effects and responses are identical.

Request media types: FastAPI parses any ``application/json`` or
``application/*+json`` body, which includes ``application/merge-patch+json``
(RFC 7396 section 5). Any other media type is a 415 with an Accept header
naming both (app/core/errors.py); the OpenAPI document lists both JSON types.
"""

MERGE_PATCH_MEDIA_TYPE = "application/merge-patch+json"

PATCH_DESCRIPTION = (
    "JSON Merge Patch (RFC 7396). Members present replace the stored value, "
    "`null` clears a nullable field (422 on a non-nullable one), absent members "
    "are unchanged. Accepts `application/merge-patch+json` and `application/json`."
)


def merge_patch_body(schema_name: str) -> dict:
    """``openapi_extra`` that documents the merge-patch media type next to JSON."""
    return {
        "requestBody": {
            "content": {
                MERGE_PATCH_MEDIA_TYPE: {
                    "schema": {"$ref": f"#/components/schemas/{schema_name}"}
                }
            }
        }
    }
