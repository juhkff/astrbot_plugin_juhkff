"""Shared assertions' traversal of leaf messages in Pixiv forward trees."""

from collections.abc import Iterable

from astrbot.api.message_components import Node


def page_nodes(nodes: Iterable[Node]) -> list[Node]:
    """Collect leaf messages in display order without relying on tree depth.

    Args:
        nodes: Top-level or nested forward nodes.

    Returns:
        Leaf nodes containing image components or source links.
    """
    pages = []
    for node in nodes:
        children = [part for part in node.content if isinstance(part, Node)]
        pages.extend(page_nodes(children) if children else [node])
    return pages
