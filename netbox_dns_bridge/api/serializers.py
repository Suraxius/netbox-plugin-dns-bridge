from rest_framework import serializers

from netbox.api.serializers import NetBoxModelSerializer, WritableNestedSerializer

import netbox_dns.models
from netbox_dns.api.nested_serializers import NestedZoneSerializer

from netbox_dns_bridge.models import CatalogZone, CatalogZoneMember, SeenTransferClient

__all__ = (
    "CatalogZoneSerializer",
    "CatalogZoneMemberSerializer",
    "SeenTransferClientSerializer",
    "NestedCatalogZoneSerializer",
    "NestedCatalogZoneMemberSerializer",
)


class NestedViewSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_dns-api:view-detail"
    )

    class Meta:
        model = netbox_dns.models.View
        fields = ("id", "url", "display", "name")
        brief_fields = ("id", "url", "display", "name")


class NestedCatalogZoneSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_dns_bridge-api:catalogzone-detail"
    )

    class Meta:
        model = CatalogZone
        fields = ("id", "url", "display", "view", "soa_serial")
        brief_fields = ("id", "url", "display", "view", "soa_serial")


class NestedCatalogZoneMemberSerializer(WritableNestedSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_dns_bridge-api:catalogzonemember-detail"
    )

    class Meta:
        model = CatalogZoneMember
        fields = ("id", "url", "display", "name", "zone", "catalog_zone")
        brief_fields = ("id", "url", "display", "name", "zone", "catalog_zone")


class CatalogZoneSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_dns_bridge-api:catalogzone-detail"
    )
    view = NestedViewSerializer()

    class Meta:
        model = CatalogZone
        fields = (
            "id",
            "url",
            "display",
            "display_url",
            "view",
            "soa_serial",
            "soa_refresh",
            "soa_retry",
            "soa_expire",
            "soa_minimum",
            "created",
            "last_updated",
            "tags",
            "custom_fields",
        )
        brief_fields = ("id", "url", "display", "view", "soa_serial")


class CatalogZoneMemberSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_dns_bridge-api:catalogzonemember-detail"
    )
    zone = NestedZoneSerializer()
    catalog_zone = NestedCatalogZoneSerializer()

    class Meta:
        model = CatalogZoneMember
        fields = (
            "id",
            "url",
            "display",
            "display_url",
            "name",
            "zone",
            "catalog_zone",
            "created",
            "last_updated",
            "tags",
            "custom_fields",
        )
        brief_fields = ("id", "url", "display", "name", "zone", "catalog_zone")


class SeenTransferClientSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_dns_bridge-api:seentransferclient-detail"
    )
    view = NestedViewSerializer()

    class Meta:
        model = SeenTransferClient
        fields = (
            "id",
            "url",
            "display",
            "display_url",
            "source_ip",
            "last_seen",
            "view",
            "created",
            "last_updated",
            "tags",
            "custom_fields",
        )
        brief_fields = ("id", "url", "display", "source_ip", "last_seen", "view")
