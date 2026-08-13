"""Pagination tuned for a till on a slow connection."""

from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    """Page-number pagination with a client-controlled, bounded page size.

    The cap matters: the Flutter client pulls the whole catalogue when it first
    syncs, and an unbounded ``page_size`` would let a device with thousands of
    items ask for all of them over a 3G connection and time out mid-transfer.
    """

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500
