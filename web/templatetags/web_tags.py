from typing import Any

from django import template
from django.utils.safestring import SafeString

from web.jsonld import ldjson_script

register = template.Library()


@register.filter
def ldjson(data: Any) -> SafeString:
    """{{ data|ldjson }}: the JSON-LD script element, escaped like json_script."""
    return ldjson_script(data)
