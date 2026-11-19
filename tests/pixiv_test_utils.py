"""Shared assertions' traversal of leaf messages in Pixiv forward trees."""

from collections.abc import Iterable

from astrbot.api.message_components import ComponentType, Node, Plain


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
        if children:
            pages.extend(page_nodes(children))
        elif any(
            part.type == ComponentType.Image
            or (
                isinstance(part, Plain)
                and any(
                    host in part.text
                    for host in ("https://i.pximg.net/", "https://i.pixiv.re/")
                )
            )
            for part in node.content
        ):
            pages.append(node)
    return pages
