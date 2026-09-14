"""Page numbers for a table footer, without printing every one of them."""

from django import template

register = template.Library()


@register.simple_tag
def elided_pages(page_obj, on_each_side=2, on_ends=1):
    """The page numbers worth showing, with `…` standing in for the gaps."""
    paginator = page_obj.paginator
    return list(paginator.get_elided_page_range(
        page_obj.number, on_each_side=on_each_side, on_ends=on_ends))


@register.simple_tag
def page_ellipsis():
    """What `elided_pages` uses for a gap, so the template can recognise it."""
    from django.core.paginator import Paginator
    return Paginator.ELLIPSIS
