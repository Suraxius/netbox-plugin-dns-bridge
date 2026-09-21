from rest_framework.routers import APIRootView

from netbox.api.viewsets import NetBoxModelViewSet

from netbox_dns_bridge.filtersets import (
    CatalogZoneFilterSet,
    CatalogZoneMemberFilterSet,
    SeenTransferClientFilterSet,
)
from netbox_dns_bridge.models import CatalogZone, CatalogZoneMember, SeenTransferClient

from .serializers import (
    CatalogZoneSerializer,
    CatalogZoneMemberSerializer,
    SeenTransferClientSerializer,
)

__all__ = (
    "NetBoxDNSBridgeRootView",
    "CatalogZoneViewSet",
    "CatalogZoneMemberViewSet",
    "SeenTransferClientViewSet",
)


class NetBoxDNSBridgeRootView(APIRootView):
    def get_view_name(self):
        return "NetBoxDNSBridge"


class CatalogZoneViewSet(NetBoxModelViewSet):
    queryset = CatalogZone.objects.all()
    serializer_class = CatalogZoneSerializer
    filterset_class = CatalogZoneFilterSet


class CatalogZoneMemberViewSet(NetBoxModelViewSet):
    queryset = CatalogZoneMember.objects.all()
    serializer_class = CatalogZoneMemberSerializer
    filterset_class = CatalogZoneMemberFilterSet


class SeenTransferClientViewSet(NetBoxModelViewSet):
    queryset = SeenTransferClient.objects.all()
    serializer_class = SeenTransferClientSerializer
    filterset_class = SeenTransferClientFilterSet
